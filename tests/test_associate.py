"""候选事件关联求解测试：分支限界、剪枝、复用限制、排序、截断与接口集成。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.associate import solve_associations
from app.engine import ScenarioValidationError, reconcile
from app.models import (
    AssociationCandidate,
    AssociationGroup,
    Constraint,
    Event,
    Scenario,
    SourceClock,
)
from app.timescale import build_clock_models, convert_events


def make_scenario(groups, extra_events=(), constraints=()):
    """门禁 badge 08:00:00；相机抓拍 photo-n 在不同时刻。"""
    return Scenario(
        name="关联求解",
        sources=[
            SourceClock(id="door", declared_utc_offset_s=0, base_uncertainty_s=1.0),
            SourceClock(id="cam", declared_utc_offset_s=0, base_uncertainty_s=1.0),
        ],
        events=[
            Event(id="badge", source_id="door", reading="2026-09-14T08:00:00Z"),
            Event(id="photo1", source_id="cam", reading="2026-09-14T08:00:02Z"),
            Event(id="photo2", source_id="cam", reading="2026-09-14T08:00:04Z"),
            Event(id="photo3", source_id="cam", reading="2026-09-14T08:05:00Z"),
            *extra_events,
        ],
        constraints=list(constraints),
        association_groups=groups,
    )


def one_group(**kwargs):
    cand = kwargs.pop(
        "candidates",
        [
            AssociationCandidate(event_id="photo1", tolerance_s=5.0, cost=1.0),
            AssociationCandidate(event_id="photo2", tolerance_s=5.0, cost=2.0),
            AssociationCandidate(event_id="photo3", tolerance_s=5.0, cost=0.5),
        ],
    )
    return AssociationGroup(
        id=kwargs.pop("id", "g1"),
        base_event_id=kwargs.pop("base_event_id", "badge"),
        candidates=cand,
        **kwargs,
    )


# ----------------------------- 求解器行为 -----------------------------


def test_exactly_one_prunes_out_of_tolerance():
    """photo3 相差 300s 超出容差被剪枝；其余两个候选各自成假设，按代价排序。"""
    sc = make_scenario([one_group()])
    r = reconcile(sc, assoc_top_k=5)
    a = r.association
    assert a.status == "ok"
    assert a.total_feasible_found == 2
    assert a.returned_k == 2
    # 代价升序：photo1(1.0) 先于 photo2(2.0)
    assert [h.pairings[0].candidate_event_id for h in a.hypotheses] == ["photo1", "photo2"]
    assert [h.rank for h in a.hypotheses] == [1, 2]
    # 被剪枝的分支记在关联组头上，并带样本矛盾链
    elim = {g.group_id: g for g in a.eliminated_groups}
    assert elim["g1"].pruned_branches == 1
    assert elim["g1"].total_options == 3
    assert elim["g1"].sample_contradiction is not None
    assert a.stats.branches_pruned == 1
    assert not a.stats.truncated


def test_hypothesis_contents_traceable():
    """假设包含配对、统一时间线、约束余量与可追溯评分。"""
    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="photo1", tolerance_s=5.0, cost=1.5),
    ])])
    r = reconcile(sc)
    h = r.association.hypotheses[0]
    p = h.pairings[0]
    assert p.group_id == "g1" and p.base_event_id == "badge"
    assert p.candidate_event_id == "photo1"
    assert p.constraint_id == "assoc:g1"
    assert p.tolerance_s == 5.0 and p.cost == 1.5
    # 窗口相交（2s 差 < 5s 容差 + 半宽），时间残差为 0
    assert p.time_residual_s == 0.0
    # 评分可追溯
    assert h.score.total_cost == 1.5
    assert h.score.derived_by == "associate.cost_plus_window_gap"
    comp = h.score.components[0]
    assert comp.group_id == "g1" and comp.candidate_event_id == "photo1"
    # 统一时间线覆盖全部事件；约束余量包含生成的 assoc:g1
    assert len(h.unified_timeline) == 4
    slack_ids = {s.constraint_id for s in h.constraint_slack}
    assert "assoc:g1" in slack_ids
    assoc_slack = next(s for s in h.constraint_slack if s.constraint_id == "assoc:g1")
    assert assoc_slack.type == "same_event" and assoc_slack.satisfiable


def test_time_residual_tie_break():
    """代价相同按时间残差排序：photo2 与 badge 差 4s，窗口不相交 → 残差更大。"""
    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="photo2", tolerance_s=20.0, cost=1.0),
        AssociationCandidate(event_id="photo1", tolerance_s=20.0, cost=1.0),
    ])])
    r = reconcile(sc, assoc_top_k=2)
    a = r.association
    assert a.status == "ok" and a.returned_k == 2
    # photo1 残差 0（窗口相交）；photo2 残差 > 0（窗口间距 2s）
    assert a.hypotheses[0].pairings[0].candidate_event_id == "photo1"
    assert a.hypotheses[0].score.time_residual_s == 0.0
    assert a.hypotheses[1].pairings[0].candidate_event_id == "photo2"
    assert a.hypotheses[1].score.time_residual_s == pytest.approx(2.0, abs=1e-6)


def test_at_most_one_allows_skip():
    """候选全部超出容差时，at_most_one 产生“不选”的空配对假设。"""
    sc = make_scenario([one_group(
        mode="at_most_one",
        candidates=[AssociationCandidate(event_id="photo3", tolerance_s=5.0, cost=1.0)],
    )])
    r = reconcile(sc)
    a = r.association
    assert a.status == "ok"
    assert a.total_feasible_found == 1
    h = a.hypotheses[0]
    assert h.pairings == []
    assert h.score.total_cost == 0.0
    assert a.eliminated_groups[0].pruned_branches == 1


def test_cross_group_reuse_forbidden():
    """同一候选事件不能被两个关联组同时选中。"""
    groups = [
        AssociationGroup(
            id="g1", base_event_id="badge", mode="exactly_one",
            candidates=[AssociationCandidate(event_id="photo1", tolerance_s=5.0)],
        ),
        AssociationGroup(
            id="g2", base_event_id="photo2", mode="exactly_one",
            candidates=[AssociationCandidate(event_id="photo1", tolerance_s=20.0)],
        ),
    ]
    sc = make_scenario(groups)
    r = reconcile(sc)
    a = r.association
    # 唯一候选 photo1 被 g1 占用后 g2 无路可走 → 无解
    assert a.status == "infeasible"
    assert a.hypotheses == []
    assert a.stats.reuse_conflicts >= 1

    # 给 g2 增加自己的候选后可解，且两组选中不同事件
    groups[1].candidates.append(
        AssociationCandidate(event_id="photo3", tolerance_s=400.0, cost=3.0)
    )
    sc = make_scenario(groups)
    a = reconcile(sc, assoc_top_k=5).association
    assert a.status == "ok"
    for h in a.hypotheses:
        chosen = {p.candidate_event_id for p in h.pairings}
        assert len(chosen) == len(h.pairings)  # 无复用


def test_infeasible_reports_elimination_and_contradiction():
    """exactly_one 全部候选不可行：报告淘汰组、矛盾链与搜索统计。"""
    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="photo3", tolerance_s=5.0, cost=1.0),
    ])])
    r = reconcile(sc)
    a = r.association
    assert a.status == "infeasible"
    assert a.hypotheses == []
    assert a.eliminated_groups[0].group_id == "g1"
    contra = a.eliminated_groups[0].sample_contradiction
    assert contra is not None
    assert set(contra.cycle_event_ids) >= {"badge", "photo3"}
    assert "assoc:g1" in contra.cycle_constraint_ids
    assert a.contradiction is not None  # 代表性矛盾链
    assert a.stats.branches_pruned == 1
    assert a.stats.nodes_expanded >= 1


def test_node_limit_truncation():
    """搜索节点上限截断：报告截断原因与统计。"""
    sc = make_scenario([one_group()])
    r = reconcile(sc, assoc_top_k=5, assoc_max_search_nodes=1)
    a = r.association
    assert a.status == "truncated"
    assert a.stats.truncated and a.stats.truncation_reason == "node_limit"
    assert a.stats.nodes_expanded == 1


def test_hypothesis_limit_truncation():
    """可行假设收集上限截断。"""
    sc = make_scenario([one_group()])
    r = reconcile(sc, assoc_top_k=5, assoc_max_hypotheses=1)
    a = r.association
    assert a.status == "truncated"
    assert a.stats.truncation_reason == "hypothesis_limit"
    assert a.total_feasible_found == 1
    assert a.returned_k == 1


def test_top_k_limits_returned():
    sc = make_scenario([one_group()])
    r = reconcile(sc, assoc_top_k=1)
    a = r.association
    assert a.status == "ok"
    assert a.total_feasible_found == 2
    assert a.returned_k == 1
    assert a.hypotheses[0].pairings[0].candidate_event_id == "photo1"


def test_base_infeasible_short_circuits():
    """基础约束自身矛盾：关联求解直接报不可行并带基础矛盾链。"""
    sc = make_scenario(
        [one_group()],
        constraints=[Constraint(id="c-bad", type="before", a="photo3", b="badge")],
    )
    # badge 08:00:00±1、photo3 08:05:00±1：要求 photo3 先于 badge 与区间矛盾
    r = reconcile(sc)
    assert r.feasible is False
    a = r.association
    assert a.status == "infeasible"
    assert a.contradiction is not None
    assert "c-bad" in a.contradiction.cycle_constraint_ids
    assert a.stats.nodes_expanded == 0


def test_no_groups_keeps_legacy_behavior():
    """未声明关联组：association 为 null，其余字段与旧版一致。"""
    sc = make_scenario([], constraints=[
        Constraint(id="c-order", type="before", a="badge", b="photo1")
    ])
    r = reconcile(sc)
    assert r.feasible is True
    assert r.association is None
    assert len(r.unified_timeline) == 4
    assert r.constraint_slack[0].constraint_id == "c-order"


def test_group_branch_order_by_candidate_count():
    """按候选数优先分支：候选少的组先展开（根节点的第一层即最小组）。"""
    groups = [
        AssociationGroup(
            id="g-wide", base_event_id="badge", mode="at_most_one",
            candidates=[
                AssociationCandidate(event_id="photo1", tolerance_s=5.0),
                AssociationCandidate(event_id="photo2", tolerance_s=20.0),
            ],
        ),
        AssociationGroup(
            id="g-narrow", base_event_id="photo2", mode="exactly_one",
            candidates=[AssociationCandidate(event_id="photo3", tolerance_s=400.0)],
        ),
    ]
    sc = make_scenario(groups)
    a = reconcile(sc, assoc_top_k=10).association
    assert a.status == "ok"
    # 树规模：g-narrow 1 个选项先分支，g-wide 3 个选项（含跳过）在后
    # 节点数 = 1(根) + 1(g-narrow) + 3(g-wide) = 5
    assert a.stats.nodes_expanded == 5
    assert a.stats.groups_total == 2


def test_solve_associations_directly():
    """直接调用求解器（不经 reconcile）。"""
    sc = make_scenario([one_group()])
    models, _r, _w, errs = build_clock_models(sc.sources, sc.anchors, sc.events, 0.0)
    assert errs == []
    intervals, _ = convert_events(sc.sources, sc.events, models, 0.0)
    res = solve_associations(
        intervals=intervals, constraints=sc.constraints,
        groups=sc.association_groups, top_k=3,
    )
    assert res.status == "ok"
    assert res.method.startswith("associate.branch_and_bound")


# ----------------------------- 校验 -----------------------------


def test_validation_errors():
    sc = make_scenario([one_group()])
    sc.association_groups[0].base_event_id = "nope"
    with pytest.raises(ScenarioValidationError, match="基准事件 nope 不存在"):
        reconcile(sc)

    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="nope", tolerance_s=1.0)
    ])])
    with pytest.raises(ScenarioValidationError, match="候选事件 nope 不存在"):
        reconcile(sc)

    sc = make_scenario([one_group(candidates=[])])
    with pytest.raises(ScenarioValidationError, match="候选列表为空"):
        reconcile(sc)

    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="badge", tolerance_s=1.0)
    ])])
    with pytest.raises(ScenarioValidationError, match="不能与基准事件相同"):
        reconcile(sc)

    sc = make_scenario([one_group(candidates=[
        AssociationCandidate(event_id="photo1", tolerance_s=5.0),
        AssociationCandidate(event_id="photo1", tolerance_s=6.0),
    ])])
    with pytest.raises(ScenarioValidationError, match="重复"):
        reconcile(sc)


# ----------------------------- HTTP 接口集成 -----------------------------


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["FORENSIC_DB"] = tmp.name
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def api_payload(groups):
    return {
        "name": "门廊关联",
        "sources": [
            {"id": "door", "declared_utc_offset_s": 0, "base_uncertainty_s": 1.0},
            {"id": "cam", "declared_utc_offset_s": 0, "base_uncertainty_s": 1.0},
        ],
        "events": [
            {"id": "badge", "source_id": "door", "reading": "2026-09-14T08:00:00Z"},
            {"id": "photo1", "source_id": "cam", "reading": "2026-09-14T08:00:02Z"},
            {"id": "photo2", "source_id": "cam", "reading": "2026-09-14T08:00:04Z"},
        ],
        "association_groups": groups,
    }


def test_api_reconcile_with_association(client):
    payload = api_payload([
        {
            "id": "g1", "base_event_id": "badge", "mode": "exactly_one",
            "candidates": [
                {"event_id": "photo1", "tolerance_s": 5.0, "cost": 1.0},
                {"event_id": "photo2", "tolerance_s": 5.0, "cost": 2.0},
            ],
        }
    ])
    r = client.post("/reconcile", json=payload, params={"top_k": 1})
    assert r.status_code == 200, r.text
    a = r.json()["association"]
    assert a["status"] == "ok"
    assert a["returned_k"] == 1 and a["total_feasible_found"] == 2
    h = a["hypotheses"][0]
    assert h["pairings"][0]["candidate_event_id"] == "photo1"
    assert h["unified_timeline"] and h["constraint_slack"]
    assert h["score"]["components"]
    assert a["stats"]["max_search_nodes"] == 10000

    # 旧请求（无关联组）：association 为 null
    r = client.post("/reconcile", json=api_payload([]))
    assert r.status_code == 200
    assert r.json()["association"] is None


def test_api_validation_400(client):
    payload = api_payload([
        {"id": "g1", "base_event_id": "ghost", "mode": "exactly_one",
         "candidates": [{"event_id": "photo1", "tolerance_s": 5.0}]}
    ])
    r = client.post("/reconcile", json=payload)
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_scenario_versions_keep_association_rules(client):
    """场景版本保存关联规则；校核已存版本时生效。"""
    payload = api_payload([
        {"id": "g1", "base_event_id": "badge", "mode": "exactly_one",
         "candidates": [{"event_id": "photo1", "tolerance_s": 5.0, "cost": 1.0}]}
    ])
    r = client.post("/scenarios", json={"scenario": payload, "note": "含关联规则"})
    assert r.status_code == 200, r.text
    sid = r.json()["scenario_id"]

    got = client.get(f"/scenarios/{sid}").json()
    assert got["payload"]["association_groups"][0]["id"] == "g1"
    assert got["payload"]["association_groups"][0]["candidates"][0]["event_id"] == "photo1"

    r = client.post(f"/scenarios/{sid}/reconcile")
    assert r.status_code == 200
    assert r.json()["association"]["status"] == "ok"
    assert r.json()["association"]["hypotheses"][0]["pairings"][0]["constraint_id"] == "assoc:g1"


def test_compare_association_rules(client):
    """/compare 比较两侧关联规则：仅一侧有的组、规则变化的组、最优代价。"""
    left = api_payload([
        {"id": "g1", "base_event_id": "badge", "mode": "exactly_one",
         "candidates": [{"event_id": "photo1", "tolerance_s": 5.0, "cost": 1.0}]}
    ])
    right = api_payload([
        {"id": "g1", "base_event_id": "badge", "mode": "exactly_one",
         "candidates": [{"event_id": "photo1", "tolerance_s": 5.0, "cost": 9.0}]},
        {"id": "g2", "base_event_id": "photo2", "mode": "at_most_one",
         "candidates": [{"event_id": "photo1", "tolerance_s": 20.0}]},
    ])
    r = client.post("/compare", json={"left": left, "right": right})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["association_groups_only_left"] == []
    assert d["association_groups_only_right"] == ["g2"]
    assert d["association_groups_changed"] == ["g1"]  # 代价 1.0 → 9.0
    assert d["hypotheses_left"] == 1
    assert d["best_cost_left"] == 1.0
    assert d["best_cost_right"] is not None
