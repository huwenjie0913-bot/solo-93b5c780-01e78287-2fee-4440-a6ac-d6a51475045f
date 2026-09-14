"""差分约束系统（Difference Constraint System）。

约束统一写成形如

    x_v - x_u ≤ w

的有向边 u→v（权 w）。加入固定节点 x_0 = 0（绝对历元）后：

* 可行性与矛盾检测：以全零初始距离（等价于超级源到所有节点连 0 边）跑
  Bellman-Ford，若第 N 轮仍可松弛，存在负环——负环就是无法同时成立的
  “最小矛盾链”；
* 上界：从节点 0 出发的最短路 dist[v] 给出 x_v ≤ dist[v]，即最晚时刻；
* 下界：把图取反（边 v→u 权 w，变量 y=-x）后从 0 跑最短路，
  得 x_v ≥ -dist'[v]，即最早时刻；
* 余量：边 (u,v,w) 的余量为 w - (x_v^max - x_u^min)，≥0，为 0 即紧约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Edge:
    u: int
    v: int
    w: float
    ref: str  # 原始记录 ID：事件 ID（一元边）或约束 ID
    kind: str  # "upper"(x≤hi) / "lower"(x≥lo) / "constraint"


@dataclass
class Cycle:
    nodes: list[int]  # 环上的节点，nodes[-1] == nodes[0]
    edges: list[Edge]  # 闭合该环依次经过的边
    total_weight: float


class DCSInfeasible(Exception):
    def __init__(self, cycle: Cycle):
        self.cycle = cycle
        super().__init__(f"差分约束系统存在负环，总权重 {cycle.total_weight:g}")


def _negate_graph(edges: list[Edge]) -> list[Edge]:
    """图取反：原边 u→v(w) 变为 v→u(w)，用于在 y=-x 上求下界。"""
    return [Edge(e.v, e.u, e.w, e.ref, e.kind) for e in edges]


def _bellman_ford(
    n: int,
    edges: list[Edge],
    start: Optional[int],
) -> tuple[list[float], list[Optional[int]], list[Optional[Edge]], Optional[int]]:
    """返回 (dist, pred_node, pred_edge, 第 N 轮仍被松弛的节点)。

    start=None 时所有节点距离初始化为 0（超级源模式，用于可行性/负环检测）；
    否则 dist[start]=0，其余为 +inf（上下界模式）。
    """
    if start is None:
        dist = [0.0] * n
    else:
        dist = [float("inf")] * n
        dist[start] = 0.0
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
    return dist, pred, pred_edge, updated


def extract_cycle(
    n: int, pred: list[Optional[int]], pred_edge: list[Optional[Edge]], bad: int
) -> Cycle:
    """从“第 N 轮被松弛的节点”沿前驱回溯，提取负环。"""
    # 沿前驱走 n 步，必然落在环上
    node = bad
    for _ in range(n):
        p = pred[node]
        if p is None:
            break
        node = p
    start = node
    cyc_edges: list[Edge] = []
    seen = {start}
    cur = start
    while True:
        e = pred_edge[cur]
        p = pred[cur]
        if e is None or p is None:
            break
        cyc_edges.append(e)
        cur = p
        if cur == start:
            break
        if cur in seen:  # 理论上不会发生，防御性处理
            break
        seen.add(cur)
    ordered_edges: list[Edge] = list(reversed(cyc_edges))
    # 正向走：start → 第一条边的终点 → … → start
    ordered_nodes = [start]
    for e in ordered_edges:
        ordered_nodes.append(e.v)
    total = sum(e.w for e in ordered_edges)
    return Cycle(nodes=ordered_nodes, edges=ordered_edges, total_weight=total)


def solve(
    n: int, edges: list[Edge], origin: int = 0
) -> tuple[list[float], list[float]]:
    """求可行解与每个变量的最小/最大值。

    返回 (lower, upper)（每个节点的最早/最晚取值）。不可行时抛出
    DCSInfeasible，携带最小矛盾负环。
    """
    _dist, pred, pred_edge, bad = _bellman_ford(n, edges, start=None)
    if bad is not None:
        raise DCSInfeasible(extract_cycle(n, pred, pred_edge, bad))

    upper, _, _, _ = _bellman_ford(n, edges, start=origin)
    neg_edges = _negate_graph(edges)
    neg_dist, _, _, _ = _bellman_ford(n, neg_edges, start=origin)
    # neg_dist[v] 是 y_v=-x_v 的上界，故 x_v ≥ -neg_dist[v]
    lower = [-d if d != float("inf") else float("-inf") for d in neg_dist]
    return lower, upper


def edge_slack(
    edges: list[Edge], lower: list[float], upper: list[float], origin: int = 0
) -> dict[str, list[tuple[Edge, float]]]:
    """按 ref 分组返回每条原始边的约束余量（秒）。

    边 u→v(w) 的余量 = w - (x_v^max - x_u^min)。
    """
    out: dict[str, list[tuple[Edge, float]]] = {}
    for e in edges:
        head_room = e.w - (upper[e.v] - lower[e.u])
        out.setdefault(e.ref, []).append((e, head_room))
    return out
