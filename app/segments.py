"""分段时钟模型：重启 / 人工校时 / 断电后的时钟跳变不再被摊进单条漂移直线。

每台设备的时钟在其生命周期内可能经历多次跳变。本模块在单来源内部按时钟段
（segment）切分：

* 段边界可由用户**显式声明**（带生效钟面时刻与边界不确定半宽），也可根据
  **连续锚点残差**对单线性拟合的跳变（``|Δ残差| ≥ 阈值``）自动检测；
* 自动检测到的边界是可选的：枚举其采纳子集，与显式边界组合成候选分段方案，
  方案在 ``max_segment_schemes`` 上限内按（可行性, 拟合残差, 段数, 模糊成本）
  稳定排序；
* 每个方案内各段独立 OLS 拟合偏移与漂移；事件按**钟面读数**归段；钟面读数
  落在边界 ± 不确定半宽内的事件保留多个可行归属，用分支限界 + Bellman-Ford
  差分约束剪枝搜索可行归段解，与候选关联求解同一套图算法；
* 段间跳变量按“同一真实时刻下，后段钟面相对前段外推钟面之差”计算；
* 方案全部不可行时，代表性矛盾链会标注涉及的时钟段与锚点。

未声明分段规则的来源仍由 ``timescale`` 的单线性模型处理，行为不变。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Optional

from .dcs import Edge, _bellman_ford, extract_cycle
from .models import (
    CalibrationAnchor,
    ClockSegment,
    Contradiction,
    DetectedJumpCandidate,
    Event,
    EventIntervalInput,
    EventSegmentAssignment,
    Quantity,
    Scenario,
    SegmentClockModel,
    SegmentJump,
    SegmentationReport,
    SegmentationScheme,
    SourceClock,
    SourceSegmentation,
)
from .timescale import (
    ClockModelError,
    FittedClock,
    TimeParseError,
    _fit_anchors,
    format_iso,
    invert_event_interval,
    parse_unix,
)

MAX_ASSIGNMENT_NODES = 10_000  # 单方案跨段归属分支搜索的节点上限
MAX_AUTO_BOUNDARIES_PER_SOURCE = 6  # 单个来源参与枚举的自动边界数上限（2^6=64 选项）

_METHOD = (
    "segments.piecewise_clock：显式边界与连续锚点残差自动检测（|Δ残差|≥阈值）"
    "组合枚举候选分段方案；每段独立 OLS 拟合偏移与漂移，事件按钟面读数归段，"
    "边界 ± 不确定半宽内的事件保留多个可行归属并做分支限界 + Bellman-Ford "
    "差分约束剪枝；方案按（可行性, 锚点残差 RMS, 段数, 跨段归属成本）排序"
)


# ----------------------------- 内部数据结构 -----------------------------


@dataclass
class _AnchorRow:
    anchor: CalibrationAnchor
    true_t: float
    clock_t: float
    ref_unc: float
    aware_clock: bool


@dataclass
class _EventRow:
    event: Event
    clock_t: float
    aware: bool


@dataclass
class _Boundary:
    seg_id: str  # 来源内段 ID（跳变后段）
    clock_t: float
    clock_reading: str  # 原始 ISO 字符串，用于报告
    origin: str  # "declared" / "auto"
    uncertainty_s: float
    residual_jump_s: Optional[float] = None  # 自动检测：单线性残差跳变
    estimated_jump_s: Optional[float] = None  # 自动检测：前缀外推跳变估计


@dataclass
class _SegFit:
    seg_id: str
    boundary: Optional[_Boundary]  # 初始段为 None
    model: FittedClock
    anchor_rows: list[_AnchorRow]
    carried: bool  # 段内无锚点、沿用前段模型
    warnings: list[str] = field(default_factory=list)


@dataclass
class _SourcePlan:
    rule: SourceSegmentation
    source: SourceClock
    initial_id: str
    earliest_clock: float  # 该源锚点/事件最早钟面时刻，用于初始段显示
    anchors: list[_AnchorRow]
    events: list[_EventRow]
    declared: list[_Boundary]
    detected: list[DetectedJumpCandidate]
    detected_boundaries: list[_Boundary]
    options: list[list[_Boundary]] = field(default_factory=list)  # 每选项 = 采纳的自动边界


@dataclass
class _AssignmentOption:
    seg_gid: str
    edges: list[Edge]
    cost: float


@dataclass
class _Slot:
    event_id: str
    nominal_gid: str
    options: list[_AssignmentOption]


@dataclass
class _SchemeEval:
    key: str
    feasible: bool
    branches: int
    truncated: bool
    ambiguity_cost: float
    chosen_assignment: dict[str, str]
    intervals: list[EventIntervalInput]  # 代表归段解下每事件的一元区间
    nominal_intervals: list[EventIntervalInput]  # 名义归段（未做跨段分支）区间
    fit_score: float
    total_abs_jump: float
    n_boundaries: int
    anchor_rms: dict[str, float]
    segment_fits: dict[str, list[_SegFit]]
    ordered_boundaries: dict[str, list[_Boundary]]
    jumps: list[SegmentJump]
    assignments: list[EventSegmentAssignment]
    contradiction: Optional[Contradiction]
    detail: str


# ----------------------------- 预处理与自动检测 -----------------------------


def _residual(model: FittedClock, true_t: float, clock_t: float) -> float:
    return (clock_t - model.t_ref) - (
        model.intercept + model.beta * (true_t - model.t_ref)
    )


def _prepare_source(
    rule: SourceSegmentation,
    source: SourceClock,
    anchors: list[CalibrationAnchor],
    events: list[Event],
    default_offset_s: float,
    scenario_threshold: Optional[float],
) -> tuple[_SourcePlan, list[str]]:
    warnings: list[str] = []
    rows: list[_AnchorRow] = []
    for anc in anchors:
        clock_t, aware_c = parse_unix(anc.clock_reading, source.declared_utc_offset_s)
        true_t, _aware_r = parse_unix(anc.reference_time, default_offset_s)
        rows.append(_AnchorRow(anc, true_t, clock_t, anc.reference_uncertainty_s, aware_c))
        if not aware_c:
            warnings.append(
                f"锚点 {anc.id}（来源 {source.id}）钟面读数无时区，"
                f"按来源声明偏移 {source.declared_utc_offset_s:g}s 解释"
            )
    rows.sort(key=lambda r: r.true_t)

    ev_rows: list[_EventRow] = []
    for ev in events:
        clock_t, aware = parse_unix(ev.reading, source.declared_utc_offset_s)
        ev_rows.append(_EventRow(ev, clock_t, aware))
        if not aware:
            warnings.append(
                f"事件 {ev.id}（来源 {source.id}）读数无时区，"
                f"按来源声明偏移 {source.declared_utc_offset_s:g}s 解释"
            )

    # 显式边界
    seen_ids: set[str] = set()
    declared: list[_Boundary] = []
    for seg in rule.segments:
        if seg.id in seen_ids:
            raise ClockModelError(f"来源 {source.id} 的时钟段 ID 重复：{seg.id}")
        seen_ids.add(seg.id)
        clock_t, _ = parse_unix(seg.start_clock_reading, source.declared_utc_offset_s)
        declared.append(
            _Boundary(
                seg_id=seg.id,
                clock_t=clock_t,
                clock_reading=seg.start_clock_reading,
                origin="declared",
                uncertainty_s=seg.boundary_uncertainty_s,
            )
        )
    declared.sort(key=lambda b: b.clock_t)
    if len(declared) >= 2:
        for b0, b1 in zip(declared, declared[1:]):
            if b1.clock_t <= b0.clock_t:
                raise ClockModelError(
                    f"来源 {source.id} 的时钟段生效时刻必须严格递增："
                    f"{b0.seg_id}({format_iso(b0.clock_t)}) 不晚于 "
                    f"{b1.seg_id}({format_iso(b1.clock_t)})"
                )
    initial_id = declared[0].seg_id if declared else "seg-initial"
    earliest_clock = min(
        [r.clock_t for r in rows] + [e.clock_t for e in ev_rows],
        default=0.0,
    )

    # 自动检测
    threshold = rule.jump_threshold_s
    if threshold is None:
        threshold = scenario_threshold
    detected: list[DetectedJumpCandidate] = []
    detected_boundaries: list[_Boundary] = []
    if threshold is not None and len(rows) >= 2:
        full_model, _fw, _ = _fit_anchors(source, [r.anchor for r in rows], default_offset_s)
        auto_idx = 0
        for i in range(len(rows) - 1):
            r0, r1 = rows[i], rows[i + 1]
            d = _residual(full_model, r1.true_t, r1.clock_t) - _residual(
                full_model, r0.true_t, r0.clock_t
            )
            if abs(d) + 1e-9 < threshold:
                continue
            auto_idx += 1
            # 用“截至 r0 的前缀线性模型”外推 r1 真实时刻处应有的钟面，估计跳变量
            jump_est: Optional[float] = None
            prefix, _pw, _ = _fit_anchors(
                source, [r.anchor for r in rows[: i + 1]], default_offset_s
            )
            if prefix.beta > 1e-9:
                pred_clock = prefix.t_ref + prefix.intercept + prefix.beta * (
                    r1.true_t - prefix.t_ref
                )
                jump_est = r1.clock_t - pred_clock
            unc = max(r0.ref_unc, r1.ref_unc) + rule.auto_boundary_uncertainty_s
            seg_id = f"seg-auto-{auto_idx}"
            cand = DetectedJumpCandidate(
                source_id=source.id,
                boundary_segment_id=seg_id,
                between_anchor_ids=(r0.anchor.id, r1.anchor.id),
                residual_jump_s=d,
                threshold_s=threshold,
                suggested_clock_reading=r1.anchor.clock_reading,
                estimated_jump_s=jump_est,
                boundary_uncertainty_s=unc,
                detail=(
                    f"锚点 {r0.anchor.id}→{r1.anchor.id} 对单线性拟合的残差跳变 "
                    f"{d:.3f}s，|Δ残差|≥阈值 {threshold:g}s；建议自钟面 "
                    f"{r1.anchor.clock_reading} 起新开段 {seg_id}"
                    + (f"，前缀外推估计跳变 {jump_est:.3f}s" if jump_est is not None else "")
                ),
            )
            detected.append(cand)
            detected_boundaries.append(
                _Boundary(
                    seg_id=seg_id,
                    clock_t=r1.clock_t,
                    clock_reading=r1.anchor.clock_reading,
                    origin="auto",
                    uncertainty_s=unc,
                    residual_jump_s=abs(d),
                    estimated_jump_s=jump_est,
                )
            )
        if not detected:
            warnings.append(
                f"来源 {source.id} 给定自动检测阈值 {threshold:g}s，"
                f"但 {len(rows)} 个连续锚点对的残差跳变均未达到阈值，未生成跳变候选"
            )
    elif threshold is not None and len(rows) < 2:
        warnings.append(
            f"来源 {source.id} 锚点不足 2 个，无法做连续锚点残差跳变检测"
        )

    if not declared and threshold is None:
        raise ClockModelError(
            f"来源 {source.id} 的分段规则既未显式声明时钟段，也无有效的"
            "跳变检测阈值（来源 jump_threshold_s 与场景 auto_jump_threshold_s 均缺省）"
        )

    # 自动边界采纳选项：全部子集（上限内），按“包含更大跳变优先、段数更少优先”排序
    options: list[list[_Boundary]] = [[]]
    if detected_boundaries:
        chosen = detected_boundaries[:MAX_AUTO_BOUNDARIES_PER_SOURCE]
        if len(detected_boundaries) > MAX_AUTO_BOUNDARIES_PER_SOURCE:
            warnings.append(
                f"来源 {source.id} 检测到 {len(detected_boundaries)} 个跳变候选，"
                f"仅残差跳变最大的 {MAX_AUTO_BOUNDARIES_PER_SOURCE} 个参与方案枚举"
            )
            chosen = sorted(
                detected_boundaries,
                key=lambda b: -(b.residual_jump_s or 0.0),
            )[:MAX_AUTO_BOUNDARIES_PER_SOURCE]
        subsets: list[list[_Boundary]] = [[]]
        for b in chosen:
            subsets += [sub + [b] for sub in subsets]
        subsets.sort(key=lambda sub: (-sum(b.residual_jump_s or 0.0 for b in sub), len(sub)))
        options = subsets

    return (
        _SourcePlan(
            rule=rule,
            source=source,
            initial_id=initial_id,
            earliest_clock=earliest_clock,
            anchors=rows,
            events=ev_rows,
            declared=declared,
            detected=detected,
            detected_boundaries=detected_boundaries,
            options=options,
        ),
        warnings,
    )


def _enumerate_choices(
    options_by_source: list[list[list[_Boundary]]], cap: int
) -> tuple[list[tuple[int, ...]], bool]:
    """按“各源选项偏好秩之和最小优先”做堆式 k-way 合并枚举，返回索引组合。"""
    dims = [len(o) for o in options_by_source]
    if not dims:
        return [], False
    start = tuple(0 for _ in dims)
    heap: list[tuple[tuple[int, ...], tuple[int, ...]]] = [
        (tuple(0 for _ in dims), start)
    ]
    seen = {start}
    out: list[tuple[int, ...]] = []
    truncated = False
    while heap and len(out) < cap:
        _key, cur = heapq.heappop(heap)
        out.append(cur)
        for k in range(len(dims)):
            if cur[k] + 1 >= dims[k]:
                continue
            nxt = list(cur)
            nxt[k] += 1
            nxt_t = tuple(nxt)
            if nxt_t in seen:
                continue
            seen.add(nxt_t)
            heapq.heappush(heap, (nxt_t, nxt_t))
    if heap:
        truncated = True
    return out, truncated


# ----------------------------- 分段拟合与跳变 -----------------------------


def _fit_scheme_source(plan: _SourcePlan, chosen_auto: list[_Boundary]) -> tuple[
    list[_SegFit], list[_Boundary], list[str]
]:
    """拟合一个方案下某来源的全部段，返回 (段拟合, 有序边界, 警告)。"""
    warnings: list[str] = []
    boundaries = sorted(plan.declared + chosen_auto, key=lambda b: b.clock_t)
    fits: list[_SegFit] = []
    prev_fit: Optional[FittedClock] = None

    for k, boundary in enumerate([None] + boundaries):
        if boundary is None:
            seg_id = plan.initial_id
            seg_anchors = [r for r in plan.anchors if (not boundaries or r.clock_t < boundaries[0].clock_t)]
        else:
            seg_id = boundary.seg_id
            lo_t = boundary.clock_t
            later = [b for b in boundaries if b.clock_t > lo_t]
            hi_t = later[0].clock_t if later else float("inf")
            seg_anchors = [r for r in plan.anchors if lo_t <= r.clock_t < hi_t]

        carried = False
        if seg_anchors:
            model, sw, _ = _fit_anchors(
                plan.source, [r.anchor for r in seg_anchors], 0.0
            )
            warnings.extend(sw)
            if model.beta <= 1e-9:
                raise ClockModelError(
                    f"来源 {plan.source.id} 段 {seg_id} 拟合速率 β={model.beta:g} 非正，"
                    "时钟模型不可逆"
                )
        else:
            carried = True
            if prev_fit is None:
                model, sw, _ = _fit_anchors(plan.source, [], 0.0)
                warnings.extend(sw)
                # 无锚点也无前段：以该源首个事件钟面为历元，与单线性无锚点路径一致
                if plan.events:
                    model.t_ref = min(e.clock_t for e in plan.events)
            else:
                model = prev_fit
            warnings.append(
                f"来源 {plan.source.id} 段 {seg_id} 内无校时锚点，"
                + ("沿用前段偏移与漂移" if prev_fit is not None else "按先验（偏移 0、速率 1）处理")
            )
        fit = _SegFit(
            seg_id=seg_id,
            boundary=boundary,
            model=model,
            anchor_rows=seg_anchors,
            carried=carried,
        )
        fits.append(fit)
        prev_fit = model

    # 锚点覆盖范围外推标记（沿用单线性模型约定：钟面读数与锚点真实时刻跨度比较）
    for idx, fit in enumerate(fits):
        m = fit.model
        lo_b = boundaries[idx - 1].clock_t if idx >= 1 else -float("inf")
        hi_b = boundaries[idx].clock_t if idx < len(boundaries) else float("inf")
        if m.t_span is not None:
            for e in plan.events:
                if lo_b <= e.clock_t < hi_b and (
                    e.clock_t < m.t_span[0] or e.clock_t > m.t_span[1]
                ):
                    m.extrapolating = True
                    break
        elif fit.carried and m.n == 0:
            m.extrapolating = True
    return fits, boundaries, warnings


def _jump_at_boundary(
    boundary: _Boundary, prev: _SegFit, cur: _SegFit
) -> Optional[float]:
    """边界处跳变量：同一真实时刻下后段钟面与前段外推钟面之差（秒）。"""
    if prev.carried or cur.carried or prev.model.n == 0 or cur.model.n == 0:
        return None
    mp, mq = prev.model, cur.model
    c_b = boundary.clock_t
    t_q = mq.t_ref + (c_b - mq.t_ref - mq.intercept) / mq.beta
    c_p = mp.t_ref + mp.intercept + mp.beta * (t_q - mp.t_ref)
    return c_b - c_p


# ----------------------------- 归段与跨段归属搜索 -----------------------------


def _segment_index_at(boundaries: list[_Boundary], clock_t: float) -> int:
    """钟面读数对应的段下标（0 为初始段）。"""
    idx = 0
    for k, b in enumerate(boundaries, start=1):
        if clock_t >= b.clock_t:
            idx = k
        else:
            break
    return idx


def _build_interval(
    plan: _SourcePlan,
    fit: _SegFit,
    er: _EventRow,
    seg_gid: str,
    derived_method: str,
) -> EventIntervalInput:
    lo, hi, half, method, _warn = invert_event_interval(er.event, plan.source, fit.model)
    q = Quantity(
        value=half,
        unit="s",
        source_ids=[plan.source.id],
        anchor_ids=[r.anchor.id for r in fit.anchor_rows],
        segment_ids=[seg_gid],
        event_ids=[er.event.id],
        derived_by="segments." + derived_method,
        detail=(
            f"钟面读数按来源 {plan.source.id} 段 {fit.seg_id} 模型反演"
            f"（a={fit.model.intercept:.3f}s, β={fit.model.beta:.9g}，"
            f"段内锚点 {len(fit.anchor_rows)} 个）；合成半宽 {half:.3f}s"
        ),
    )
    return EventIntervalInput(
        event_id=er.event.id, source_id=plan.source.id, lo=lo, hi=hi, quantity=q
    )


def _search_assignment(
    scenario: Scenario,
    plans: list[_SourcePlan],
    fits_by_source: dict[str, list[_SegFit]],
    boundaries_by_source: dict[str, list[_Boundary]],
    fixed_edges: list[Edge],
    intervals_order: list[EventIntervalInput],
    idx: dict[str, int],
    constraints,
) -> tuple[
    bool, int, bool, float, dict[str, str], list[EventIntervalInput], Optional[Contradiction]
]:
    """对边界不确定区内的事件做多归属分支限界，返回代表（成本最小）可行解。"""
    n = len(intervals_order) + 1
    slots: list[_Slot] = []
    # 代表区间：初始先填名义段，最终按选中解替换
    rep_interval: dict[str, EventIntervalInput] = {iv.event_id: iv for iv in intervals_order}

    for plan in plans:
        fits = fits_by_source[plan.source.id]
        boundaries = boundaries_by_source[plan.source.id]
        fit_by_id = {f.seg_id: f for f in fits}
        for er in plan.events:
            nom_k = _segment_index_at(boundaries, er.clock_t)
            feasible_idx = {nom_k}
            for k, b in enumerate(boundaries, start=1):
                if abs(er.clock_t - b.clock_t) <= b.uncertainty_s + 1e-9:
                    feasible_idx.add(k)
                    feasible_idx.add(k - 1)
            nom_fit = fits[nom_k]
            options: list[_AssignmentOption] = []
            for k in sorted(feasible_idx):
                fit = fits[k]
                gid = f"{plan.source.id}:{fit.seg_id}"
                iv = _build_interval(
                    plan, fit, er, gid,
                    "assign_nominal" if k == nom_k else "assign_boundary_uncertainty",
                )
                node = idx[er.event.id]
                cost = 0.0 if k == nom_k else abs(
                    er.clock_t - boundaries[k - 1].clock_t
                )
                options.append(
                    _AssignmentOption(
                        seg_gid=gid,
                        cost=cost,
                        edges=[
                            Edge(0, node, iv.hi, er.event.id, "upper"),
                            Edge(node, 0, -iv.lo, er.event.id, "lower"),
                        ],
                    )
                )
                if k == nom_k:
                    rep_interval[er.event.id] = iv
            nominal_gid = f"{plan.source.id}:{nom_fit.seg_id}"
            if len(options) > 1:
                # 名义段优先，其余按跨边界距离（成本）排序
                options.sort(key=lambda o: (o.seg_gid != nominal_gid, o.cost, o.seg_gid))
                slots.append(_Slot(er.event.id, nominal_gid, options))

    # 候选数最少的事件先分支（与关联求解同策略）
    slots.sort(key=lambda s: (len(s.options), s.event_id))

    from .engine import _constraint_edges, _contradiction  # 延迟导入避免循环依赖

    base_edges = list(fixed_edges)
    for c in constraints:
        base_edges.extend(_constraint_edges(c, idx))

    stats = {"nodes": 0, "pruned": 0, "truncated": False}
    assignment: list[Optional[_AssignmentOption]] = [None] * len(slots)
    best: dict[str, object] = {"cost": float("inf"), "assignment": None}
    rep_contra: dict[str, object] = {"depth": -1, "contra": None}

    # 代表区间（名义段）列表，供矛盾链叙述使用
    nominal_intervals = [rep_interval[iv.event_id] for iv in intervals_order]

    def record_prune(level: int, slot: _Slot, opt: _AssignmentOption, pred, pred_edge, bad) -> None:
        stats["pruned"] += 1
        cycle = extract_cycle(n, pred, pred_edge, bad)
        contra = _contradiction(cycle, idx, nominal_intervals, list(constraints))
        iv_by_eid = {iv.event_id: iv for iv in nominal_intervals}
        involved_sources = {
            iv_by_eid[eid].source_id
            for eid in contra.cycle_event_ids
            if iv_by_eid.get(eid) and iv_by_eid[eid].source_id
        }
        # 该剪枝路径上已选归属 + 当前被拒归属；仅保留与矛盾链事件同源的段
        chosen_gids = {o.seg_gid for o in assignment if o is not None}
        chosen_gids.add(opt.seg_gid)
        seg_ids = {
            gid for gid in chosen_gids if gid.split(":", 1)[0] in involved_sources
        }
        anchor_ids: set[str] = set()
        for iv in nominal_intervals:
            if iv.source_id in involved_sources:
                anchor_ids.update(iv.quantity.anchor_ids)
        contra.segment_ids = sorted(seg_ids)
        contra.related_record_ids["clock_segments"] = sorted(seg_ids)
        if anchor_ids:
            contra.related_record_ids["anchors"] = sorted(
                set(contra.related_record_ids.get("anchors", [])) | anchor_ids
            )
        contra.explanation += (
            f"；分段时钟模型下，该矛盾链涉及时钟段 {sorted(seg_ids) or '（无跨段归属）'}"
        )
        if level > rep_contra["depth"]:
            rep_contra["depth"] = level
            rep_contra["contra"] = contra

    def dfs(level: int, edges: list[Edge], cost: float) -> None:
        if best["assignment"] is not None and cost >= best["cost"]:  # 界：成本不可能更优
            return
        if stats["nodes"] >= MAX_ASSIGNMENT_NODES:
            stats["truncated"] = True
            return
        stats["nodes"] += 1
        if level == len(slots):
            best["cost"] = cost
            best["assignment"] = list(assignment)
            best["edges"] = list(edges)
            return
        slot = slots[level]
        for opt in slot.options:
            if stats["truncated"]:
                return
            new_edges = edges + opt.edges
            _d, p, pe, bad_node = _bellman_ford(n, new_edges, start=None)
            if bad_node is not None:
                record_prune(level, slot, opt, p, pe, bad_node)
                continue
            assignment[level] = opt
            dfs(level + 1, new_edges, cost + opt.cost)
            assignment[level] = None

    dfs(0, base_edges, 0.0)

    if best["assignment"] is None:
        return (
            False,
            stats["nodes"],
            stats["truncated"],
            0.0,
            {},
            [rep_interval[iv.event_id] for iv in intervals_order],
            rep_contra["contra"],  # type: ignore[arg-type]
        )

    chosen: dict[str, str] = {}
    final_intervals = {iv.event_id: iv for iv in intervals_order}
    total_cost = 0.0
    for slot, opt in zip(slots, best["assignment"]):
        if opt is None:
            continue
        chosen[slot.event_id] = opt.seg_gid
        total_cost += opt.cost
        # 用选中归属的区间替换代表区间
        for o in slot.options:
            if o.seg_gid == opt.seg_gid:
                node_edges = o.edges
                # 从边恢复 lo/hi
                hi_e = next(e for e in node_edges if e.u == 0)
                lo_e = next(e for e in node_edges if e.v == 0)
                src_iv = rep_interval[slot.event_id]
                q = src_iv.quantity.model_copy(
                    update={"segment_ids": [opt.seg_gid]}
                )
                final_intervals[slot.event_id] = EventIntervalInput(
                    event_id=slot.event_id,
                    source_id=src_iv.source_id,
                    lo=-lo_e.w,
                    hi=hi_e.w,
                    quantity=q,
                )
                break
    ordered = [final_intervals[iv.event_id] for iv in intervals_order]
    return True, stats["nodes"], stats["truncated"], total_cost, chosen, ordered, None


# ----------------------------- 方案评估与报告 -----------------------------


def _scheme_key(plans: list[_SourcePlan], boundaries_by_source: dict[str, list[_Boundary]]) -> str:
    parts: list[str] = []
    for plan in plans:
        ids = [b.seg_id for b in boundaries_by_source[plan.source.id]]
        parts.append(f"{plan.source.id}:[{','.join(ids)}]")
    return ";".join(parts)


def _anchor_rms(fits: list[_SegFit]) -> float:
    ss = 0.0
    cnt = 0
    for fit in fits:
        if fit.carried:
            continue
        m = fit.model
        for r in fit.anchor_rows:
            e = _residual(m, r.true_t, r.clock_t)
            ss += e * e
            cnt += 1
    return math.sqrt(ss / cnt) if cnt else 0.0


def _make_segment_reports(
    plan: _SourcePlan,
    fits: list[_SegFit],
    boundaries: list[_Boundary],
    jumps: list[SegmentJump],
) -> list[SegmentClockModel]:
    jump_by_id = {j.boundary_segment_id: j for j in jumps}
    out: list[SegmentClockModel] = []
    for fit in fits:
        m = fit.model
        boundary = fit.boundary
        jump_q: Optional[float] = None
        jump_resid: Optional[float] = None
        if boundary is not None:
            j = jump_by_id.get(boundary.seg_id)
            jump_q = j.jump_s if j else None
            jump_resid = boundary.residual_jump_s
        anchor_ids = [r.anchor.id for r in fit.anchor_rows]
        out.append(
            SegmentClockModel(
                source_id=plan.source.id,
                segment_id=fit.seg_id,
                start_clock_reading=(
                    boundary.clock_reading if boundary is not None else format_iso(plan.earliest_clock)
                ),
                start_clock_unix_s=(
                    boundary.clock_t if boundary is not None else plan.earliest_clock
                ),
                origin=boundary.origin if boundary is not None else "declared",
                boundary_uncertainty_s=boundary.uncertainty_s if boundary is not None else 0.0,
                offset_s=Quantity(
                    value=m.intercept,
                    unit="s",
                    source_ids=[plan.source.id],
                    anchor_ids=anchor_ids,
                    segment_ids=[f"{plan.source.id}:{fit.seg_id}"],
                    derived_by=(
                        "segments.ols_segment_fit" if m.n >= 2
                        else "segments.single_anchor_offset" if m.n == 1
                        else "segments.carried_or_prior"
                    ),
                    detail=f"段 {fit.seg_id} 参考历元 {format_iso(m.t_ref)} 处 clock-true 偏移；{m.n} 个锚点",
                ),
                drift_ppm=Quantity(
                    value=(m.beta - 1.0) * 1e6,
                    unit="ppm",
                    source_ids=[plan.source.id],
                    anchor_ids=anchor_ids,
                    segment_ids=[f"{plan.source.id}:{fit.seg_id}"],
                    derived_by="segments.ols_segment_fit" if m.n >= 2 else "segments.assumed_rate",
                    detail=f"段 {fit.seg_id} 的 clock 相对 true 线性漂移率 (β-1)*1e6"
                    + ("，锚点不足无法识别，按 0ppm" if m.n < 2 else ""),
                ),
                jump_s=jump_q,
                jump_residual_s=jump_resid,
                reference_epoch=format_iso(m.t_ref),
                anchor_count=m.n if not fit.carried else 0,
                anchor_ids=anchor_ids,
                fit_rms_residual_s=m.sigma_fit if (m.n >= 2 and not fit.carried) else None,
                extrapolation=m.extrapolating,
                warnings=list(fit.warnings),
            )
        )
    return out


def _make_assignments(
    plans: list[_SourcePlan],
    boundaries_by_source: dict[str, list[_Boundary]],
    chosen: dict[str, str],
) -> list[EventSegmentAssignment]:
    out: list[EventSegmentAssignment] = []
    for plan in plans:
        boundaries = boundaries_by_source[plan.source.id]
        for er in plan.events:
            nom_k = _segment_index_at(boundaries, er.clock_t)
            feasible = {nom_k}
            near: list[str] = []
            for k, b in enumerate(boundaries, start=1):
                if abs(er.clock_t - b.clock_t) <= b.uncertainty_s + 1e-9:
                    feasible.add(k)
                    feasible.add(k - 1)
                    near.append(
                        f"距边界 {b.seg_id}({format_iso(b.clock_t)}) "
                        f"{abs(er.clock_t - b.clock_t):.3f}s ≤ 半宽 {b.uncertainty_s:g}s"
                    )
            ordered_k = sorted(feasible)
            seg_names = [
                (plan.initial_id if k == 0 else boundaries[k - 1].seg_id) for k in ordered_k
            ]
            nominal_id = plan.initial_id if nom_k == 0 else boundaries[nom_k - 1].seg_id
            assigned_gid = chosen.get(er.event.id, f"{plan.source.id}:{nominal_id}")
            basis = (
                f"钟面 {er.event.reading}（解析 {format_iso(er.clock_t)}）按读数落在段 "
                f"{nominal_id}"
            )
            if near:
                basis += "；边界不确定区：" + "；".join(near)
                basis += f"；可行归属 {seg_names}，代表解采用 {assigned_gid.split(':', 1)[1]}"
            else:
                basis += "（唯一可行归属）"
            out.append(
                EventSegmentAssignment(
                    event_id=er.event.id,
                    source_id=plan.source.id,
                    nominal_segment_id=nominal_id,
                    feasible_segment_ids=seg_names,
                    assigned_segment_id=assigned_gid.split(":", 1)[1],
                    ambiguous=len(seg_names) > 1,
                    basis=basis,
                )
            )
    return out


# ----------------------------- 入口 -----------------------------


def run_segmentation(
    scenario: Scenario,
    legacy_intervals: list[EventIntervalInput],
    legacy_models: dict[str, FittedClock],
) -> tuple[
    SegmentationReport,
    Optional[list[EventIntervalInput]],
    Optional[list[EventIntervalInput]],
    list[str],
]:
    """执行分段时钟校核。

    返回 (报告, 代表方案一元区间（无可行方案为 None）, 排名最高方案的名义
    归段区间（不可行时供调整建议使用）, 警告)。
    """
    warnings: list[str] = []
    src_by_id = {s.id: s for s in scenario.sources}
    anchors_by_source: dict[str, list[CalibrationAnchor]] = {}
    events_by_source: dict[str, list[Event]] = {}
    for anc in scenario.anchors:
        anchors_by_source.setdefault(anc.source_id, []).append(anc)
    for ev in scenario.events:
        if ev.source_id is not None:
            events_by_source.setdefault(ev.source_id, []).append(ev)

    plans: list[_SourcePlan] = []
    rule_sources: set[str] = set()
    for rule in scenario.clock_segments:
        if rule.source_id not in src_by_id:
            raise ClockModelError(f"分段规则引用了不存在的来源 {rule.source_id}")
        if rule.source_id in rule_sources:
            raise ClockModelError(f"来源 {rule.source_id} 声明了多条分段规则")
        rule_sources.add(rule.source_id)
        plan, pw = _prepare_source(
            rule,
            src_by_id[rule.source_id],
            anchors_by_source.get(rule.source_id, []),
            events_by_source.get(rule.source_id, []),
            scenario.default_utc_offset_s,
            scenario.auto_jump_threshold_s,
        )
        warnings.extend(pw)
        plans.append(plan)

    # 枚举方案（自动边界子集的组合）
    choices, enum_truncated = _enumerate_choices(
        [p.options for p in plans], scenario.max_segment_schemes
    )

    # 固定（非分段来源）事件的一元边
    seg_event_ids = {e.event.id for p in plans for e in p.events}
    fixed_intervals = [iv for iv in legacy_intervals if iv.event_id not in seg_event_ids]
    all_events_ordered = list(scenario.events)
    idx = {ev.id: i + 1 for i, ev in enumerate(all_events_ordered)}

    evaluations: list[_SchemeEval] = []

    for combo in choices:
        fits_by_source: dict[str, list[_SegFit]] = {}
        boundaries_by_source: dict[str, list[_Boundary]] = {}
        scheme_warnings: list[str] = []
        for plan, ci in zip(plans, combo):
            chosen_auto = plan.options[ci]
            fits, boundaries, sw = _fit_scheme_source(plan, chosen_auto)
            fits_by_source[plan.source.id] = fits
            boundaries_by_source[plan.source.id] = boundaries
            scheme_warnings.extend(sw)

        # 跳变量
        jumps: list[SegmentJump] = []
        total_abs = 0.0
        for plan in plans:
            fits = fits_by_source[plan.source.id]
            for k in range(1, len(fits)):
                boundary = boundaries_by_source[plan.source.id][k - 1]
                jump = _jump_at_boundary(boundary, fits[k - 1], fits[k])
                if jump is not None:
                    total_abs += abs(jump)
                jumps.append(
                    SegmentJump(
                        source_id=plan.source.id,
                        boundary_segment_id=boundary.seg_id,
                        at_clock_reading=boundary.clock_reading,
                        origin=boundary.origin,  # type: ignore[arg-type]
                        jump_s=jump,
                        uncertainty_s=boundary.uncertainty_s,
                        detail=(
                            f"边界 {boundary.seg_id}（{boundary.origin}）处，"
                            + (
                                f"同一真实时刻下后段钟面较前段外推值"
                                f"{'向前' if jump >= 0 else '向后'}拨 {abs(jump):.3f}s"
                                if jump is not None
                                else "相邻段锚点不足，跳变量无法估计"
                            )
                            + (
                                f"；自动检测残差跳变 {boundary.residual_jump_s:.3f}s"
                                if boundary.residual_jump_s is not None
                                else ""
                            )
                        ),
                    )
                )

        # 代表区间先按名义段构造（非分段来源沿用旧区间）
        rep_list: list[EventIntervalInput] = []
        for ev in all_events_ordered:
            if ev.id in seg_event_ids:
                plan = next(p for p in plans if p.source.id == ev.source_id)
                er = next(e for e in plan.events if e.event.id == ev.id)
                boundaries = boundaries_by_source[plan.source.id]
                nom_k = _segment_index_at(boundaries, er.clock_t)
                fit = fits_by_source[plan.source.id][nom_k]
                gid = f"{plan.source.id}:{fit.seg_id}"
                rep_list.append(_build_interval(plan, fit, er, gid, "assign_nominal"))
            else:
                rep_list.append(next(iv for iv in legacy_intervals if iv.event_id == ev.id))

        fixed_edges: list[Edge] = []
        for iv in fixed_intervals:
            node = idx[iv.event_id]
            fixed_edges.append(Edge(0, node, iv.hi, iv.event_id, "upper"))
            fixed_edges.append(Edge(node, 0, -iv.lo, iv.event_id, "lower"))

        (
            feasible,
            branches,
            assign_truncated,
            amb_cost,
            chosen_map,
            final_intervals,
            contra,
        ) = _search_assignment(
            scenario,
            plans,
            fits_by_source,
            boundaries_by_source,
            fixed_edges,
            rep_list,
            idx,
            scenario.constraints,
        )

        rms_by_source = {p.source.id: _anchor_rms(fits_by_source[p.source.id]) for p in plans}
        pooled_ss = 0.0
        pooled_n = 0
        for plan in plans:
            for fit in fits_by_source[plan.source.id]:
                if fit.carried:
                    continue
                for r in fit.anchor_rows:
                    e = _residual(fit.model, r.true_t, r.clock_t)
                    pooled_ss += e * e
                    pooled_n += 1
        fit_score = math.sqrt(pooled_ss / pooled_n) if pooled_n else 0.0
        n_boundaries = sum(len(v) for v in boundaries_by_source.values())
        key = _scheme_key(plans, boundaries_by_source)

        warnings.extend(w for w in scheme_warnings if w not in warnings)
        evaluations.append(
            _SchemeEval(
                key=key,
                feasible=feasible,
                branches=branches,
                truncated=assign_truncated,
                ambiguity_cost=amb_cost,
                chosen_assignment=chosen_map,
                intervals=final_intervals,
                nominal_intervals=rep_list,
                fit_score=fit_score,
                total_abs_jump=total_abs,
                n_boundaries=n_boundaries,
                anchor_rms=rms_by_source,
                segment_fits=fits_by_source,
                ordered_boundaries=boundaries_by_source,
                jumps=jumps,
                assignments=_make_assignments(plans, boundaries_by_source, chosen_map),
                contradiction=contra,
                detail=(
                    f"方案 {key}：{n_boundaries} 个跳变边界，锚点分段残差 RMS "
                    f"{fit_score:.3f}s，跨段归属搜索 {branches} 节点，"
                    + ("差分约束可行" if feasible else "所有跨段归属均被差分约束剪枝")
                ),
            )
        )

    # 排序：可行优先 → 拟合 RMS → 段数少 → 跨段成本低 → key 稳定
    evaluations.sort(
        key=lambda e: (
            0 if e.feasible else 1,
            e.fit_score,
            e.n_boundaries,
            e.ambiguity_cost,
            e.key,
        )
    )

    any_feasible = any(e.feasible for e in evaluations)
    chosen_eval = evaluations[0] if evaluations else None
    schemes: list[SegmentationScheme] = []
    for rank, ev in enumerate(evaluations, start=1):
        seg_reports: list[SegmentClockModel] = []
        for plan in plans:
            seg_reports.extend(
                _make_segment_reports(
                    plan,
                    ev.segment_fits[plan.source.id],
                    ev.ordered_boundaries[plan.source.id],
                    [j for j in ev.jumps if j.source_id == plan.source.id],
                )
            )
        schemes.append(
            SegmentationScheme(
                key=ev.key,
                rank=rank,
                segments=seg_reports,
                jumps=ev.jumps,
                assignments=ev.assignments,
                feasible=ev.feasible,
                chosen=(rank == 1),
                chosen_assignment=ev.chosen_assignment if ev.feasible else None,
                assignment_branches=ev.branches,
                fit_score_s=ev.fit_score,
                total_abs_jump_s=ev.total_abs_jump,
                ambiguity_cost_s=ev.ambiguity_cost,
                anchor_rms_residual_s=ev.anchor_rms,
                contradiction=ev.contradiction if not ev.feasible else None,
                detail=ev.detail,
            )
        )

    detected_all = [d for p in plans for d in p.detected]
    any_assign_truncated = any(e.truncated for e in evaluations)
    if any_feasible:
        status = "ok"
    elif enum_truncated or any_assign_truncated:
        status = "truncated"
    else:
        status = "infeasible"

    report = SegmentationReport(
        status=status,  # type: ignore[arg-type]
        segmented_sources=[p.source.id for p in plans],
        detected_jumps=detected_all,
        schemes=schemes,
        best_scheme_key=chosen_eval.key if chosen_eval else None,
        schemes_total=len(evaluations),
        schemes_truncated=enum_truncated,
        max_schemes=scenario.max_segment_schemes,
        max_assignment_nodes=MAX_ASSIGNMENT_NODES,
        method=_METHOD,
    )
    primary = chosen_eval.intervals if (chosen_eval is not None and chosen_eval.feasible) else None
    nominal_best = chosen_eval.nominal_intervals if chosen_eval is not None else None
    # 去重警告，保持稳定顺序
    uniq: list[str] = []
    for w in warnings:
        if w not in uniq:
            uniq.append(w)
    return report, primary, nominal_best, uniq
