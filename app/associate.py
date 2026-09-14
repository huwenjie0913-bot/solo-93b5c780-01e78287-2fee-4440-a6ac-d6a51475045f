"""候选事件关联求解：分支限界 + 差分约束剪枝。

门禁刷卡、摄像头抓拍、设备报警等记录往往只能按时间窗推断是否属于同一件事；
把“确定的 same_event 对”写死容易引入错误配对。本模块允许为基准事件声明
一组候选事件（匹配容差 + 可选代价），由求解器在以下规则下搜索可行配对：

* 关联组基数：``exactly_one`` 必须选中一个候选，``at_most_one`` 可选或跳过；
* 候选事件是全局资源：同一候选事件不可被多个关联组同时选中（不可跨组复用）；
* 分支顺序：按各组可选项数升序（候选数优先，``at_most_one`` 的跳过项计入），
  声明顺序作为并列时的稳定次序；组内按 (代价, 事件 ID) 排序，
  ``at_most_one`` 的跳过项排在最前；
* 剪枝：每选中一个候选即把对应的 same_event 差分边（|t_base − t_cand| ≤ 容差）
  加入图，复用 dcs 的 Bellman-Ford 可行性校核，负环即剪枝并提取矛盾链；
* 上限：``max_search_nodes`` 限制展开节点数，``max_hypotheses`` 限制收集的
  可行假设数；达到任一上限即截断并在统计中说明原因；
* 排序：可行假设按 (总代价, 时间残差, 发现序) 稳定排序，返回前 ``top_k`` 个。
  时间残差 = 该配对两事件在统一时间线可行窗口下的最小间距（0 表示窗口相交）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .dcs import Edge, _bellman_ford, extract_cycle, solve
from .engine import _build_slack, _build_timeline, _contradiction, build_graph
from .models import (
    AssociationGroup,
    AssociationHypothesis,
    AssociationPairing,
    AssociationResult,
    AssociationSearchStats,
    Constraint,
    EventIntervalInput,
    GroupElimination,
    HypothesisScore,
    ScoreComponent,
)

ASSOC_CONSTRAINT_PREFIX = "assoc:"

_METHOD = (
    "associate.branch_and_bound：按可选项数升序（候选数优先）确定关联组分支顺序，"
    "组内按 (代价, 事件 ID) 排序；每选一个候选即加入 same_event 差分边并用 "
    "Bellman-Ford 可行性校核剪枝（负环即矛盾链）；候选事件全局不可跨组复用；"
    "可行假设按 (总代价, 时间残差, 发现序) 稳定排序取前 K"
)


@dataclass(frozen=True)
class _Option:
    """关联组的一个可选项；event_id 为 None 表示 at_most_one 的“跳过”。"""

    event_id: Optional[str]
    tolerance_s: float = 0.0
    cost: float = 0.0


def _group_options(group: AssociationGroup) -> list[_Option]:
    opts = [_Option(c.event_id, c.tolerance_s, c.cost) for c in group.candidates]
    opts.sort(key=lambda o: (o.cost, o.event_id or ""))
    if group.mode == "at_most_one":
        opts.insert(0, _Option(None))
    return opts


def _pair_edges(group: AssociationGroup, opt: _Option, idx: dict[str, int]) -> list[Edge]:
    """选中配对对应的 same_event 差分边：|t_base − t_cand| ≤ 容差。"""
    ia, ib = idx[group.base_event_id], idx[opt.event_id]
    cid = ASSOC_CONSTRAINT_PREFIX + group.id
    return [
        Edge(ib, ia, opt.tolerance_s, cid, "constraint"),
        Edge(ia, ib, opt.tolerance_s, cid, "constraint"),
    ]


def _synthetic_constraints(
    groups: list[AssociationGroup],
    assignment: list[Optional[_Option]],
    extra: Optional[tuple[AssociationGroup, _Option]] = None,
) -> list[Constraint]:
    """把当前已选配对翻译成 same_event 约束（用于余量报告与矛盾链追溯）。"""
    out: list[Constraint] = []
    pairs = list(zip(groups, assignment))
    if extra is not None:
        pairs.append(extra)
    for g, opt in pairs:
        if opt is None or opt.event_id is None:
            continue
        out.append(
            Constraint(
                id=ASSOC_CONSTRAINT_PREFIX + g.id,
                type="same_event",
                a=g.base_event_id,
                b=opt.event_id,
                tolerance_s=opt.tolerance_s,
                note=f"关联组 {g.id} 选中的候选配对",
            )
        )
    return out


def solve_associations(
    *,
    intervals: list[EventIntervalInput],
    constraints: list[Constraint],
    groups: list[AssociationGroup],
    top_k: int = 3,
    max_hypotheses: int = 100,
    max_search_nodes: int = 10_000,
) -> AssociationResult:
    """在差分约束校核下搜索候选事件配对，返回前 ``top_k`` 个可行假设。"""
    n, idx, base_edges = build_graph(intervals, constraints)
    options_by_group = [_group_options(g) for g in groups]
    # 候选数优先：可选项数最少的组先分支；声明顺序作稳定次序
    order = sorted(range(len(groups)), key=lambda i: (len(options_by_group[i]), i))

    stats = {
        "nodes_expanded": 0,
        "branches_pruned": 0,
        "reuse_conflicts": 0,
        "truncated": False,
        "reason": None,
    }
    hypotheses: list[dict] = []  # 发现序的可行假设内部记录
    elim: dict[str, dict] = {}  # group_id -> {"pruned": int, "contra": Contradiction}
    rep: dict[str, object] = {"contra": None, "depth": -1}  # 最深剪枝处的代表性矛盾链

    # 基础场景（不含任何配对）先校核一次：若已不可行，任何假设都不可能成立
    _d, pred, pred_edge, bad = _bellman_ford(n, base_edges, start=None)
    if bad is not None:
        cycle = extract_cycle(n, pred, pred_edge, bad)
        contra = _contradiction(cycle, idx, intervals, list(constraints))
        return AssociationResult(
            status="infeasible",
            hypotheses=[],
            total_feasible_found=0,
            returned_k=0,
            eliminated_groups=[],
            contradiction=contra,
            stats=AssociationSearchStats(
                groups_total=len(groups),
                nodes_expanded=0,
                branches_pruned=0,
                reuse_conflicts=0,
                leaves_feasible=0,
                top_k=top_k,
                max_hypotheses=max_hypotheses,
                max_search_nodes=max_search_nodes,
                truncated=False,
                truncation_reason=None,
            ),
            method=_METHOD + "；基础场景（不含配对）已不可行，未展开搜索",
        )

    assignment: list[Optional[_Option]] = [None] * len(groups)
    used: set[str] = set()

    def record_prune(
        level: int,
        gi: int,
        opt: _Option,
        pred: list[Optional[int]],
        pred_edge: list[Optional[Edge]],
        bad_node: int,
    ) -> None:
        stats["branches_pruned"] += 1
        cycle = extract_cycle(n, pred, pred_edge, bad_node)
        synth = _synthetic_constraints(groups, assignment, extra=(groups[gi], opt))
        contra = _contradiction(cycle, idx, intervals, list(constraints) + synth)
        rec = elim.setdefault(groups[gi].id, {"pruned": 0, "contra": None})
        rec["pruned"] += 1
        if rec["contra"] is None:
            rec["contra"] = contra
        if level > rep["depth"]:
            rep["depth"] = level
            rep["contra"] = contra

    def record_leaf(edges: list[Edge]) -> None:
        lower, upper = solve(n, edges)
        synth = _synthetic_constraints(groups, assignment)
        pairings: list[AssociationPairing] = []
        components: list[ScoreComponent] = []
        total_cost = 0.0
        total_resid = 0.0
        for g, opt in zip(groups, assignment):
            if opt is None or opt.event_id is None:
                continue
            lo_b, hi_b = lower[idx[g.base_event_id]], upper[idx[g.base_event_id]]
            lo_c, hi_c = lower[idx[opt.event_id]], upper[idx[opt.event_id]]
            resid = max(0.0, lo_b - hi_c, lo_c - hi_b)
            pairings.append(
                AssociationPairing(
                    group_id=g.id,
                    base_event_id=g.base_event_id,
                    candidate_event_id=opt.event_id,
                    constraint_id=ASSOC_CONSTRAINT_PREFIX + g.id,
                    tolerance_s=opt.tolerance_s,
                    cost=opt.cost,
                    time_residual_s=resid,
                )
            )
            components.append(
                ScoreComponent(
                    group_id=g.id,
                    candidate_event_id=opt.event_id,
                    cost=opt.cost,
                    time_residual_s=resid,
                )
            )
            total_cost += opt.cost
            total_resid += resid
        hypotheses.append(
            {
                "pairings": pairings,
                "score": HypothesisScore(
                    total_cost=total_cost,
                    time_residual_s=total_resid,
                    components=components,
                    derived_by="associate.cost_plus_window_gap",
                    detail=(
                        "总代价=Σ 选中候选的代价；时间残差=Σ 配对两事件在统一时间线"
                        "可行窗口下的最小间距（0 表示窗口相交）"
                    ),
                ),
                "timeline": _build_timeline(intervals, idx, lower, upper),
                "slack": _build_slack(edges, list(constraints) + synth, lower, upper),
            }
        )
        if len(hypotheses) >= max_hypotheses:
            stats["truncated"] = True
            stats["reason"] = "hypothesis_limit"

    def dfs(level: int, edges: list[Edge]) -> None:
        if stats["truncated"]:
            return
        if stats["nodes_expanded"] >= max_search_nodes:
            stats["truncated"] = True
            stats["reason"] = "node_limit"
            return
        stats["nodes_expanded"] += 1
        if level == len(order):
            record_leaf(edges)
            return
        gi = order[level]
        group = groups[gi]
        for opt in options_by_group[gi]:
            if stats["truncated"]:
                return
            if opt.event_id is None:  # at_most_one 的跳过项
                dfs(level + 1, edges)
                continue
            if opt.event_id in used:
                stats["reuse_conflicts"] += 1
                continue
            new_edges = edges + _pair_edges(group, opt, idx)
            _dd, p, pe, bad_node = _bellman_ford(n, new_edges, start=None)
            if bad_node is not None:
                record_prune(level, gi, opt, p, pe, bad_node)
                continue
            assignment[gi] = opt
            used.add(opt.event_id)
            dfs(level + 1, new_edges)
            used.discard(opt.event_id)
            assignment[gi] = None

    dfs(0, base_edges)

    # 稳定排序：总代价 → 时间残差 → 发现序
    ranked = sorted(
        enumerate(hypotheses),
        key=lambda t: (t[1]["score"].total_cost, t[1]["score"].time_residual_s, t[0]),
    )
    top: list[AssociationHypothesis] = []
    for rank, (_disc, h) in enumerate(ranked[: max(top_k, 0)], start=1):
        top.append(
            AssociationHypothesis(
                rank=rank,
                pairings=h["pairings"],
                score=h["score"],
                unified_timeline=h["timeline"],
                constraint_slack=h["slack"],
            )
        )

    eliminated = [
        GroupElimination(
            group_id=g.id,
            base_event_id=g.base_event_id,
            pruned_branches=elim[g.id]["pruned"],
            total_options=len(options_by_group[gi]),
            sample_contradiction=elim[g.id]["contra"],
        )
        for gi, g in enumerate(groups)
        if g.id in elim
    ]

    if stats["truncated"]:
        status = "truncated"
    elif hypotheses:
        status = "ok"
    else:
        status = "infeasible"

    return AssociationResult(
        status=status,
        hypotheses=top,
        total_feasible_found=len(hypotheses),
        returned_k=len(top),
        eliminated_groups=eliminated,
        contradiction=rep["contra"],
        stats=AssociationSearchStats(
            groups_total=len(groups),
            nodes_expanded=stats["nodes_expanded"],
            branches_pruned=stats["branches_pruned"],
            reuse_conflicts=stats["reuse_conflicts"],
            leaves_feasible=len(hypotheses),
            top_k=top_k,
            max_hypotheses=max_hypotheses,
            max_search_nodes=max_search_nodes,
            truncated=stats["truncated"],
            truncation_reason=stats["reason"],
        ),
        method=_METHOD,
    )
