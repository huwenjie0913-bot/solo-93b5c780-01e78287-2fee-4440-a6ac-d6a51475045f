"""分段时钟模型测试：显式边界、段 ID 唯一性、边界模糊多归属、自动检测与回退。"""

import pytest

from app.engine import ScenarioValidationError, diff_plans, reconcile
from app.models import (
    CalibrationAnchor,
    ClockSegment,
    Constraint,
    Event,
    Scenario,
    SourceClock,
    SourceSegmentation,
)
from app.timescale import parse_unix

_EPOCH = "2026-09-13T00:00:00Z"
_DAY = 86400


def iso(offset_s: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(seconds=offset_s)
    ).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def backward_jump_scenario(boundary_uncertainty_s: float = 60.0) -> Scenario:
    """dev 在 day1 00:00 重启、钟被向后拨 600s；e-edge 落在边界 ±60s 模糊区。

    初始段锚点（钟面/真值都在边界钟面 86400 之前）：
        a0: 0 -> 0
        a1: day0 12:00 -> day0 12:00
    重启段锚点（钟面比真值慢 600s，钟面已越过边界）：
        a2: day2 00:00 - 600(钟面) -> day2 00:00(真值)
    """
    return Scenario(
        name="边界模糊",
        sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
        anchors=[
            CalibrationAnchor(id="a0", source_id="dev",
                              clock_reading=iso(0), reference_time=iso(0)),
            CalibrationAnchor(id="a1", source_id="dev",
                              clock_reading=iso(43200), reference_time=iso(43200)),
            CalibrationAnchor(id="a2", source_id="dev",
                              clock_reading=iso(2 * _DAY - 600),
                              reference_time=iso(2 * _DAY)),
        ],
        events=[
            Event(id="e-ref", source_id=None, reading=iso(_DAY + 50)),
            # 钟面 day1 00:00:30，恰在边界 day1 00:00 ±60s 内
            Event(id="e-edge", source_id="dev", reading=iso(_DAY + 30)),
        ],
        constraints=[Constraint(id="c-order", type="before", a="e-edge", b="e-ref")],
        clock_segments=[
            SourceSegmentation(
                source_id="dev",
                segments=[
                    ClockSegment(
                        id="seg-boot",
                        start_clock_reading=iso(_DAY),
                        boundary_uncertainty_s=boundary_uncertainty_s,
                    )
                ],
            )
        ],
    )


def test_explicit_single_boundary_distinct_segment_ids():
    """回归：只声明 seg-boot 边界时，初始段不得也叫 seg-boot。"""
    sc = backward_jump_scenario()
    result = reconcile(sc)
    assert result.feasible, result.contradiction
    assert result.segmentation is not None
    chosen = result.segmentation.schemes[0]
    seg_ids = [s.segment_id for s in chosen.segments]
    assert seg_ids == ["seg-initial", "seg-boot"]
    # 两个物理段分别独立拟合：初始段偏移 0、重启段偏移 -600s
    by_id = {s.segment_id: s for s in chosen.segments}
    assert by_id["seg-initial"].offset_s.value == pytest.approx(0.0)
    assert by_id["seg-boot"].offset_s.value == pytest.approx(-600.0)
    jump = chosen.jumps[0]
    assert jump.boundary_segment_id == "seg-boot"
    assert jump.origin == "declared"
    assert jump.jump_s == pytest.approx(-600.0)


def test_boundary_event_keeps_two_distinct_assignments_and_feasible():
    """边界事件必须保留两个不同归属，求解器选让约束成立的初始段。"""
    sc = backward_jump_scenario()
    result = reconcile(sc)
    assert result.feasible
    chosen = result.segmentation.schemes[0]
    edge = next(a for a in chosen.assignments if a.event_id == "e-edge")
    assert edge.ambiguous
    assert set(edge.feasible_segment_ids) == {"seg-initial", "seg-boot"}
    assert edge.assigned_segment_id == "seg-initial"

    windows = {w.earliest_unix_s.event_ids[0]: w for w in result.unified_timeline}
    win = windows["e-edge"]
    # 时间线上同样列出两个不同归属 GID，并标出代表解采用的段
    assert set(win.segment_ids) == {"dev:seg-initial", "dev:seg-boot"}
    assert win.assigned_segment_id == "dev:seg-initial"

    # 归初始段时真值中心 ≈ day1+30（先于 e-ref 的 day1+50）
    t0 = parse_unix(_EPOCH)[0]
    center = (win.earliest_unix_s.value + win.latest_unix_s.value) / 2
    assert center == pytest.approx(t0 + _DAY + 30, abs=0.01)


def test_boundary_event_other_assignment_chosen_when_constraints_reverse():
    """反向约束下同一模糊事件应改归重启段（真值 day1+630）。"""
    sc = backward_jump_scenario()
    sc.constraints = [Constraint(id="c-rev", type="before", a="e-ref", b="e-edge")]
    result = reconcile(sc)
    assert result.feasible
    edge = next(
        a for a in result.segmentation.schemes[0].assignments
        if a.event_id == "e-edge"
    )
    assert edge.assigned_segment_id == "seg-boot"
    win = {w.earliest_unix_s.event_ids[0]: w
           for w in result.unified_timeline}["e-edge"]
    t0 = parse_unix(_EPOCH)[0]
    center = (win.earliest_unix_s.value + win.latest_unix_s.value) / 2
    assert center == pytest.approx(t0 + _DAY + 630, abs=0.01)


def test_both_assignments_infeasible_flags_segments_in_contradiction():
    """两个归属都无法满足约束时，矛盾链要指出涉及的段与锚点。"""
    sc = backward_jump_scenario()
    sc.constraints = [
        Constraint(id="c-big", type="min_interval", a="e-edge", b="e-ref",
                   min_s=100_000.0)
    ]
    result = reconcile(sc)
    assert not result.feasible
    assert result.segmentation.status == "infeasible"
    con = result.contradiction
    assert con is not None
    seg_refs = set(con.related_record_ids.get("clock_segments", []))
    assert seg_refs, con
    assert any(g.startswith("dev:") for g in seg_refs)
    assert con.related_record_ids.get("anchors")
    # 所有枚举方案（显式边界只有一个）都不可行
    assert all(not sch.feasible for sch in result.segmentation.schemes)


def test_zero_uncertainty_boundary_is_unambiguous():
    """边界不确定半宽为 0 时，边界事件不产生多归属（按钟面读数唯一段）。"""
    sc = backward_jump_scenario(boundary_uncertainty_s=0.0)
    result = reconcile(sc)
    edge = next(
        a for a in result.segmentation.schemes[0].assignments
        if a.event_id == "e-edge"
    )
    # 钟面 86430 >= 边界 86400，唯一归属重启段；无初始段归属可选，
    # 故“e-edge 先于 e-ref（day1+50）”的约束无法满足
    assert edge.feasible_segment_ids == ["seg-boot"]
    assert not edge.ambiguous
    assert not result.feasible


def test_auto_segmentation_detects_forward_jump():
    """自动检测：day1 后钟被向前拨 300s，最佳方案应识别单一跳变、残差 0。"""
    anchors = [
        CalibrationAnchor(id="a0", source_id="dev",
                          clock_reading=iso(0), reference_time=iso(0)),
        CalibrationAnchor(id="a1", source_id="dev",
                          clock_reading=iso(_DAY), reference_time=iso(_DAY)),
        CalibrationAnchor(id="a2", source_id="dev",
                          clock_reading=iso(2 * _DAY + 300),
                          reference_time=iso(2 * _DAY)),
        CalibrationAnchor(id="a3", source_id="dev",
                          clock_reading=iso(3 * _DAY + 300),
                          reference_time=iso(3 * _DAY)),
    ]
    sc = Scenario(
        name="自动跳变",
        sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
        anchors=anchors,
        events=[
            Event(id="e-before", source_id="dev", reading=iso(_DAY + 100)),
            Event(id="e-after", source_id="dev", reading=iso(2 * _DAY + 400)),
        ],
        constraints=[Constraint(id="c-order", type="before",
                                 a="e-before", b="e-after")],
        clock_segments=[
            SourceSegmentation(source_id="dev", jump_threshold_s=60.0)
        ],
    )
    result = reconcile(sc)
    assert result.feasible
    detected = {(d.between_anchor_ids): d for d in result.segmentation.detected_jumps}
    assert ("a1", "a2") in detected
    assert detected[("a1", "a2")].estimated_jump_s == pytest.approx(300.0)
    chosen = result.segmentation.schemes[0]
    assert chosen.fit_score_s == pytest.approx(0.0, abs=1e-6)
    assert len(chosen.segments) == 2
    jump = chosen.jumps[0]
    assert jump.jump_s == pytest.approx(300.0)
    assert jump.origin == "auto"
    # 自动段 ID 与隐式初始段 ID 不同
    assert chosen.segments[0].segment_id == "seg-initial"
    assert chosen.segments[1].segment_id.startswith("seg-auto-")
    # 反演真值：e-after 钟面 day2+400 对应真值 day2+100
    t0 = parse_unix(_EPOCH)[0]
    win = {w.earliest_unix_s.event_ids[0]: w for w in result.unified_timeline}
    center = (win["e-after"].earliest_unix_s.value + win["e-after"].latest_unix_s.value) / 2
    assert center == pytest.approx(t0 + 2 * _DAY + 100, abs=0.01)


def test_no_segment_config_uses_single_linear_model():
    """未提供分段配置：segmentation 为 null，单线性行为完全不变。"""
    sc = Scenario(
        name="单线性",
        sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
        anchors=[
            CalibrationAnchor(id="a0", source_id="dev",
                              clock_reading=iso(0), reference_time=iso(0)),
        ],
        events=[Event(id="e1", source_id="dev", reading=iso(100))],
    )
    result = reconcile(sc)
    assert result.feasible
    assert result.segmentation is None
    assert len(result.clock_models) == 1
    win = result.unified_timeline[0]
    assert win.segment_ids == []
    assert win.assigned_segment_id is None


def test_segment_validation_errors():
    sc = backward_jump_scenario()
    # 引用不存在的来源
    sc.clock_segments[0].source_id = "nope"
    with pytest.raises(ScenarioValidationError, match="不存在的来源"):
        reconcile(sc)

    # 既无显式段也无阈值
    sc = backward_jump_scenario()
    sc.clock_segments[0] = SourceSegmentation(source_id="dev")
    with pytest.raises(ScenarioValidationError, match="必须显式声明时钟段"):
        reconcile(sc)


def test_segmentation_roundtrip_and_compare():
    """段规则随场景版本存取，/compare 识别边界移动与最佳方案。"""
    import os
    import tempfile

    from fastapi.testclient import TestClient

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["FORENSIC_DB"] = tmp.name
    import importlib

    import app.main as main
    importlib.reload(main)

    try:
        with TestClient(main.app) as c:
            sc = backward_jump_scenario()
            r = c.post("/scenarios", json={"scenario": sc.model_dump()})
            assert r.status_code == 200, r.text
            sid = r.json()["scenario_id"]
            r = c.post(f"/scenarios/{sid}/reconcile")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["feasible"] is True
            seg_ids = [s["segment_id"] for s in data["segmentation"]["schemes"][0]["segments"]]
            assert seg_ids == ["seg-initial", "seg-boot"]

            # 左方案：边界钟面 day0 23:00（-3600s）；右方案：day1 00:00
            sc_left = sc.model_dump()
            sc_left["clock_segments"][0]["segments"][0]["start_clock_reading"] = iso(_DAY - 3600)
            r = c.post(
                "/compare",
                json={"left": sc_left, "right": sc.model_dump()},
            )
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["segment_rules_changed"] == ["dev"]
            change = next(
                ch for ch in d["segment_boundary_moves"] if ch["source_id"] == "dev"
            )
            assert change["moves"][0]["segment_id"] == "seg-boot"
            assert change["moves"][0]["delta_s"] == pytest.approx(3600.0)
            assert d["best_scheme_left"] and d["best_scheme_right"]
    finally:
        os.unlink(tmp.name)


def test_compare_detects_segment_rule_addition_and_best_scheme_change():
    base = backward_jump_scenario()
    left = base.model_copy(deep=True)
    left.clock_segments = []
    right = base
    rl, rr = reconcile(left), reconcile(right)
    d = diff_plans(rl, rr, "l", "r", left, right)
    assert d.segment_sources_only_right == ["dev"]
    assert d.segment_sources_only_left == []
    assert d.best_scheme_left is None
    assert d.best_scheme_right is not None
    assert d.best_scheme_changed is False
