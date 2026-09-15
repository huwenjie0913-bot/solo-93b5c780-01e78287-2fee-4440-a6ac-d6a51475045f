"""IANA 时区歧义求解：zoneinfo + 随应用安装的 tzdata。

秋季夏令时回拨会让同一设备的本地（墙钟）时间出现两次，固定 UTC 偏移会把
取证记录放错一小时。本模块允许来源声明 IANA 时区名（``iana_timezone``），
把无偏移（naive）读数解析为**全部合法 UTC 候选**：

* 普通时刻：fold=0 与 fold=1 给出同一 UTC，去重后为 1 个候选；
* 秋季回拨重叠（如 America/New_York 11 月第一个周日的 01:00–02:00）：
  两个 fold 对应两个不同 UTC（偏移相差一小时），为 2 个候选；
* 春季跳时空洞（如 3 月第二个周日的 02:00–03:00）：两个 fold 的往返
  校验都失败，本地时间不存在，为 0 个候选（gap）。

候选的合法性用 PEP 495 往返校验判定：把 ``wall.replace(tzinfo=tz, fold=f)``
  换算到 UTC 再映射回本地，能回到原墙钟读数的才是合法候选。

歧义事件的 fold 组合接入现有差分约束与分支限界（与 ``associate`` /
``segments`` 同一套 Bellman-Ford 剪枝）：每个歧义事件的两个候选各给出一组
一元区间边，由事件约束剪枝选出可行的 fold 组合；搜索达到节点上限时给出
稳定的截断状态（``truncated`` / ``node_limit``）。

显式偏移读数始终优先、不进入候选展开；未声明时区的来源保持原有固定偏移
行为。所有解析只依赖 ``zoneinfo`` 与 ``tzdata``，与进程时区无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dcs import Edge, _bellman_ford, extract_cycle
from .models import (
    Contradiction,
    EventIntervalInput,
    Quantity,
    Scenario,
    TimezoneCandidate,
    TimezoneReport,
    TimezoneSearchStats,
    EventTimezoneResolution,
)
from .timescale import FittedClock, TimeParseError, format_iso

MAX_RECORDED_COMBINATIONS = 64  # 报告中记录的可行 fold 组合样本数上限（计数本身不受限）

_METHOD = (
    "timezone.fold_branch_and_bound：naive 读数按来源声明的 IANA 时区（zoneinfo/tzdata）"
    "展开为全部合法 UTC 候选（PEP 495 往返校验：回拨重叠 2 个、普通时刻 1 个、"
    "春季跳时空洞 0 个）；每个歧义事件的 fold 候选生成互斥的一元区间分支，"
    "用分支限界 + Bellman-Ford 差分约束剪枝搜索可行 fold 组合（fold=0 优先为"
    "代表解）；达到节点上限且仍有节点未探索时报告 truncated(node_limit)；"
    "显式偏移读数优先、不参与展开"
)


class TimezoneResolutionError(ValueError):
    """IANA 时区名无法加载等时区解析错误（映射为 HTTP 400）。"""


def load_zone(name: str) -> ZoneInfo:
    """加载 IANA 时区；名称非法时抛出 ``TimezoneResolutionError``。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimezoneResolutionError(f"无法加载 IANA 时区：{name!r}（{exc}）") from exc


def _parse_naive(wall_text: str) -> datetime:
    if not isinstance(wall_text, str) or not wall_text.strip():
        raise TimeParseError("时间字符串为空")
    try:
        dt = datetime.fromisoformat(wall_text.strip())
    except ValueError as exc:
        raise TimeParseError(f"无法解析 ISO 8601 时间：{wall_text!r}（{exc}）") from exc
    if dt.tzinfo is not None:
        raise TimeParseError(f"期望 naive 本地时间，实际带显式偏移：{wall_text!r}")
    return dt


def _offset_iso(offset_s: float) -> str:
    sign = "+" if offset_s >= 0 else "-"
    total = abs(int(round(offset_s)))
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def utc_candidates(wall_text: str, tz: ZoneInfo) -> list[TimezoneCandidate]:
    """naive 墙钟读数在时区 ``tz`` 下的全部合法 UTC 候选（按 UTC 升序）。

    往返校验：fold 解释换算到 UTC 后能映射回原墙钟读数才合法。普通时刻
    两个 fold 相同（去重为 1 个）；回拨重叠 2 个；跳时空洞 0 个。
    """
    dt = _parse_naive(wall_text)
    by_ts: dict[float, TimezoneCandidate] = {}
    for fold in (0, 1):
        aware = dt.replace(tzinfo=tz, fold=fold)
        ts = aware.timestamp()
        back = datetime.fromtimestamp(ts, tz)
        if back.replace(tzinfo=None) != dt:
            continue  # 跳时空洞：该 fold 解释映射到了别的本地时间
        if ts in by_ts:
            continue  # 普通时刻：两个 fold 给出同一 UTC
        off = aware.utcoffset().total_seconds()
        by_ts[ts] = TimezoneCandidate(
            unix_s=ts,
            iso=format_iso(ts),
            utc_offset_s=off,
            offset_iso=_offset_iso(off),
            fold=fold,
            tz_rule=aware.tzname() or tz.key,
        )
    return [by_ts[k] for k in sorted(by_ts)]


def classify_naive(wall_text: str, tz: ZoneInfo) -> Literal["unique", "ambiguous", "gap"]:
    """naive 读数在时区下的候选数分类。"""
    n = len(utc_candidates(wall_text, tz))
    if n >= 2:
        return "ambiguous"
    if n == 1:
        return "unique"
    return "gap"


def resolve_naive_deterministic(wall_text: str, tz_name: str) -> float:
    """naive 读数的确定性单值解析（锚点拟合/名义区间用）：fold=0 规则。

    重叠取首次出现（跳时前偏移），空洞按 PEP 495 fold=0 解释；与
    ``utc_candidates`` 的 fold=0 候选一致（空洞时无合法候选，仍给出确定值）。
    """
    tz = load_zone(tz_name)
    dt = _parse_naive(wall_text)
    return dt.replace(tzinfo=tz, fold=0).timestamp()


def naive_interpretation_note(
    wall_text: str, tz_name: str, *, nominal: bool = True
) -> str:
    """naive 读数在声明时区下解释方式的人可读说明（供警告/推导文本复用）。"""
    tz = load_zone(tz_name)
    kind = classify_naive(wall_text, tz)
    if kind == "ambiguous":
        cands = utc_candidates(wall_text, tz)
        desc = " / ".join(f"fold={c.fold}（{c.offset_iso} {c.tz_rule}，{c.iso}）" for c in cands)
        return (
            f"按声明时区 {tz_name} 解析：读数落在秋季回拨重叠内，有 {len(cands)} 个合法 "
            f"UTC 候选（{desc}）" + ("；此处确定性采用 fold=0" if nominal else "")
        )
    if kind == "gap":
        return (
            f"按声明时区 {tz_name} 解析：{describe_gap(wall_text, tz_name)}；"
            "该读数本不应出现，请复核记录" + ("；此处按 PEP 495 fold=0 规则解释" if nominal else "")
        )
    cand = utc_candidates(wall_text, tz)[0]
    return f"按声明时区 {tz_name} 解析（{cand.offset_iso} {cand.tz_rule}）"


def _find_transition(tz: ZoneInfo, lo_ts: float, hi_ts: float) -> float:
    """在 [lo_ts, hi_ts] 内二分 UTC 偏移跳变点（秒），返回跳变后的第一个时刻。"""
    off_lo = datetime.fromtimestamp(lo_ts, tz).utcoffset()
    lo, hi = lo_ts, hi_ts
    for _ in range(64):
        mid = (lo + hi) / 2
        if datetime.fromtimestamp(mid, tz).utcoffset() == off_lo:
            lo = mid
        else:
            hi = mid
    return hi


def describe_gap(wall_text: str, tz_name: str) -> str:
    """春季跳时空洞的人可读描述：被跳过的本地区间与两侧 UTC 偏移规则。"""
    tz = load_zone(tz_name)
    dt = _parse_naive(wall_text)
    pre = dt.replace(tzinfo=tz, fold=0)  # 跳时前偏移
    post = dt.replace(tzinfo=tz, fold=1)  # 跳时后偏移
    off_pre = pre.utcoffset()
    off_post = post.utcoffset()
    lo, hi = sorted([pre.timestamp(), post.timestamp()])
    trans = _find_transition(tz, lo, hi)
    gap_start = datetime.fromtimestamp(trans, timezone.utc) + off_pre
    gap_end = datetime.fromtimestamp(trans, timezone.utc) + off_post
    name_pre = (datetime.fromtimestamp(trans, timezone.utc) - timedelta(seconds=1)).astimezone(tz).tzname()
    name_post = datetime.fromtimestamp(trans, timezone.utc).astimezone(tz).tzname()
    return (
        f"本地时间 {dt.isoformat()} 在时区 {tz_name} 不存在：春季跳时把 "
        f"{gap_start.strftime('%Y-%m-%dT%H:%M:%S')}–{gap_end.strftime('%Y-%m-%dT%H:%M:%S')} "
        f"的本地区间整体跳过（UTC 偏移 {_offset_iso(off_pre.total_seconds())} {name_pre} → "
        f"{_offset_iso(off_post.total_seconds())} {name_post}）"
    )


# ----------------------------- fold 组合搜索 -----------------------------


@dataclass
class _FoldOption:
    candidate: TimezoneCandidate
    lo: float
    hi: float


@dataclass
class _FoldSlot:
    event_id: str
    options: list[_FoldOption]  # 按 UTC 升序（fold=0 在前）


def _invert_candidate(
    model: FittedClock, cand_unix_s: float, reading_uncertainty_s: float
) -> tuple[float, float, float]:
    """候选 UTC（即钟面读数在该偏移解释下的坐标）经时钟模型反演为真值区间。"""
    center, half = model.invert(cand_unix_s, reading_uncertainty_s)
    return center - half, center + half, half


def _candidate_interval(
    event_id: str,
    source_id: str,
    model: FittedClock,
    cand: TimezoneCandidate,
    tz_name: str,
    lo: float,
    hi: float,
    half: float,
    adopted_note: str,
) -> EventIntervalInput:
    q = Quantity(
        value=half,
        unit="s",
        source_ids=[source_id],
        anchor_ids=list(model.anchor_ids),
        event_ids=[event_id],
        derived_by="timezone.fold_resolution",
        detail=(
            f"naive 读数在 IANA 时区 {tz_name} 下按 fold={cand.fold}"
            f"（{cand.offset_iso} {cand.tz_rule}）解析为 {cand.iso}，"
            f"再按来源 {source_id} 时钟模型反演；合成半宽 {half:.3f}s；{adopted_note}"
        ),
    )
    return EventIntervalInput(event_id=event_id, source_id=source_id, lo=lo, hi=hi, quantity=q)


def run_timezone_resolution(
    scenario: Scenario,
    models: dict[str, FittedClock],
    legacy_intervals: list[EventIntervalInput],
    max_search_nodes: int = 10_000,
) -> tuple[TimezoneReport, Optional[list[EventIntervalInput]], list[str]]:
    """执行 IANA 时区歧义求解。

    返回 (报告, 代表 fold 组合下的事件区间（无可行组合为 None）, 警告)。
    调用方保证至少一个来源声明了 ``iana_timezone``。
    """
    from .engine import _constraint_edges, _contradiction  # 延迟导入，避免循环依赖

    warnings: list[str] = []
    tz_sources = {s.id: s for s in scenario.sources if s.iana_timezone}
    zones = {sid: load_zone(s.iana_timezone) for sid, s in tz_sources.items()}
    for s in tz_sources.values():
        if abs(s.declared_utc_offset_s) > 1e-9:
            warnings.append(
                f"来源 {s.id} 同时声明 declared_utc_offset_s={s.declared_utc_offset_s:g}s "
                f"与 iana_timezone={s.iana_timezone}：naive 读数按时区规则解析，固定偏移被忽略"
            )

    resolutions: list[EventTimezoneResolution] = []
    gap_event_ids: list[str] = []
    ambiguous_event_ids: list[str] = []
    slots: list[_FoldSlot] = []
    # 唯一候选/显式事件的区间覆盖（键为事件 ID）；歧义事件的区间在搜索后确定
    override: dict[str, EventIntervalInput] = {}
    slot_options: dict[str, list[tuple[_FoldOption, EventIntervalInput]]] = {}

    for ev in scenario.events:
        sid = ev.source_id
        if sid is None or sid not in tz_sources:
            continue
        source = tz_sources[sid]
        tz_name = source.iana_timezone
        zone = zones[sid]
        model = models[sid]
        text = ev.reading.strip()
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            continue  # 非法读数已在时钟换算阶段报错
        if dt.tzinfo is not None:
            # 显式偏移优先：不进入候选展开
            ts = dt.timestamp()
            off = dt.utcoffset().total_seconds()
            cand = TimezoneCandidate(
                unix_s=ts,
                iso=format_iso(ts),
                utc_offset_s=off,
                offset_iso=_offset_iso(off),
                fold=None,
                tz_rule=dt.tzname() or "explicit",
            )
            resolutions.append(
                EventTimezoneResolution(
                    event_id=ev.id,
                    source_id=sid,
                    reading=ev.reading,
                    iana_timezone=tz_name,
                    status="explicit_offset",
                    candidates=[cand],
                    adopted_unix_s=ts,
                    adopted_utc_offset_s=off,
                    adopted_fold=None,
                    basis=(
                        f"读数自带显式偏移 {_offset_iso(off)}，显式偏移优先于时区声明，"
                        "不参与 fold 候选展开"
                    ),
                )
            )
            continue

        cands = utc_candidates(text, zone)
        if not cands:
            gap_desc = describe_gap(text, tz_name)
            gap_event_ids.append(ev.id)
            warnings.append(f"事件 {ev.id}（来源 {sid}）读数无时区，{gap_desc}")
            resolutions.append(
                EventTimezoneResolution(
                    event_id=ev.id,
                    source_id=sid,
                    reading=ev.reading,
                    iana_timezone=tz_name,
                    status="gap",
                    candidates=[],
                    adopted_unix_s=None,
                    adopted_utc_offset_s=None,
                    adopted_fold=None,
                    basis=gap_desc + "；该本地时间不存在，无法映射到 UTC",
                )
            )
            continue
        if len(cands) == 1:
            cand = cands[0]
            lo, hi, half = _invert_candidate(model, cand.unix_s, ev.reading_uncertainty_s)
            override[ev.id] = _candidate_interval(
                ev.id, sid, model, cand, tz_name, lo, hi, half, "唯一合法候选"
            )
            resolutions.append(
                EventTimezoneResolution(
                    event_id=ev.id,
                    source_id=sid,
                    reading=ev.reading,
                    iana_timezone=tz_name,
                    status="unique",
                    candidates=[cand],
                    adopted_unix_s=cand.unix_s,
                    adopted_utc_offset_s=cand.utc_offset_s,
                    adopted_fold=cand.fold,
                    basis=(
                        f"读数在时区 {tz_name} 下唯一对应 {cand.iso}"
                        f"（{cand.offset_iso} {cand.tz_rule}），无回拨歧义"
                    ),
                )
            )
            continue

        # 回拨重叠：两个合法候选，进入 fold 组合搜索
        ambiguous_event_ids.append(ev.id)
        warnings.append(
            f"事件 {ev.id}（来源 {sid}）读数无时区，在声明时区 {tz_name} 的"
            f"秋季回拨重叠内有两个合法 UTC 候选，将由事件约束选取 fold"
        )
        options: list[_FoldOption] = []
        built: list[tuple[_FoldOption, EventIntervalInput]] = []
        for cand in cands:
            lo, hi, half = _invert_candidate(model, cand.unix_s, ev.reading_uncertainty_s)
            opt = _FoldOption(candidate=cand, lo=lo, hi=hi)
            options.append(opt)
            built.append(
                (
                    opt,
                    _candidate_interval(
                        ev.id, sid, model, cand, tz_name, lo, hi, half,
                        "回拨重叠候选，是否采用见 fold_assignments",
                    ),
                )
            )
        slots.append(_FoldSlot(event_id=ev.id, options=options))
        slot_options[ev.id] = built
        resolutions.append(
            EventTimezoneResolution(
                event_id=ev.id,
                source_id=sid,
                reading=ev.reading,
                iana_timezone=tz_name,
                status="ambiguous",
                candidates=cands,
                adopted_unix_s=None,
                adopted_utc_offset_s=None,
                adopted_fold=None,
                basis=(
                    f"读数落在时区 {tz_name} 的秋季回拨重叠内，{len(cands)} 个合法候选"
                    "待事件约束选取"
                ),
            )
        )

    res_by_id = {r.event_id: r for r in resolutions}
    tz_names = sorted({s.iana_timezone for s in tz_sources.values()})
    stats = TimezoneSearchStats(
        ambiguous_events=len(slots),
        combinations_total=2 ** len(slots),
        nodes_expanded=0,
        branches_pruned=0,
        feasible_combinations=0,
        max_search_nodes=max_search_nodes,
        truncated=False,
        truncation_reason=None,
    )

    def _gap_contradiction() -> Contradiction:
        gap_sources = sorted({res_by_id[e].source_id for e in gap_event_ids if res_by_id[e].source_id})
        gap_zones = sorted({res_by_id[e].iana_timezone for e in gap_event_ids})
        return Contradiction(
            cycle_event_ids=[],
            cycle_constraint_ids=[],
            total_slack_s=0.0,
            related_record_ids={
                "events": sorted(gap_event_ids),
                "constraints": [],
                "sources": gap_sources,
                "anchors": [],
                "timezones": gap_zones,
            },
            explanation=(
                "以下事件的本地读数在声明时区中不存在（春季跳时空洞），无法映射到任何 "
                "UTC 时刻：" + "；".join(res_by_id[e].basis for e in sorted(gap_event_ids))
            ),
        )

    # 存在空洞事件：任何 fold 组合都无法安置它们，直接判不可行
    if gap_event_ids:
        for r in resolutions:
            if r.status == "ambiguous":
                r.basis += "；因存在空洞事件，未执行 fold 组合搜索"
        report = TimezoneReport(
            status="infeasible",
            timezone_sources=sorted(tz_sources),
            resolutions=resolutions,
            ambiguous_events=sorted(ambiguous_event_ids),
            gap_events=sorted(gap_event_ids),
            fold_assignments={},
            stats=stats,
            contradiction=_gap_contradiction(),
            method=_METHOD,
        )
        return report, None, warnings

    # 名义区间：唯一候选/显式事件用覆盖区间，歧义事件先用 fold=0（仅供矛盾叙述）
    nominal = [override.get(iv.event_id, iv) for iv in legacy_intervals]

    if not slots:
        report = TimezoneReport(
            status="ok",
            timezone_sources=sorted(tz_sources),
            resolutions=resolutions,
            ambiguous_events=[],
            gap_events=[],
            fold_assignments={},
            stats=stats,
            contradiction=None,
            method=_METHOD,
        )
        return report, nominal, warnings

    # ---------------- fold 组合分支限界 ----------------
    slots.sort(key=lambda s: s.event_id)  # 全部为 2 选项，按事件 ID 稳定排序
    idx = {ev.id: i + 1 for i, ev in enumerate(scenario.events)}
    n = len(scenario.events) + 1
    slot_ids = {s.event_id for s in slots}
    base_edges: list[Edge] = []
    for iv in nominal:
        if iv.event_id in slot_ids:
            continue
        node = idx[iv.event_id]
        base_edges.append(Edge(0, node, iv.hi, iv.event_id, "upper"))
        base_edges.append(Edge(node, 0, -iv.lo, iv.event_id, "lower"))
    for c in scenario.constraints:
        base_edges.extend(_constraint_edges(c, idx))

    state = {"nodes": 0, "pruned": 0, "truncated": False}
    chosen: list[Optional[int]] = [None] * len(slots)
    feasible_combos: list[dict[str, int]] = []  # 记录样本（供选择依据叙述）
    feasible_total = [0]
    representative: dict[str, object] = {"combo": None, "edges": None}
    rep_contra: dict[str, object] = {"depth": -1, "contra": None}

    def record_prune(level: int, slot: _FoldSlot, opt: _FoldOption, pred, pred_edge, bad) -> None:
        state["pruned"] += 1
        cycle = extract_cycle(n, pred, pred_edge, bad)
        contra = _contradiction(cycle, idx, nominal, list(scenario.constraints))
        # 该路径上已选 fold + 当前被拒 fold 的候选描述
        path: list[str] = []
        for li, oi in enumerate(chosen):
            if oi is None:
                continue
            c = slots[li].options[oi].candidate
            path.append(f"{slots[li].event_id}=fold{c.fold}({c.offset_iso} {c.tz_rule})")
        c = opt.candidate
        path.append(f"{slot.event_id}=fold{c.fold}({c.offset_iso} {c.tz_rule}) 被拒")
        contra.related_record_ids["timezones"] = tz_names
        contra.explanation += (
            f"；时区歧义求解：fold 组合路径 {'，'.join(path)} 被差分约束剪枝"
            f"（适用时区 {', '.join(tz_names)}）"
        )
        if level > rep_contra["depth"]:
            rep_contra["depth"] = level
            rep_contra["contra"] = contra

    def dfs(level: int, edges: list[Edge]) -> None:
        if state["truncated"]:
            return
        if state["nodes"] >= max_search_nodes:
            state["truncated"] = True
            return
        state["nodes"] += 1
        if level == len(slots):
            feasible_total[0] += 1
            combo = {
                slots[li].event_id: slots[li].options[oi].candidate.fold
                for li, oi in enumerate(chosen)
                if oi is not None
            }
            if len(feasible_combos) < MAX_RECORDED_COMBINATIONS:
                feasible_combos.append(combo)
            if representative["combo"] is None:  # fold=0 优先序下的首个可行组合即代表解
                representative["combo"] = combo
                representative["edges"] = list(edges)
            return
        slot = slots[level]
        node = idx[slot.event_id]
        for oi, opt in enumerate(slot.options):
            if state["truncated"]:
                return
            new_edges = edges + [
                Edge(0, node, opt.hi, slot.event_id, "upper"),
                Edge(node, 0, -opt.lo, slot.event_id, "lower"),
            ]
            _d, p, pe, bad = _bellman_ford(n, new_edges, start=None)
            if bad is not None:
                record_prune(level, slot, opt, p, pe, bad)
                continue
            chosen[level] = oi
            dfs(level + 1, new_edges)
            chosen[level] = None

    dfs(0, base_edges)

    stats.nodes_expanded = state["nodes"]
    stats.branches_pruned = state["pruned"]
    stats.feasible_combinations = feasible_total[0]
    stats.truncated = state["truncated"]
    if state["truncated"]:
        stats.truncation_reason = "node_limit"

    rep_combo = representative["combo"]
    if rep_combo is None:
        # 无可行组合：截断（上限内未穷尽）或全部组合被约束排除
        contra = rep_contra["contra"]
        if contra is None and state["truncated"]:
            contra = Contradiction(
                cycle_event_ids=[],
                cycle_constraint_ids=[],
                total_slack_s=0.0,
                related_record_ids={
                    "events": sorted(slot_ids),
                    "constraints": [],
                    "sources": sorted({res_by_id[e].source_id for e in slot_ids if res_by_id[e].source_id}),
                    "anchors": [],
                    "timezones": tz_names,
                },
                explanation=(
                    f"fold 组合搜索在节点上限 {max_search_nodes} 处截断，"
                    "已探索范围内未发现可行组合，亦未定位到具体矛盾链；"
                    "提高 tz_max_search_nodes 可继续搜索"
                ),
            )
        report = TimezoneReport(
            status="truncated" if state["truncated"] else "infeasible",
            timezone_sources=sorted(tz_sources),
            resolutions=resolutions,
            ambiguous_events=sorted(ambiguous_event_ids),
            gap_events=[],
            fold_assignments={},
            stats=stats,
            contradiction=contra,  # type: ignore[arg-type]
            method=_METHOD,
        )
        return report, None, warnings

    # 代表解：填充逐事件采用结论与最终区间
    fold_assignments = {eid: int(f) for eid, f in rep_combo.items()}  # type: ignore[union-attr]
    enum_note = (
        f"共 {feasible_total[0]} 个可行 fold 组合"
        + ("（枚举达节点上限，为已探索范围内计数）" if state["truncated"] else "")
        + "，代表解按 fold=0 优先选取"
        if feasible_total[0] > 1
        else "事件约束下唯一可行的 fold 组合"
    )
    final = dict(override)
    for slot in slots:
        fold = fold_assignments[slot.event_id]
        res = res_by_id[slot.event_id]
        cand = next(c for c in res.candidates if c.fold == fold)
        other = next(c for c in res.candidates if c.fold != fold)
        other_feasible = any(
            combo.get(slot.event_id) == other.fold for combo in feasible_combos
        )
        if other_feasible:
            alt_note = f"另一候选 fold={other.fold}（{other.offset_iso} {other.tz_rule}）在已探索范围内亦可行，未采用"
        elif state["truncated"]:
            alt_note = f"另一候选 fold={other.fold}（{other.offset_iso} {other.tz_rule}）在已探索范围内不可行（枚举截断）"
        else:
            alt_note = f"另一候选 fold={other.fold}（{other.offset_iso} {other.tz_rule}）被差分约束排除"
        res.adopted_unix_s = cand.unix_s
        res.adopted_utc_offset_s = cand.utc_offset_s
        res.adopted_fold = cand.fold
        res.basis = (
            f"回拨重叠：读数在时区 {res.iana_timezone} 下有 {len(res.candidates)} 个合法 "
            f"UTC 候选；采用 fold={cand.fold}（{cand.offset_iso} {cand.tz_rule}，{cand.iso}）；"
            f"{alt_note}；{enum_note}"
        )
        for opt, iv in slot_options[slot.event_id]:
            if opt.candidate.fold == fold:
                final[slot.event_id] = iv
                break

    final_intervals = [final.get(iv.event_id, iv) for iv in legacy_intervals]
    report = TimezoneReport(
        status="truncated" if state["truncated"] else "ok",
        timezone_sources=sorted(tz_sources),
        resolutions=resolutions,
        ambiguous_events=sorted(ambiguous_event_ids),
        gap_events=[],
        fold_assignments=fold_assignments,
        stats=stats,
        contradiction=None,
        method=_METHOD,
    )
    return report, final_intervals, warnings
