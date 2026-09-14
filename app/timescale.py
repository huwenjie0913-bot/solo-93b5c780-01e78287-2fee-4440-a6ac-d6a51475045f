"""时间换算与时钟模型。

统一时间轴采用 UTC Unix 秒（浮点）。时钟模型为线性模型：

    钟面时刻 c 与真实时刻 t（均以参考历元 ``t_ref`` 归零）满足
        c - t_ref = a + β * (t - t_ref)
    其中 a 为偏移（秒），β = 1 + r，r 为线性漂移率（ppm 报告 r*1e6）。

校时锚点用普通最小二乘（OLS）拟合 (t, c)；事件由钟面读数反演真实时刻，
并给出合成不确定半宽（拟合残差预测区间 + 锚点参考不确定度 + 读数粒度 +
时钟基线误差；锚点覆盖范围外再叠加先验漂移外推量）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import (
    CalibrationAnchor,
    ClockModel,
    Event,
    EventIntervalInput,
    Quantity,
    SourceClock,
)


class TimeParseError(ValueError):
    pass


class ClockModelError(ValueError):
    pass


def parse_unix(value: str, fallback_utc_offset_s: float = 0.0) -> tuple[float, bool]:
    """解析 ISO 8601 字符串为 UTC Unix 秒。

    带显式偏移的字符串按其偏移换算；naive 字符串视为 ``fallback_utc_offset_s``
    指定的本地时间。返回 (unix 秒, 是否带显式时区)。
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeParseError("时间字符串为空")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeParseError(f"无法解析 ISO 8601 时间：{value!r}（{exc}）") from exc
    if dt.tzinfo is not None:
        return dt.timestamp(), True
    # naive：按给定 UTC 偏移解释本地钟面，utc = local - offset
    return dt.timestamp() - fallback_utc_offset_s, False


def format_iso(unix_seconds: float) -> str:
    """Unix 秒格式化为带 Z 的 UTC ISO 8601 字符串（保留毫秒）。"""
    if not math.isfinite(unix_seconds):
        return "invalid"
    dt = datetime.fromtimestamp(unix_seconds, tz=None)
    whole = int(math.floor(unix_seconds))
    millis = int(round((unix_seconds - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    base = datetime.utcfromtimestamp(whole).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{millis:03d}Z" if millis else f"{base}Z"


@dataclass
class FittedClock:
    source_id: str
    t_ref: float
    intercept: float  # a：t_ref 处 clock - true（秒）
    beta: float  # β：clock 相对 true 的走时速率
    sigma_fit: float  # 拟合残差标准差（钟面秒，n>=2）
    anchor_unc: float  # 锚点参考时刻最大不确定度（秒）
    n: int
    t_span: tuple[float, float] | None  # 锚点真实时刻覆盖范围
    sxx: float
    x_mean: float
    base_uncertainty: float
    drift_prior_ppm: float
    anchor_ids: list[str] = field(default_factory=list)
    extrapolating: bool = False
    warnings: list[str] = field(default_factory=list)

    def invert(self, clock_t: float, reading_uncertainty_s: float = 0.0) -> tuple[float, float]:
        """钟面时刻 -> (真实时刻中心, 不确定半宽)，单位均为秒。"""
        if self.beta <= 1e-9:
            raise ClockModelError(
                f"来源 {self.source_id} 拟合速率 β={self.beta:g} 非正，时钟模型不可逆"
            )
        x = clock_t - self.t_ref  # 钟面相对历元
        center = self.t_ref + (x - self.intercept) / self.beta

        if self.n >= 2:
            # OLS 单样本预测区间（钟面秒）
            pred = self.sigma_fit * math.sqrt(
                max(0.0, 1.0 + 1.0 / self.n + (x - self.x_mean) ** 2 / self.sxx)
            )
            sigma_clock = math.sqrt(pred * pred + self.anchor_unc * self.anchor_unc)
        elif self.n == 1:
            sigma_clock = self.anchor_unc
        else:
            sigma_clock = 0.0

        half = (reading_uncertainty_s + sigma_clock) / self.beta + self.base_uncertainty

        # 锚点覆盖范围之外：叠加先验漂移外推允许量（线性，按到覆盖区的距离）
        if self.t_span is not None:
            lo, hi = self.t_span
            if clock_t < lo:
                half += self.drift_prior_ppm * (lo - clock_t) / 1e6
            elif clock_t > hi:
                half += self.drift_prior_ppm * (clock_t - hi) / 1e6
        else:
            half += self.drift_prior_ppm * abs(x) / 1e6

        return center, max(half, 0.0)


def _fit_anchors(
    source: SourceClock,
    anchors: list[CalibrationAnchor],
    default_offset_s: float,
) -> tuple[FittedClock, list[str], list[dict]]:
    """拟合单个来源的时钟模型，返回 (模型, 警告, 锚点换算明细)。"""
    warnings: list[str] = []
    rows: list[dict] = []
    pts: list[tuple[float, float]] = []  # (true_t, clock_t)
    max_ref_unc = 0.0

    for anc in anchors:
        clock_t, aware_c = parse_unix(anc.clock_reading, source.declared_utc_offset_s)
        true_t, aware_r = parse_unix(anc.reference_time, default_offset_s)
        pts.append((true_t, clock_t))
        max_ref_unc = max(max_ref_unc, anc.reference_uncertainty_s)
        rows.append(
            {
                "anchor_id": anc.id,
                "clock_unix_s": clock_t,
                "true_unix_s": true_t,
                "clock_explicit_tz": aware_c,
                "ref_explicit_tz": aware_r,
            }
        )
        if not aware_c:
            warnings.append(
                f"锚点 {anc.id}（来源 {source.id}）钟面读数无时区，"
                f"按来源声明偏移 {source.declared_utc_offset_s:g}s 解释"
            )

    n = len(pts)
    if n == 0:
        # t_ref 占位；事件换算时以首个事件时间为历元
        model = FittedClock(
            source_id=source.id,
            t_ref=0.0,
            intercept=0.0,
            beta=1.0,
            sigma_fit=0.0,
            anchor_unc=0.0,
            n=0,
            t_span=None,
            sxx=0.0,
            x_mean=0.0,
            base_uncertainty=source.base_uncertainty_s,
            drift_prior_ppm=source.drift_ppm,
            anchor_ids=[],
        )
        warnings.append(
            f"来源 {source.id} 无校时锚点：假定偏移为 0、速率为 1，"
            f"不确定度仅由基线 {source.base_uncertainty_s:g}s 与先验漂移 "
            f"{source.drift_ppm:g}ppm 外推给出"
        )
        return model, warnings, rows

    t_ref = pts[0][0]
    xs = [t - t_ref for t, _ in pts]
    ys = [c - t_ref for _, c in pts]
    x_mean = sum(xs) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)

    if n == 1 or sxx <= 1e-12:
        beta = 1.0
        intercept = sum(ys[i] - xs[i] for i in range(n)) / n
        sigma_fit = 0.0
        if n >= 2:
            warnings.append(
                f"来源 {source.id} 的 {n} 个锚点真实时刻完全重合，"
                "仅能估计偏移、无法识别漂移，速率按 1.0 处理"
            )
    else:
        sxy = sum((xs[i] - x_mean) * (ys[i] - sum(ys) / n) for i in range(n))
        y_mean = sum(ys) / n
        beta = sxy / sxx
        intercept = y_mean - beta * x_mean
        resid = [ys[i] - (intercept + beta * xs[i]) for i in range(n)]
        # n=2 时两点确定一条直线、无残差自由度
        sigma_fit = (
            math.sqrt(sum(r * r for r in resid) / (n - 2)) if n > 2 else 0.0
        )
        drift_ppm = (beta - 1.0) * 1e6
        if abs(drift_ppm) > source.drift_ppm + 100.0:
            warnings.append(
                f"来源 {source.id} 拟合漂移 {drift_ppm:.1f}ppm 显著超过先验 "
                f"{source.drift_ppm:g}ppm，请复核锚点"
            )

    if beta <= 1e-9:
        raise ClockModelError(
            f"来源 {source.id} 拟合速率 β={beta:g} 非正（时钟倒走/数据错误），无法反演"
        )

    t_span = (min(t for t, _ in pts), max(t for t, _ in pts))
    model = FittedClock(
        source_id=source.id,
        t_ref=t_ref,
        intercept=intercept,
        beta=beta,
        sigma_fit=sigma_fit,
        anchor_unc=max_ref_unc,
        n=n,
        t_span=t_span,
        sxx=sxx,
        x_mean=x_mean,
        base_uncertainty=source.base_uncertainty_s,
        drift_prior_ppm=source.drift_ppm,
        anchor_ids=[a.id for a in anchors],
    )
    return model, warnings, rows


def build_clock_models(
    sources: list[SourceClock],
    anchors: list[CalibrationAnchor],
    events: list[Event],
    default_offset_s: float,
) -> tuple[dict[str, FittedClock], dict[str, ClockModel], list[str], list[str]]:
    """拟合所有来源时钟。

    返回 (内部模型 dict, 对外 ClockModel 报告 dict, 警告, 错误)。
    引用了未知来源的锚点记入错误。
    """
    errors: list[str] = []
    warnings: list[str] = []
    by_source: dict[str, list[CalibrationAnchor]] = {}
    source_ids = {s.id for s in sources}
    for anc in anchors:
        if anc.source_id not in source_ids:
            errors.append(f"锚点 {anc.id} 引用了不存在的来源 {anc.source_id}")
            continue
        by_source.setdefault(anc.source_id, []).append(anc)

    models: dict[str, FittedClock] = {}
    reports: dict[str, ClockModel] = {}

    # 预先确定每个来源首个事件钟面时刻（无锚点时作为参考历元，便于解释）
    first_event_t: dict[str, float] = {}
    for ev in events:
        if ev.source_id and ev.source_id in source_ids:
            src = next(s for s in sources if s.id == ev.source_id)
            t, _ = parse_unix(ev.reading, src.declared_utc_offset_s)
            first_event_t.setdefault(ev.source_id, t)

    for source in sources:
        model, w, _rows = _fit_anchors(source, by_source.get(source.id, []), default_offset_s)
        if model.n == 0 and source.id in first_event_t:
            model.t_ref = first_event_t[source.id]
        warnings.extend(w)

        extrap = False
        if model.t_span is not None:
            for ev in events:
                if ev.source_id != source.id:
                    continue
                ct, _ = parse_unix(ev.reading, source.declared_utc_offset_s)
                if ct < model.t_span[0] or ct > model.t_span[1]:
                    extrap = True
                    break
        elif model.n == 0:
            extrap = True
        model.extrapolating = extrap
        models[source.id] = model

        intercept_q = Quantity(
            value=model.intercept,
            unit="s",
            source_ids=[source.id],
            anchor_ids=[a.id for a in by_source.get(source.id, [])],
            derived_by="timescale.ols_clock_fit" if model.n >= 2 else (
                "timescale.single_anchor_offset" if model.n == 1 else "timescale.uncalibrated_prior"
            ),
            detail=(
                f"参考历元 {format_iso(model.t_ref)} 处 clock-true 偏移；"
                f"基于 {model.n} 个锚点"
                + ("（OLS 线性拟合）" if model.n >= 2 else "")
            ),
        )
        drift_q = Quantity(
            value=(model.beta - 1.0) * 1e6,
            unit="ppm",
            source_ids=[source.id],
            anchor_ids=[a.id for a in by_source.get(source.id, [])],
            derived_by="timescale.ols_clock_fit" if model.n >= 2 else (
                "timescale.assumed_rate" if model.n < 2 else "timescale.ols_clock_fit"
            ),
            detail="clock 相对 true 的线性漂移率 (β-1)*1e6"
            + ("，锚点不足无法识别，按 0ppm" if model.n < 2 or model.sxx <= 1e-12 else ""),
        )
        reports[source.id] = ClockModel(
            source_id=source.id,
            offset_s=intercept_q,
            drift_ppm=drift_q,
            reference_epoch=format_iso(model.t_ref),
            anchor_count=model.n,
            fit_rms_residual_s=model.sigma_fit if model.n >= 2 else None,
            extrapolation=extrap,
            warnings=list(w),
        )

    return models, reports, warnings, errors


def convert_events(
    sources: list[SourceClock],
    events: list[Event],
    models: dict[str, FittedClock],
    default_offset_s: float,
) -> tuple[list[EventIntervalInput], list[str]]:
    """把事件读数换算为统一时间轴上的 [最早, 最晚] 区间。"""
    out: list[EventIntervalInput] = []
    warnings: list[str] = []
    src_by_id = {s.id: s for s in sources}

    for ev in events:
        if ev.source_id is None:
            t, aware = parse_unix(ev.reading, default_offset_s)
            half = ev.reading_uncertainty_s
            q = Quantity(
                value=half,
                unit="s",
                event_ids=[ev.id],
                derived_by="timescale.reference_timestamp",
                detail=(
                    "事件无关联时钟，读数直接作为统一参考时间"
                    + ("" if aware else f"（naive，按场景默认偏移 {default_offset_s:g}s 解释）")
                    + f"，半宽仅含读数不确定度 {half:g}s"
                ),
            )
            out.append(EventIntervalInput(event_id=ev.id, source_id=None, lo=t - half, hi=t + half, quantity=q))
            continue

        if ev.source_id not in src_by_id:
            raise ClockModelError(f"事件 {ev.id} 引用了不存在的来源 {ev.source_id}")
        source = src_by_id[ev.source_id]
        clock_t, aware = parse_unix(ev.reading, source.declared_utc_offset_s)
        model = models[ev.source_id]
        center, half = model.invert(clock_t, ev.reading_uncertainty_s)
        if not aware:
            warnings.append(
                f"事件 {ev.id}（来源 {ev.source_id}）读数无时区，"
                f"按来源声明偏移 {source.declared_utc_offset_s:g}s 解释"
            )
        method = (
            "timescale.invert_ols" if model.n >= 2
            else "timescale.invert_single_anchor" if model.n == 1
            else "timescale.invert_uncalibrated"
        )
        detail_parts = [
            f"钟面读数按来源 {ev.source_id} 模型反演（a={model.intercept:.3f}s, "
            f"β={model.beta:.9g}）",
            f"合成半宽 {half:.3f}s = 读数粒度 {ev.reading_uncertainty_s:g}s + 锚点/拟合项"
            f" + 基线 {model.base_uncertainty:g}s"
            + (f" + 漂移外推({model.drift_prior_ppm:g}ppm)" if model.extrapolating else ""),
        ]
        q = Quantity(
            value=half,
            unit="s",
            source_ids=[ev.source_id],
            anchor_ids=list(model.anchor_ids),
            event_ids=[ev.id],
            derived_by=method,
            detail="；".join(detail_parts),
        )
        out.append(
            EventIntervalInput(
                event_id=ev.id,
                source_id=ev.source_id,
                lo=center - half,
                hi=center + half,
                quantity=q,
            )
        )
    return out, warnings
