"""端到端校核逻辑测试。"""

import pytest

from app.engine import (
    ScenarioValidationError,
    build_graph,
    diff_plans,
    reconcile,
)
from app.models import (
    CalibrationAnchor,
    Constraint,
    Event,
    Scenario,
    SourceClock,
)


def make_two_source_scenario(gap_s: float = 120.0):
    """门禁说 08:00，相机 EXIF 说 08:02，但同一事件要求 before。"""
    return Scenario(
        name="两源对时",
        sources=[
            SourceClock(id="door", declared_utc_offset_s=28800, base_uncertainty_s=1.0),
            SourceClock(id="cam", declared_utc_offset_s=28800, base_uncertainty_s=1.0),
        ],
        anchors=[
            CalibrationAnchor(
                id="ntp-door",
                source_id="door",
                clock_reading="2026-09-14T08:00:00+08:00",
                reference_time="2026-09-14T00:00:05Z",
                reference_uncertainty_s=0.5,
            ),
            CalibrationAnchor(
                id="ntp-cam",
                source_id="cam",
                clock_reading="2026-09-14T08:00:00+08:00",
                reference_time="2026-09-14T00:00:00Z",
                reference_uncertainty_s=0.5,
            ),
        ],
        events=[
            Event(
                id="badge",
                source_id="door",
                reading="2026-09-14T08:00:00+08:00",
                reading_uncertainty_s=0.0,
            ),
            Event(
                id="photo",
                source_id="cam",
                reading="2026-09-14T08:02:00+08:00",
                reading_uncertainty_s=0.0,
            ),
        ],
        constraints=[
            Constraint(id="c-photo-after-badge", type="before", a="badge", b="photo")
        ],
    )


def test_clock_offset_fit_and_inversion():
    from app.timescale import build_clock_models, convert_events

    sc = make_two_source_scenario()
    models, reports, warnings, errors = build_clock_models(
        sc.sources, sc.anchors, sc.events, sc.default_utc_offset_s
    )
    assert errors == []
    # door 钟面 08:00 对应真值 00:00:05 → offset = -5s（clock-true）
    assert reports["door"].offset_s.value == pytest.approx(-5.0)
    assert reports["cam"].offset_s.value == pytest.approx(0.0)
    intervals, _ = convert_events(sc.sources, sc.events, models, 0.0)
    lo = {iv.event_id: iv for iv in intervals}
    from app.timescale import parse_unix

    t0 = parse_unix("2026-09-14T00:00:00Z")[0]
    # badge 真值中心 = 00:00:05Z
    assert (lo["badge"].lo + lo["badge"].hi) / 2 == pytest.approx(t0 + 5.0, abs=0.01)
    # photo 真值中心 = 00:02:00Z
    assert (lo["photo"].lo + lo["photo"].hi) / 2 == pytest.approx(t0 + 120.0, abs=0.01)


def test_feasible_timeline_and_slack():
    sc = make_two_source_scenario()
    result = reconcile(sc)
    assert result.feasible
    slack = result.constraint_slack[0]
    assert slack.satisfiable
    # 两事件中心相差 115s，区间半宽各 1.5s，余量 ≈ 112s
    assert slack.slack_s == pytest.approx(112.0, abs=1.0)
    by_id = {w.earliest_unix_s.event_ids[0]: w for w in result.unified_timeline}
    assert by_id["badge"].earliest_unix_s.unit == "unix_seconds_utc"
    assert by_id["badge"].width_s.unit == "s"
    assert by_id["badge"].earliest_unix_s.derived_by.startswith("dcs.")
    assert by_id["badge"].source_id == "door"


def test_contradiction_chain_and_records():
    sc = make_two_source_scenario()
    # 强制要求 photo 先于 badge，与真实先后矛盾
    sc.constraints = [
        Constraint(id="c-wrong-order", type="before", a="photo", b="badge")
    ]
    result = reconcile(sc)
    assert not result.feasible
    con = result.contradiction
    assert set(con.cycle_event_ids) == {"badge", "photo"}
    assert con.cycle_constraint_ids == ["c-wrong-order"]
    assert con.total_slack_s < 0
    assert set(con.related_record_ids) >= {"events", "constraints", "sources", "anchors"}
    assert set(con.related_record_ids["events"]) == {"badge", "photo"}
    assert set(con.related_record_ids["sources"]) == {"door", "cam"}


def test_min_interval_contradiction_amount():
    sc = make_two_source_scenario()
    # photo 比 badge 晚约 115s，却要求至少 1000s
    sc.constraints = [
        Constraint(id="c-too-far", type="min_interval", a="badge", b="photo", min_s=1000.0)
    ]
    result = reconcile(sc)
    assert not result.feasible
    # 环：photo→badge(-1000) + badge→0(-3.5) + 0→photo(121.5) = -882
    assert result.contradiction.total_slack_s == pytest.approx(-882.0, abs=0.5)


def test_same_event_tolerance():
    sc = make_two_source_scenario()
    sc.constraints = [
        Constraint(id="c-same", type="same_event", a="badge", b="photo", tolerance_s=200.0)
    ]
    result = reconcile(sc)
    assert result.feasible
    # |120-5|=115，容差 200，两侧各留区间半宽 1.5 → 余量 82
    assert result.constraint_slack[0].slack_s == pytest.approx(82.0, abs=2.0)

    sc.constraints[0].tolerance_s = 110.0
    result = reconcile(sc)
    assert not result.feasible


def test_adjustment_budget_repair():
    sc = make_two_source_scenario()
    sc.constraints = [
        Constraint(id="c-wrong-order", type="before", a="photo", b="badge")
    ]
    # 不给预算：给出最小所需 L∞ 预算（两源各分一半，约 56s）
    result = reconcile(sc)
    assert result.adjustment.min_required_budget_s == pytest.approx(56.0, abs=2.0)

    # 预算不足
    result = reconcile(sc, budget_s=40.0)
    assert result.adjustment.feasible is False

    # 预算充足：给出建议（door 前移约 56s、cam 后移约 56s 等可行组合）
    result = reconcile(sc, budget_s=100.0)
    adj = result.adjustment
    assert adj.feasible
    shifts = {a.source_id: a.suggested_shift_s.value for a in adj.adjustments}
    assert max(abs(v) for v in shifts.values()) <= 100.0 + 1e-6
    # 用建议值平移后，系统必须可行
    _verify_shifts_feasible(sc, shifts)


def _verify_shifts_feasible(sc: Scenario, shifts: dict[str, float]):
    """把建议修正量直接施加到事件区间上，重新求解必须可行。"""
    from app.timescale import build_clock_models, convert_events
    from app.dcs import solve

    models, _r, _w, _e = build_clock_models(sc.sources, sc.anchors, sc.events, 0.0)
    intervals, _ = convert_events(sc.sources, sc.events, models, 0.0)
    for iv in intervals:
        if iv.source_id in shifts:
            iv.lo += shifts[iv.source_id]
            iv.hi += shifts[iv.source_id]
    n, _idx, edges = build_graph(intervals, sc.constraints)
    lower, upper = solve(n, edges)
    for i in range(1, n):
        assert lower[i] <= upper[i] + 1e-9


def test_drift_fit():
    """两个锚点识别线性漂移：钟每天快 8.64s（100ppm）。"""
    sc = Scenario(
        name="漂移",
        sources=[
            SourceClock(id="dev", declared_utc_offset_s=0, drift_ppm=200, base_uncertainty_s=0.0)
        ],
        anchors=[
            CalibrationAnchor(
                id="a1",
                source_id="dev",
                clock_reading="2026-09-13T00:00:00Z",
                reference_time="2026-09-13T00:00:00Z",
            ),
            CalibrationAnchor(
                id="a2",
                source_id="dev",
                clock_reading="2026-09-14T00:00:08.64Z",
                reference_time="2026-09-14T00:00:00Z",
            ),
            CalibrationAnchor(
                id="a3",
                source_id="dev",
                clock_reading="2026-09-15T00:00:17.28Z",
                reference_time="2026-09-15T00:00:00Z",
            ),
        ],
        events=[
            Event(id="e1", source_id="dev", reading="2026-09-15T12:00:30.2412Z"),
        ],
        constraints=[],
    )
    result = reconcile(sc)
    cm = result.clock_models[0]
    assert cm.drift_ppm.value == pytest.approx(100.0, rel=1e-6)
    # 钟面 Sep 15 12:00:30.2412（t0 后 2 天 43230.2412 钟面秒），
    # β=1.0001 反演后真实偏移 = 216030.2412/1.0001 ≈ 216008.6403s
    win = result.unified_timeline[0]
    center = (win.earliest_unix_s.value + win.latest_unix_s.value) / 2
    from app.timescale import parse_unix

    t0 = parse_unix("2026-09-13T00:00:00Z")[0]
    assert center == pytest.approx(t0 + 216008.6403, abs=0.01)


def test_naive_timestamp_uses_declared_offset():
    sc = Scenario(
        name="naive",
        default_utc_offset_s=28800,
        sources=[SourceClock(id="cam", declared_utc_offset_s=28800)],
        events=[
            Event(id="e", source_id="cam", reading="2026-09-14T08:00:00"),
            Event(id="g", source_id=None, reading="2026-09-14T08:00:00"),
        ],
        constraints=[Constraint(id="same", type="same_event", a="e", b="g", tolerance_s=0.0)],
    )
    result = reconcile(sc)
    assert result.feasible
    assert any("无时区" in w for w in result.warnings)


def test_validation_errors():
    sc = make_two_source_scenario()
    sc.events[0].source_id = "nope"
    with pytest.raises(ScenarioValidationError, match="不存在"):
        reconcile(sc)

    sc = make_two_source_scenario()
    sc.constraints[0].type = "min_interval"
    sc.constraints[0].min_s = None
    with pytest.raises(ScenarioValidationError, match="缺少 min_s"):
        reconcile(sc)


def test_plan_difference():
    sc1 = make_two_source_scenario()
    sc2 = make_two_source_scenario()
    sc2.constraints[0] = Constraint(
        id="c-same", type="same_event", a="badge", b="photo", tolerance_s=60.0
    )
    r1 = reconcile(sc1)
    r2 = reconcile(sc2)
    d = diff_plans(r1, r2, "a", "b", sc1, sc2)
    assert d.feasible_left and not d.feasible_right
    assert d.constraints_only_left == ["c-photo-after-badge"]
    assert d.constraints_only_right == ["c-same"]
    assert r2.contradiction is not None
    assert set(r2.contradiction.cycle_event_ids) == {"badge", "photo"}
    # 不可行方案也给出了修复所需的最小修正预算
    assert r2.adjustment.min_required_budget_s is not None
    assert r2.adjustment.min_required_budget_s == pytest.approx(26.0, abs=2.0)
