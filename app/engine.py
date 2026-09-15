"""校核引擎：时钟换算 → 差分约束 → 求解/矛盾 → 偏移建议/方案比较 → 候选关联。"""

from __future__ import annotations

from typing import Optional

from .dcs import Cycle, DCSInfeasible, Edge, edge_slack, extract_cycle, solve, _bellman_ford
from .models import (
    AdjustmentReport,
    Constraint,
    ConstraintSlack,
    Contradiction,
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
    tz_sources: set[str] = set()
    for s in scenario.sources:
        if s.id in seen:
            errs.append(f"来源 ID 重复：{s.id}")
        seen.add(s.id)
        if s.iana_timezone:
            from .timezones import TimezoneResolutionError, load_zone

            try:
                load_zone(s.iana_timezone)
            except TimezoneResolutionError as exc:
                errs.append(f"来源 {s.id} 声明的 IANA 时区无效：{exc}")
            tz_sources.add(s.id)
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
    constraint_ids = {c.id for c in scenario.constraints}
    seen.clear()
    seg_sources: set[str] = set()
    for rule in scenario.clock_segments:
        if rule.source_id in seg_sources:
            errs.append(f"来源 {rule.source_id} 声明了多条分段时钟规则")
        seg_sources.add(rule.source_id)
        if not any(s.id == rule.source_id for s in scenario.sources):
            errs.append(f"分段规则引用了不存在的来源 {rule.source_id}")
        if rule.source_id in tz_sources:
            errs.append(
                f"来源 {rule.source_id} 同时声明 IANA 时区与分段时钟规则，"
                "暂不支持组合使用（可分别为不同来源声明）"
            )
        seg_ids: set[str] = set()
        for seg in rule.segments:
            if seg.id in seg_ids:
                errs.append(
                    f"来源 {rule.source_id} 的时钟段 ID 重复：{seg.id}"
                )
            seg_ids.add(seg.id)
        if not rule.segments and rule.jump_threshold_s is None and scenario.auto_jump_threshold_s is None:
            errs.append(
                f"来源 {rule.source_id} 的分段规则必须显式声明时钟段，或给出 "
                "jump_threshold_s / 场景 auto_jump_threshold_s"
            )
    seen.clear()
    for g in scenario.association_groups:
        if g.id in seen:
            errs.append(f"关联组 ID 重复：{g.id}")
        seen.add(g.id)
        if f"assoc:{g.id}" in constraint_ids:
            errs.append(f"关联组 {g.id} 生成的约束 ID assoc:{g.id} 与已有约束冲突")
        if g.base_event_id not in events:
            errs.append(f"关联组 {g.id} 的基准事件 {g.base_event_id} 不存在")
        if g.mode == "exactly_one" and not g.candidates:
            errs.append(f"关联组 {g.id} 为 exactly_one 但候选列表为空")
        cand_seen: set[str] = set()
        for cand in g.candidates:
            if cand.event_id not in events:
                errs.append(f"关联组 {g.id} 的候选事件 {cand.event_id} 不存在")
            if cand.event_id == g.base_event_id:
                errs.append(f"关联组 {g.id} 的候选事件不能与基准事件相同（{cand.event_id}）")
            if cand.event_id in cand_seen:
                errs.append(f"关联组 {g.id} 的候选事件 {cand.event_id} 重复")
            cand_seen.add(cand.event_id)
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


# ----------------------------- 结果组装（可行时间线 / 约束余量） -----------------------------


def _build_timeline(
    intervals: list[EventIntervalInput],
    idx: dict[str, int],
    lower: list[float],
    upper: list[float],
    segment_candidates: Optional[dict[str, list[str]]] = None,
    assigned_segments: Optional[dict[str, str]] = None,
) -> list[EventWindow]:
    """由差分系统求得的每事件最早/最晚时刻组装统一时间线。"""
    segment_candidates = segment_candidates or {}
    assigned_segments = assigned_segments or {}
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
                segment_ids=segment_candidates.get(iv.event_id, iv.quantity.segment_ids),
                assigned_segment_id=assigned_segments.get(iv.event_id),
            )
        )
    return timeline


def _build_slack(
    edges: list[Edge],
    constraints: list[Constraint],
    lower: list[float],
    upper: list[float],
) -> list[ConstraintSlack]:
    """逐条用户约束（含关联求解生成的 same_event 约束）计算约束余量。"""
    slack_map = edge_slack(edges, lower, upper)
    slack_reports: list[ConstraintSlack] = []
    for c in constraints:
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
    return slack_reports


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
    seg_ids = sorted(
        {
            sid
            for eid in related_events
            for sid in iv_by_id[eid].quantity.segment_ids
        }
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
            **({"clock_segments": seg_ids} if seg_ids else {}),
        },
        segment_ids=seg_ids,
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


def reconcile(
    scenario: Scenario,
    budget_s: Optional[float] = None,
    assoc_top_k: int = 3,
    assoc_max_hypotheses: int = 100,
    assoc_max_search_nodes: int = 10_000,
    tz_max_search_nodes: int = 10_000,
) -> ReconcileResult:
    validate_scenario(scenario)
    warnings: list[str] = []
    try:
        models, reports, cw, fit_errors = build_clock_models(
            scenario.sources, scenario.anchors, scenario.events, scenario.default_utc_offset_s
        )
        legacy_intervals, ew = convert_events(
            scenario.sources, scenario.events, models, scenario.default_utc_offset_s
        )
    except (TimeParseError, ClockModelError) as exc:
        raise ScenarioValidationError(str(exc)) from exc
    if fit_errors:
        raise ScenarioValidationError("；".join(fit_errors))

    # IANA 时区歧义求解：声明了 iana_timezone 的来源，naive 读数展开为全部合法
    # UTC 候选，由差分约束选取可行 fold 组合；未声明时区的场景保持固定偏移行为。
    tz_report = None
    if any(s.iana_timezone for s in scenario.sources):
        from .timezones import run_timezone_resolution

        tz_report, tz_intervals, tzw = run_timezone_resolution(
            scenario, models, legacy_intervals, tz_max_search_nodes
        )
        warnings.extend(tzw)
        if tz_intervals is None:
            # 不存在的本地时间（春季跳时空洞）或全部 fold 组合均被约束排除：
            # 事件无法安置到统一时间轴，直接判不可行
            warnings.extend(cw)
            warnings.extend(ew)
            return ReconcileResult(
                scenario_name=scenario.name,
                feasible=False,
                clock_models=list(reports.values()),
                event_intervals=legacy_intervals,
                contradiction=tz_report.contradiction,
                timezone=tz_report,
                warnings=warnings,
            )
        # 代表 fold 组合下的区间替换名义区间，供后续差分求解/分段/关联复用
        legacy_intervals = tz_intervals

    # 分段时钟路径：声明了 clock_segments 时，分段来源的事件区间由分段模型接管，
    # 其余来源沿用单线性区间；未声明时行为与旧版本完全一致。
    if scenario.clock_segments:
        from .segments import run_segmentation

        try:
            seg_report, primary, nominal_best, sw = run_segmentation(
                scenario, legacy_intervals, models
            )
        except (TimeParseError, ClockModelError) as exc:
            raise ScenarioValidationError(str(exc)) from exc
        warnings.extend(cw)
        warnings.extend(ew)
        warnings.extend(sw)
        # 分段来源的单线性拟合类警告（漂移过大/无锚点等）以分段报告为准；
        # naive 时间戳解析警告（含“无时区”）保留
        seg_source_ids = set(seg_report.segmented_sources)
        deduped: list[str] = []
        for w in warnings:
            if (
                "无时区" not in w
                and any(w.startswith(f"来源 {sid} ") for sid in seg_source_ids)
            ):
                continue
            if w not in deduped:
                deduped.append(w)
        warnings = deduped

        chosen = seg_report.schemes[0] if seg_report.schemes else None
        # 归段依据：采用排名 1 方案的全部事件归属；模糊事件的多个可行归属另行收集
        candidate_map: dict[str, list[str]] = {}
        assigned_map: dict[str, str] = {}
        if chosen:
            for a in chosen.assignments:
                prefix = f"{a.source_id}:" if a.source_id else ""
                assigned_map[a.event_id] = f"{prefix}{a.assigned_segment_id}"
                if a.ambiguous:
                    candidate_map[a.event_id] = [
                        f"{prefix}{s}" for s in a.feasible_segment_ids
                    ]

        if primary is not None:
            intervals = primary
            n, idx, edges = build_graph(intervals, scenario.constraints)
            iv_by_id = {iv.event_id: iv for iv in intervals}
            ev_source: dict[int, Optional[str]] = {0: None}
            for eid, node in idx.items():
                ev_source[node] = iv_by_id[eid].source_id
            try:
                lower, upper = solve(n, edges)
            except DCSInfeasible as exc:
                # 理论上不应发生（搜索已校核），仍给出稳健回退
                contradiction = _contradiction(exc.cycle, idx, intervals, scenario.constraints)
                result = ReconcileResult(
                    scenario_name=scenario.name,
                    feasible=False,
                    clock_models=list(reports.values()),
                    event_intervals=intervals,
                    contradiction=contradiction,
                    timezone=tz_report,
                    segmentation=seg_report,
                    warnings=warnings,
                )
                _attach_association(
                    result, scenario, intervals,
                    assoc_top_k, assoc_max_hypotheses, assoc_max_search_nodes,
                )
                return result
            result = ReconcileResult(
                scenario_name=scenario.name,
                feasible=True,
                unified_timeline=_build_timeline(
                    intervals, idx, lower, upper,
                    segment_candidates=candidate_map or None,
                    assigned_segments=assigned_map,
                ),
                clock_models=list(reports.values()),
                event_intervals=intervals,
                constraint_slack=_build_slack(edges, scenario.constraints, lower, upper),
                timezone=tz_report,
                segmentation=seg_report,
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
            _attach_association(
                result, scenario, intervals,
                assoc_top_k, assoc_max_hypotheses, assoc_max_search_nodes,
            )
            return result

        # 所有分段方案都不可行：用排名最高方案的名义区间描述矛盾与调整建议
        intervals = nominal_best or legacy_intervals
        n, idx, edges = build_graph(intervals, scenario.constraints)
        iv_by_id = {iv.event_id: iv for iv in intervals}
        ev_source = {0: None}
        for eid, node in idx.items():
            ev_source[node] = iv_by_id[eid].source_id
        # 重新求解以提取负环（区间来自名义归段）
        contradiction: Optional[Contradiction] = chosen.contradiction if chosen else None
        try:
            solve(n, edges)
        except DCSInfeasible as exc:
            contradiction = _contradiction(exc.cycle, idx, intervals, scenario.constraints)
        result = ReconcileResult(
            scenario_name=scenario.name,
            feasible=False,
            clock_models=list(reports.values()),
            event_intervals=intervals,
            contradiction=contradiction,
            timezone=tz_report,
            segmentation=seg_report,
            warnings=warnings,
        )
        result.adjustment = _compute_adjustments(
            scenario, n, edges, intervals, scenario.constraints,
            ev_source, budget_s, currently_feasible=False,
        )
        for adj in result.adjustment.adjustments:
            if adj.source_id in reports:
                adj.clock_model_offset_s = reports[adj.source_id].offset_s.value
        _attach_association(
            result, scenario, intervals,
            assoc_top_k, assoc_max_hypotheses, assoc_max_search_nodes,
        )
        return result

    warnings.extend(cw)
    warnings.extend(ew)
    intervals = legacy_intervals
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
            timezone=tz_report,
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
        _attach_association(
            result, scenario, intervals,
            assoc_top_k, assoc_max_hypotheses, assoc_max_search_nodes,
        )
        return result

    # 可行：组装统一时间线与约束余量
    result = ReconcileResult(
        scenario_name=scenario.name,
        feasible=True,
        unified_timeline=_build_timeline(intervals, idx, lower, upper),
        clock_models=list(reports.values()),
        event_intervals=intervals,
        constraint_slack=_build_slack(edges, scenario.constraints, lower, upper),
        timezone=tz_report,
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
    _attach_association(
        result, scenario, intervals,
        assoc_top_k, assoc_max_hypotheses, assoc_max_search_nodes,
    )
    return result


def _attach_association(
    result: ReconcileResult,
    scenario: Scenario,
    intervals: list[EventIntervalInput],
    top_k: int,
    max_hypotheses: int,
    max_search_nodes: int,
) -> None:
    """声明了关联组时执行候选关联求解并挂到结果上；未声明则保持原行为。"""
    if not scenario.association_groups:
        return
    from .associate import solve_associations  # 延迟导入，避免与 engine 循环依赖

    result.association = solve_associations(
        intervals=intervals,
        constraints=scenario.constraints,
        groups=scenario.association_groups,
        top_k=top_k,
        max_hypotheses=max_hypotheses,
        max_search_nodes=max_search_nodes,
    )


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
    lag = {g.id: g for g in left_scenario.association_groups}
    rag = {g.id: g for g in right_scenario.association_groups}
    changed_groups = sorted(
        gid for gid in set(lag) & set(rag)
        if lag[gid].model_dump() != rag[gid].model_dump()
    )

    # ---- 分段时钟规则差异 ----
    from .models import SegmentBoundaryChange, SegmentBoundaryMove
    from .timescale import parse_unix as _parse_unix

    lseg = {r.source_id: r for r in left_scenario.clock_segments}
    rseg = {r.source_id: r for r in right_scenario.clock_segments}
    seg_only_left = sorted(set(lseg) - set(rseg))
    seg_only_right = sorted(set(rseg) - set(lseg))
    seg_rules_changed: list[str] = []
    boundary_changes: list[SegmentBoundaryChange] = []
    for sid in sorted(set(lseg) & set(rseg)):
        lr, rr = lseg[sid], rseg[sid]
        if lr.model_dump() != rr.model_dump():
            seg_rules_changed.append(sid)
        lsrc = next((s for s in left_scenario.sources if s.id == sid), None)
        rsrc = next((s for s in right_scenario.sources if s.id == sid), None)
        lb = {s.id: s for s in lr.segments}
        rb = {s.id: s for s in rr.segments}
        added = sorted(set(rb) - set(lb))
        removed = sorted(set(lb) - set(rb))
        moves: list[SegmentBoundaryMove] = []
        for bid in sorted(set(lb) & set(rb)):
            lseg_model, rseg_model = lb[bid], rb[bid]
            off = (lsrc.declared_utc_offset_s if lsrc else 0.0)
            roff = (rsrc.declared_utc_offset_s if rsrc else 0.0)
            lt = _parse_unix(lseg_model.start_clock_reading, off)[0]
            rt = _parse_unix(rseg_model.start_clock_reading, roff)[0]
            if (
                abs(rt - lt) > 1e-9
                or abs(lseg_model.boundary_uncertainty_s - rseg_model.boundary_uncertainty_s) > 1e-9
            ):
                moves.append(
                    SegmentBoundaryMove(
                        source_id=sid,
                        segment_id=bid,
                        left_clock_reading=lseg_model.start_clock_reading,
                        right_clock_reading=rseg_model.start_clock_reading,
                        delta_s=rt - lt,
                        left_boundary_uncertainty_s=lseg_model.boundary_uncertainty_s,
                        right_boundary_uncertainty_s=rseg_model.boundary_uncertainty_s,
                    )
                )
        threshold_changed = lr.jump_threshold_s != rr.jump_threshold_s or (
            left_scenario.auto_jump_threshold_s != right_scenario.auto_jump_threshold_s
            and lr.jump_threshold_s is None and rr.jump_threshold_s is None
        )
        if added or removed or moves or threshold_changed:
            boundary_changes.append(
                SegmentBoundaryChange(
                    source_id=sid,
                    segments_added=added,
                    segments_removed=removed,
                    moves=moves,
                    threshold_changed=threshold_changed,
                )
            )

    best_scheme_left = left.segmentation.best_scheme_key if left.segmentation else None
    best_scheme_right = right.segmentation.best_scheme_key if right.segmentation else None
    best_scheme_changed = (
        best_scheme_left is not None
        and best_scheme_right is not None
        and best_scheme_left != best_scheme_right
    )

    # ---- IANA 时区声明与 fold 解析差异 ----
    from .models import FoldDifference

    ltz = {s.id: s.iana_timezone for s in left_scenario.sources if s.iana_timezone}
    rtz = {s.id: s.iana_timezone for s in right_scenario.sources if s.iana_timezone}
    tz_only_left = sorted(set(ltz) - set(rtz))
    tz_only_right = sorted(set(rtz) - set(ltz))
    tz_decl_changed = sorted(k for k in set(ltz) & set(rtz) if ltz[k] != rtz[k])

    def _resolution_map(r: ReconcileResult) -> dict[str, tuple]:
        if r.timezone is None:
            return {}
        return {
            res.event_id: (
                res.iana_timezone,
                res.adopted_fold,
                res.adopted_utc_offset_s,
                res.adopted_unix_s,
            )
            for res in r.timezone.resolutions
        }

    lres, rres = _resolution_map(left), _resolution_map(right)
    fold_diffs: list[FoldDifference] = []
    for eid in sorted(set(lres) | set(rres)):
        l, r = lres.get(eid), rres.get(eid)
        if l is not None and r is not None and l == r:
            continue  # 两侧解析结论一致
        if l is None and r is None:
            continue
        # 一侧未声明时区（无解析记录）或两侧采用的 fold/偏移/UTC 不同
        if l is not None and r is not None and l[0] == r[0] and l[1:] == r[1:]:
            continue
        delta = (r[3] - l[3]) if (l is not None and r is not None and l[3] is not None and r[3] is not None) else None
        fold_diffs.append(
            FoldDifference(
                event_id=eid,
                left_iana_timezone=l[0] if l else None,
                right_iana_timezone=r[0] if r else None,
                left_fold=l[1] if l else None,
                right_fold=r[1] if r else None,
                left_utc_offset_s=l[2] if l else None,
                right_utc_offset_s=r[2] if r else None,
                left_unix_s=l[3] if l else None,
                right_unix_s=r[3] if r else None,
                delta_s=delta,
            )
        )

    def _assoc_summary(r: ReconcileResult) -> tuple[Optional[int], Optional[float]]:
        if r.association is None:
            return None, None
        best = (
            r.association.hypotheses[0].score.total_cost
            if r.association.hypotheses
            else None
        )
        return len(r.association.hypotheses), best

    hyp_l, cost_l = _assoc_summary(left)
    hyp_r, cost_r = _assoc_summary(right)
    return PlanDifference(
        left_ref=left_ref,
        right_ref=right_ref,
        events_only_left=sorted(set(le) - set(re)),
        events_only_right=sorted(set(re) - set(le)),
        constraints_only_left=sorted(lc - rc),
        constraints_only_right=sorted(rc - lc),
        sources_only_left=sorted(ls - rs),
        sources_only_right=sorted(rs - ls),
        association_groups_only_left=sorted(set(lag) - set(rag)),
        association_groups_only_right=sorted(set(rag) - set(lag)),
        association_groups_changed=changed_groups,
        hypotheses_left=hyp_l,
        hypotheses_right=hyp_r,
        best_cost_left=cost_l,
        best_cost_right=cost_r,
        segment_sources_only_left=seg_only_left,
        segment_sources_only_right=seg_only_right,
        segment_rules_changed=seg_rules_changed,
        segment_boundary_moves=boundary_changes,
        best_scheme_left=best_scheme_left,
        best_scheme_right=best_scheme_right,
        best_scheme_changed=best_scheme_changed,
        timezones_only_left=tz_only_left,
        timezones_only_right=tz_only_right,
        timezone_declarations_changed=tz_decl_changed,
        fold_differences=fold_diffs,
        event_window_deltas_s=deltas,
        feasible_left=left.feasible,
        feasible_right=right.feasible,
        min_budget_left_s=(left.adjustment.min_required_budget_s if left.adjustment else None),
        min_budget_right_s=(right.adjustment.min_required_budget_s if right.adjustment else None),
        explanation=(
            "event_window_deltas_s 中各量为右方案相对左方案的秒级差值（右-左）；"
            "宽度差为正表示右方案该事件的可行时刻范围更宽；"
            "association_groups_* 比较两侧声明的候选关联规则，"
            "hypotheses_*/best_cost_* 为各自关联求解返回的假设数与最优假设总代价；"
            "segment_sources_*/segment_rules_changed/segment_boundary_moves 比较两侧"
            "分段时钟规则的新增、删除与边界移动，best_scheme_* 为各自代表分段方案键"
            "（best_scheme_changed 表示跳变边界组合或最佳方案发生变化）；"
            "timezones_only_*/timezone_declarations_changed 比较两侧来源的 IANA 时区声明，"
            "fold_differences 列出两侧时区解析结论不同的事件（采用的 fold、UTC 偏移"
            "与 UTC 时刻差异，delta_s 为右-左秒级差）"
        ),
    )
