"""校核引擎：时钟换算 → 差分约束 → 求解/矛盾 → 偏移建议/方案比较。"""

from __future__ import annotations

from typing import Optional

from .dcs import Cycle, DCSInfeasible, Edge, edge_slack, extract_cycle, solve, _bellman_ford
from .models import (
    AdjustmentReport,
    Constraint,
    ConstraintSlack,
    Contradiction,
    Event,
    EventIntervalInput,
    EventWindow,
    PlanDifference,
    Quantity,
    ReconcileResult,
    Scenario,
    SourceAdjustment,
)
from .timescale import (
    ClockModelError,
    TimeParseError,
    build_clock_models,
    convert_events,
    format_iso,
)

MAX_BUDGET_S = 315_360_000.0  # 二分搜索最小修正预算的上限：10 年（秒）


class ScenarioValidationError(ValueError):
    """场景引用/结构错误（输入非法，应映射为 HTTP 400）。"""


# ----------------------------- 校验 -----------------------------


def validate_scenario(scenario: Scenario) -> None:
    errs: list[str] = []
    seen: set[str] = set()
    for s in scenario.sources:
        if s.id in seen:
            errs.append(f"来源 ID 重复：{s.id}")
        seen.add(s.id)
    seen.clear()
    for ev in scenario.events:
        if ev.id in seen:
            errs.append(f"事件 ID 重复：{ev.id}")
        seen.add(ev.id)
        if ev.source_id is not None and not any(s.id == ev.source_id for s in scenario.sources):
            errs.append(f"事件 {ev.id} 引用了不存在的来源 {ev.source_id}")
    seen.clear()
    events = {e.id for e in scenario.events}
    for c in scenario.constraints:
        if c.id in seen:
            errs.append(f"约束 ID 重复：{c.id}")
        seen.add(c.id)
        if c.a not in events:
            errs.append(f"约束 {c.id} 的事件 a={c.a} 不存在")
        if c.b not in events:
            errs.append(f"约束 {c.id} 的事件 b={c.b} 不存在")
        if c.a == c.b:
            errs.append(f"约束 {c.id} 的两个事件不能相同（{c.a}）")
        if c.type == "min_interval" and c.min_s is None:
            errs.append(f"约束 {c.id} 为 min_interval 但缺少 min_s")
        if c.type == "max_interval" and c.max_s is None:
            errs.append(f"约束 {c.id} 为 max_interval 但缺少 max_s")
        if (
            c.type in ("min_interval", "max_interval")
            and c.min_s is not None
            and c.max_s is not None
            and c.type == "min_interval"
        ):
            pass
    if errs:
        raise ScenarioValidationError("；".join(errs))


# ----------------------------- 图构建 -----------------------------


def _constraint_edges(c: Constraint, idx: dict[str, int]) -> list[Edge]:
    """把一条用户约束翻译为差分边（节点 0 为历元，事件节点为 1..m）。"""
    ia, ib = idx[c.a], idx[c.b]
    cid = c.id
    if c.type == "before":
        # t_b - t_a ≥ 0  →  t_a - t_b ≤ 0  →  b→a, 0
        return [Edge(ib, ia, 0.0, cid, "constraint")]
    if c.type == "same_event":
        # |t_a - t_b| ≤ tol
        return [
            Edge(ib, ia, c.tolerance_s, cid, "constraint"),  # t_a - t_b ≤ tol
            Edge(ia, ib, c.tolerance_s, cid, "constraint"),  # t_b - t_a ≤ tol
        ]
    if c.type == "min_interval":
        # a 先于 b 至少 min：t_b - t_a ≥ min → t_a - t_b ≤ -min
        return [Edge(ib, ia, -float(c.min_s), cid, "constraint")]
    if c.type == "max_interval":
        # a 先于 b 至多 max：t_b - t_a ≤ max
        return [Edge(ia, ib, float(c.max_s), cid, "constraint")]
    raise ScenarioValidationError(f"未知约束类型：{c.type}")


def build_graph(
    intervals: list[EventIntervalInput],
    constraints: list[Constraint],
) -> tuple[int, dict[str, int], list[Edge]]:
    idx = {iv.event_id: i + 1 for i, iv in enumerate(intervals)}
    n = len(intervals) + 1
    edges: list[Edge] = []
    for iv in intervals:
        i = idx[iv.event_id]
        edges.append(Edge(0, i, iv.hi, iv.event_id, "upper"))  # x_i ≤ hi
        edges.append(Edge(i, 0, -iv.lo, iv.event_id, "lower"))  # x_i ≥ lo
    for c in constraints:
        edges.extend(_constraint_edges(c, idx))
    return n, idx, edges


# ----------------------------- 矛盾链 -----------------------------


def _contradiction(
    cycle: Cycle,
    idx: dict[str, int],
    intervals: list[EventIntervalInput],
    constraints: list[Constraint],
) -> Contradiction:
    node_to_event = {i: eid for eid, i in idx.items()}
    cycle_events = [node_to_event[v] for v in cycle.nodes if v in node_to_event]
    edge_events: list[str] = []
    cycle_constraints: list[str] = []
    for e in cycle.edges:
        if e.kind in ("upper", "lower"):
            edge_events.append(e.ref)
        else:
            cycle_constraints.append(e.ref)

    iv_by_id = {iv.event_id: iv for iv in intervals}
    con_by_id = {c.id: c for c in constraints}
    related_events = sorted(set(cycle_events) | set(edge_events))
    sources = sorted(
        {iv_by_id[eid].source_id for eid in related_events if iv_by_id[eid].source_id}
    )
    anchors = sorted(
        {a for iv in intervals if iv.source_id in sources for a in iv.quantity.anchor_ids}
    )
    chain = " → ".join(cycle_events + [cycle_events[0]] if cycle_events else [])
    edge_desc = []
    for e in cycle.edges:
        if e.kind in ("upper", "lower"):
            iv = iv_by_id[e.ref]
            bound = iv.hi if e.kind == "upper" else iv.lo
            edge_desc.append(
                f"{e.ref} 的{('最晚≤' + format_iso(bound)) if e.kind == 'upper' else ('最早≥' + format_iso(bound))}"
            )
        elif e.kind == "bound":
            edge_desc.append(f"修正幅度边界（{e.ref}）")
        else:
            cc = con_by_id[e.ref]
            edge_desc.append(f"{cc.type} 约束 {e.ref}（{cc.a},{cc.b}，边权 {e.w:g}s）")
    explanation = (
        f"以下约束沿闭环 {chain} 叠加后要求总时差 ≤ {cycle.total_weight:.3f}s（<0），"
        f"即时间至少要倒流 {-cycle.total_weight:.3f}s 才能全部满足：" + "；".join(edge_desc)
    )
    return Contradiction(
        cycle_event_ids=cycle_events,
        cycle_constraint_ids=sorted(set(cycle_constraints)),
        total_slack_s=cycle.total_weight,
        related_record_ids={
            "events": related_events,
            "constraints": sorted(set(cycle_constraints)),
            "sources": sources,
            "anchors": anchors,
        },
        explanation=explanation,
    )


# ----------------------------- 偏移建议（平移量差分系统） -----------------------------


def _multi_source_bellman_ford(
    n: int, edges: list[Edge], seed_dist: dict[int, float]
) -> tuple[list[float], Optional[int], list[Optional[int]], list[Optional[Edge]]]:
    """多源 Bellman-Ford：种子节点距离初始化为给定值（其余 +inf）。

    返回 (dist, 第 N 轮仍被松弛的节点, pred, pred_edge)。
    """
    dist = [float("inf")] * n
    for s, d0 in seed_dist.items():
        dist[s] = min(dist[s], d0)
    pred: list[Optional[int]] = [None] * n
    pred_edge: list[Optional[Edge]] = [None] * n
    updated: Optional[int] = None
    for _ in range(n):
        updated = None
        for e in edges:
            if dist[e.u] == float("inf"):
                continue
            nd = dist[e.u] + e.w
            if nd + 1e-12 < dist[e.v]:
                dist[e.v] = nd
                pred[e.v] = e.u
                pred_edge[e.v] = e
                updated = e.v
        if updated is None:
            break
    return dist, updated, pred, pred_edge


def _shift_system(
    n: int,
    edges: list[Edge],
    intervals: list[EventIntervalInput],
    source_order: list[str],
    ev_source: dict[int, Optional[str]],
) -> tuple[list[Edge], Optional[Cycle]]:
    """消去事件变量，得到只关于来源整体平移量 s_j 的差分系统。

    对每个来源 g（历元/无来源事件归入 ``None``），以其全部事件为 0 距离
    种子做一次多源最短路，令 d_g[j] = min_{i∈g}(hi_i + D[i→j])。
    平移后相容性条件聚合为来源对约束：

        s_g - s_h ≤ min_{i∈g, j∈h}(hi_i - lo_j + D[i→j])
                  = min_{j∈h}(d_g[j] - lo_j)

    无来源事件视为 s_0=0（固定节点）。事件约束图自身含负环时返回该环，
    那是整体平移无法消除的固有矛盾。
    """
    m = n - 1
    iv = {i + 1: intervals[i] for i in range(m)}
    bf_edges = [e for e in edges if e.u != 0 and e.v != 0]

    groups: dict[Optional[str], list[int]] = {}
    for i in range(1, m + 1):
        groups.setdefault(ev_source.get(i), []).append(i)

    # 先做一次零初值 BF，检测事件约束自身负环
    dist0, pred0, pe0, bad0 = _bellman_ford(n, bf_edges, start=None)
    if bad0 is not None:
        return [], extract_cycle(n, pred0, pe0, bad0)

    src_node = {sid: k + 1 for k, sid in enumerate(source_order)}
    sedges: list[Edge] = []

    group_keys = list(groups.keys())
    best_edge: dict[tuple[Optional[str], Optional[str]], float] = {}
    for g in group_keys:
        # 种子初始距离 = hi_i：使得到达 j 的最短路即 min_i (hi_i + D[i→j])
        seed_dist = {i: iv[i].hi for i in groups[g]}
        dist, _bad, _p, _pe = _multi_source_bellman_ford(n, bf_edges, seed_dist)
        for j in range(1, m + 1):
            if dist[j] == float("inf"):
                continue
            h = ev_source.get(j)
            val = dist[j] - iv[j].lo
            key = (g, h)
            if key not in best_edge or val < best_edge[key]:
                best_edge[key] = val

    for (g, h), w in best_edge.items():
        u = src_node[g] if g else 0
        v = src_node[h] if h else 0
        if u == v:
            continue
        sedges.append(Edge(u, v, w, f"shift:{g or 'epoch'}|{h or 'epoch'}", "shift"))

    return sedges, None


def _shift_feasible(
    ns: int, sedges: list[Edge], budget_s: float
) -> tuple[bool, list[float], Optional[Cycle]]:
    bounded = list(sedges)
    for node in range(1, ns):
        bounded.append(Edge(0, node, budget_s, f"budget:node{node}", "bound"))
        bounded.append(Edge(node, 0, budget_s, f"budget:node{node}", "bound"))
    dist, pred, pred_edge, bad = _bellman_ford(ns, bounded, start=None)
    if bad is not None:
        return False, dist, extract_cycle(ns, pred, pred_edge, bad)
    return True, dist, None


def _shift_bounds(ns: int, sedges: list[Edge]) -> tuple[list[float], list[float]]:
    upper, _, _, _ = _bellman_ford(ns, sedges, start=0)
    neg = [Edge(e.v, e.u, e.w, e.ref, e.kind) for e in sedges]
    ndist, _, _, _ = _bellman_ford(ns, neg, start=0)
    lower = [-x for x in ndist]
    return lower, upper


def _compute_adjustments(
    scenario: Scenario,
    n: int,
    edges: list[Edge],
    intervals: list[EventIntervalInput],
    constraints: list[Constraint],
    ev_source: dict[int, Optional[str]],
    budget_s: Optional[float],
    currently_feasible: bool,
) -> AdjustmentReport:
    source_order = [s.id for s in scenario.sources]
    src_names = {s.id: (s.name or s.id) for s in scenario.sources}
    ns = len(source_order) + 1
    sedges, bad_cycle = _shift_system(n, edges, intervals, source_order, ev_source)

    # 固有矛盾：事件约束图自身成负环，整体平移无法消除
    if bad_cycle is not None:
        idx = {iv.event_id: i + 1 for i, iv in enumerate(intervals)}
        return AdjustmentReport(
            budget_s=budget_s,
            feasible=False,
            min_required_budget_s=None,
            adjustments=[],
            residual_contradiction=_contradiction(bad_cycle, idx, intervals, constraints),
            method=(
                "dcs.shift_dcs：事件间约束（不依赖时钟偏移）自身形成负环"
                f"（总权重 {bad_cycle.total_weight:.3f}s），整体平移时钟无法消除，"
                "必须修改约束或重新校时"
            ),
        )

    # 无平移变量（所有事件都是统一参考时间）：可行性即当前结论
    if ns == 1:
        return AdjustmentReport(
            budget_s=budget_s,
            feasible=currently_feasible,
            min_required_budget_s=0.0 if currently_feasible else None,
            adjustments=[],
            method="dcs.shift_dcs（场景中无可调整时钟来源）",
        )

    # 无预算约束下先判可行性
    ok0, _, _ = _shift_feasible(ns, sedges, MAX_BUDGET_S)
    if not ok0:
        min_budget: Optional[float] = None
    else:
        lo_b, hi_b = 0.0, MAX_BUDGET_S
        ok_hi, _, _ = _shift_feasible(ns, sedges, hi_b)
        if not ok_hi:
            min_budget = None
        else:
            for _ in range(40):
                mid = (lo_b + hi_b) / 2
                ok_m, _, _ = _shift_feasible(ns, sedges, mid)
                if ok_m:
                    hi_b = mid
                else:
                    lo_b = mid
            min_budget = hi_b

    adjustments: list[SourceAdjustment] = []
    feasible_at_budget = currently_feasible
    residual: Optional[Contradiction] = None

    if budget_s is not None:
        ok_b, dist_b, cyc_b = _shift_feasible(ns, sedges, budget_s)
        feasible_at_budget = ok_b
        if not ok_b:
            # 用预算内仍存在的负环构造矛盾说明
            residual = _shift_cycle_contradiction(
                cyc_b, source_order, intervals, constraints
            )
        else:
            # 在预算有界图上求各平移量的可行区间（最短路上下确界）
            bounded = list(sedges)
            for node in range(1, ns):
                bounded.append(Edge(0, node, budget_s, f"budget:node{node}", "bound"))
                bounded.append(Edge(node, 0, budget_s, f"budget:node{node}", "bound"))
            upper, _, _, _ = _bellman_ford(ns, bounded, start=0)
            neg = [Edge(e.v, e.u, e.w, e.ref, e.kind) for e in bounded]
            ndist, _, _, _ = _bellman_ford(ns, neg, start=0)
            for k, sid in enumerate(source_order):
                node = k + 1
                lo_s, hi_s = -ndist[node], upper[node]
                suggested = min(max(dist_b[node], lo_s), hi_s)
                adjustments.append(
                    SourceAdjustment(
                        source_id=sid,
                        suggested_shift_s=Quantity(
                            value=suggested,
                            unit="s",
                            source_ids=[sid],
                            derived_by="dcs.shift_feasible_potential",
                            detail=(
                                f"建议把来源 {src_names[sid]} 的时钟整体平移 {suggested:.3f}s"
                                f"（正值=把该来源事件时刻向后拨）；预算 {budget_s:g}s 内"
                                f"该修正量的可行区间 [{lo_s:.3f}, {hi_s:.3f}]s，"
                                "取值点为 Bellman-Ford 可行势能"
                            ),
                        ),
                        shift_feasible_range_s=(lo_s, hi_s),
                        clock_model_offset_s=0.0,
                    )
                )

    return AdjustmentReport(
        budget_s=budget_s,
        feasible=feasible_at_budget,
        min_required_budget_s=min_budget,
        adjustments=adjustments,
        residual_contradiction=residual,
        method=(
            "dcs.shift_dcs：先在事件间约束图上做 Floyd-Warshall 全源最短路 D[i→j]，"
            "消去事件变量后得到仅含来源平移量 s_j 的差分约束 "
            "s_i-s_j ≤ hi_i-lo_j+D[i→j]；叠加 |s_j|≤预算 用 Bellman-Ford 判可行性，"
            "40 次二分给出最小 L∞ 修正预算；建议值取可行势能点，可行区间取节点上下确界"
        ),
    )


def _shift_cycle_contradiction(
    cycle: Optional[Cycle],
    source_order: list[str],
    intervals: list[EventIntervalInput],
    constraints: list[Constraint],
) -> Optional[Contradiction]:
    if cycle is None:
        return None
    sources_in_cycle = [
        source_order[v - 1] for v in cycle.nodes if 1 <= v <= len(source_order)
    ]
    return Contradiction(
        cycle_event_ids=[],
        cycle_constraint_ids=sorted({c.id for c in constraints}),
        total_slack_s=cycle.total_weight,
        related_record_ids={
            "events": [iv.event_id for iv in intervals if iv.source_id in sources_in_cycle],
            "constraints": sorted({c.id for c in constraints}),
            "sources": sources_in_cycle,
            "anchors": sorted(
                {a for iv in intervals if iv.source_id in sources_in_cycle
                 for a in iv.quantity.anchor_ids}
            ),
        },
        explanation=(
            f"在限定修正幅度内，来源间平移量约束沿 "
            f"{' → '.join(sources_in_cycle + sources_in_cycle[:1])} 仍成负环"
            f"（总权重 {cycle.total_weight:.3f}s）；放宽预算至少到报告的"
            "最小所需预算才可修复"
        ),
    )


# ----------------------------- 主编排 -----------------------------


def reconcile(scenario: Scenario, budget_s: Optional[float] = None) -> ReconcileResult:
    validate_scenario(scenario)
    warnings: list[str] = []
    try:
        models, reports, cw, fit_errors = build_clock_models(
            scenario.sources, scenario.anchors, scenario.events, scenario.default_utc_offset_s
        )
        warnings.extend(cw)
        intervals, ew = convert_events(
            scenario.sources, scenario.events, models, scenario.default_utc_offset_s
        )
        warnings.extend(ew)
    except (TimeParseError, ClockModelError) as exc:
        raise ScenarioValidationError(str(exc)) from exc
    if fit_errors:
        raise ScenarioValidationError("；".join(fit_errors))

    n, idx, edges = build_graph(intervals, scenario.constraints)

    # 事件节点 → 来源（用于偏移建议增广图）
    iv_by_id = {iv.event_id: iv for iv in intervals}
    ev_source: dict[int, Optional[str]] = {0: None}
    for eid, node in idx.items():
        ev_source[node] = iv_by_id[eid].source_id

    try:
        lower, upper = solve(n, edges)
    except DCSInfeasible as exc:
        contradiction = _contradiction(exc.cycle, idx, intervals, scenario.constraints)
        result = ReconcileResult(
            scenario_name=scenario.name,
            feasible=False,
            clock_models=list(reports.values()),
            event_intervals=intervals,
            contradiction=contradiction,
            warnings=warnings,
        )
        result.adjustment = _compute_adjustments(
            scenario, n, edges, intervals, scenario.constraints,
            ev_source, budget_s, currently_feasible=False,
        )
        # 填充当前模型偏移
        for adj in result.adjustment.adjustments:
            if adj.source_id in reports:
                adj.clock_model_offset_s = reports[adj.source_id].offset_s.value
        return result

    # 可行：组装统一时间线
    timeline: list[EventWindow] = []
    for iv in intervals:
        i = idx[iv.event_id]
        lo, hi = lower[i], upper[i]
        mid = (lo + hi) / 2
        width = hi - lo
        q_lo = Quantity(
            value=lo,
            unit="unix_seconds_utc",
            source_ids=[iv.source_id] if iv.source_id else [],
            anchor_ids=list(iv.quantity.anchor_ids),
            event_ids=[iv.event_id],
            derived_by="dcs.shortest_path_lower_bound",
            detail=f"取反图上从历元节点 0 到事件节点 {i} 的最短路，即该事件最早可能时刻",
        )
        q_hi = Quantity(
            value=hi,
            unit="unix_seconds_utc",
            source_ids=[iv.source_id] if iv.source_id else [],
            anchor_ids=list(iv.quantity.anchor_ids),
            event_ids=[iv.event_id],
            derived_by="dcs.shortest_path_upper_bound",
            detail=f"原图上从历元节点 0 到事件节点 {i} 的最短路，即该事件最晚可能时刻",
        )
        q_w = Quantity(
            value=width,
            unit="s",
            source_ids=[iv.source_id] if iv.source_id else [],
            anchor_ids=list(iv.quantity.anchor_ids),
            event_ids=[iv.event_id],
            derived_by="engine.window_width",
            detail="最晚 - 最早；0 表示该事件时刻被约束完全钉死",
        )
        timeline.append(
            EventWindow(
                earliest=format_iso(lo),
                latest=format_iso(hi),
                earliest_unix_s=q_lo,
                latest_unix_s=q_hi,
                representative=format_iso(mid),
                width_s=q_w,
                source_id=iv.source_id,
            )
        )

    # 约束余量
    slack_map = edge_slack(edges, lower, upper)
    slack_reports: list[ConstraintSlack] = []
    for c in scenario.constraints:
        per_edges = slack_map[c.id]
        worst = min(s for _, s in per_edges)
        tight_names = {
            "before": f"t({c.b}) - t({c.a}) ≥ 0",
            "same_event": f"|t({c.a}) - t({c.b})| ≤ {c.tolerance_s:g}s",
            "min_interval": f"t({c.b}) - t({c.a}) ≥ {c.min_s:g}s" if c.min_s is not None else "min_interval",
            "max_interval": f"t({c.b}) - t({c.a}) ≤ {c.max_s:g}s" if c.max_s is not None else "max_interval",
        }
        slack_reports.append(
            ConstraintSlack(
                constraint_id=c.id,
                type=c.type,
                a=c.a,
                b=c.b,
                satisfiable=worst >= -1e-9,
                slack_s=max(worst, 0.0),
                detail=(
                    f"{tight_names[c.type]}；约束余量 {worst:.3f}s"
                    + ("（紧约束）" if abs(worst) <= 1e-9 else "")
                ),
            )
        )

    result = ReconcileResult(
        scenario_name=scenario.name,
        feasible=True,
        unified_timeline=timeline,
        clock_models=list(reports.values()),
        event_intervals=intervals,
        constraint_slack=slack_reports,
        warnings=warnings,
    )
    if budget_s is not None:
        result.adjustment = _compute_adjustments(
            scenario, n, edges, intervals, scenario.constraints,
            ev_source, budget_s, currently_feasible=True,
        )
        for adj in result.adjustment.adjustments:
            if adj.source_id in reports:
                adj.clock_model_offset_s = reports[adj.source_id].offset_s.value
    return result


# ----------------------------- 方案比较 -----------------------------


def diff_plans(
    left: ReconcileResult,
    right: ReconcileResult,
    left_ref: str,
    right_ref: str,
    left_scenario: Scenario,
    right_scenario: Scenario,
) -> PlanDifference:
    le = {w.earliest_unix_s.event_ids[0]: w for w in left.unified_timeline}
    re = {w.earliest_unix_s.event_ids[0]: w for w in right.unified_timeline}
    common = sorted(set(le) & set(re))
    deltas: dict[str, dict[str, float]] = {}
    for eid in common:
        l, r = le[eid], re[eid]
        l_lo, l_hi = l.earliest_unix_s.value, l.latest_unix_s.value
        r_lo, r_hi = r.earliest_unix_s.value, r.latest_unix_s.value
        deltas[eid] = {
            "earliest_delta_s": r_lo - l_lo,
            "latest_delta_s": r_hi - l_hi,
            "representative_delta_s": (
                (r_lo + r_hi) / 2 - (l_lo + l_hi) / 2
            ),
            "width_delta_s": (r_hi - r_lo) - (l_hi - l_lo),
        }
    ls = {s.id for s in left_scenario.sources}
    rs = {s.id for s in right_scenario.sources}
    lc = {c.id for c in left_scenario.constraints}
    rc = {c.id for c in right_scenario.constraints}
    return PlanDifference(
        left_ref=left_ref,
        right_ref=right_ref,
        events_only_left=sorted(set(le) - set(re)),
        events_only_right=sorted(set(re) - set(le)),
        constraints_only_left=sorted(lc - rc),
        constraints_only_right=sorted(rc - lc),
        sources_only_left=sorted(ls - rs),
        sources_only_right=sorted(rs - ls),
        event_window_deltas_s=deltas,
        feasible_left=left.feasible,
        feasible_right=right.feasible,
        min_budget_left_s=(left.adjustment.min_required_budget_s if left.adjustment else None),
        min_budget_right_s=(right.adjustment.min_required_budget_s if right.adjustment else None),
        explanation=(
            "event_window_deltas_s 中各量为右方案相对左方案的秒级差值（右-左）；"
            "宽度差为正表示右方案该事件的可行时刻范围更宽"
        ),
    )
