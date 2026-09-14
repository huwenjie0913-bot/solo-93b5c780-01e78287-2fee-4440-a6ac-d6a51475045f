"""无显式时区读数换算的跨进程时区回归测试。

naive 时间戳只能按来源声明偏移换算，结果不得随部署环境 TZ 变化。
通过 ``time.tzset()`` 在同一进程内切换进程时区，对 UTC、America/New_York、
Asia/Tokyo 三种环境分别重算完整时间线，并比对全部时间量与约束结论。
"""

import os
import time

import pytest

from app.engine import reconcile
from app.models import (
    CalibrationAnchor,
    Constraint,
    Event,
    Scenario,
    SourceClock,
)
from app.timescale import format_iso, parse_unix

pytestmark = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="当前平台不支持 time.tzset()"
)

PROCESS_TZS = ["UTC", "America/New_York", "Asia/Tokyo"]


@pytest.fixture(autouse=True)
def restore_tz():
    old = os.environ.get("TZ")
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _set_tz(name: str) -> None:
    os.environ["TZ"] = name
    time.tzset()


def _naive_scenario() -> Scenario:
    """门禁与相机读数均不带时区，声明为东八区；带先于约束。"""
    return Scenario(
        name="naive-跨时区一致",
        default_utc_offset_s=28800,
        sources=[
            SourceClock(id="door", declared_utc_offset_s=28800, base_uncertainty_s=1.0),
            SourceClock(id="cam", declared_utc_offset_s=28800, base_uncertainty_s=1.0),
        ],
        anchors=[
            CalibrationAnchor(
                id="a-door",
                source_id="door",
                clock_reading="2026-09-14T08:00:00",  # naive，按 28800 解释
                reference_time="2026-09-14T00:00:05Z",
                reference_uncertainty_s=0.5,
            ),
            CalibrationAnchor(
                id="a-cam",
                source_id="cam",
                clock_reading="2026-09-14T08:00:00",
                reference_time="2026-09-14T00:00:00Z",
                reference_uncertainty_s=0.5,
            ),
        ],
        events=[
            Event(id="badge", source_id="door",
                  reading="2026-09-14T08:00:00"),
            Event(id="photo", source_id="cam",
                  reading="2026-09-14T08:02:00"),
            # 无来源事件：naive 回退到场景默认偏移（28800）
            Event(id="radio", source_id=None,
                  reading="2026-09-14T08:02:00"),
        ],
        constraints=[
            Constraint(id="c1", type="before", a="badge", b="photo"),
            Constraint(id="c2", type="same_event", a="photo", b="radio",
                       tolerance_s=2.0),
        ],
    )


def _timeline_signature(result):
    """提取完整时间线的可比对签名。"""
    sig = {"feasible": result.feasible, "windows": {}, "slack": {}, "clocks": {}}
    for w in result.unified_timeline:
        sig["windows"][w.earliest_unix_s.event_ids[0]] = (
            w.earliest,
            w.latest,
            w.representative,
            w.earliest_unix_s.value,
            w.latest_unix_s.value,
            w.width_s.value,
        )
    for s in result.constraint_slack:
        sig["slack"][s.constraint_id] = (s.satisfiable, round(s.slack_s or 0.0, 9))
    for c in result.clock_models:
        sig["clocks"][c.source_id] = (round(c.offset_s.value, 9), round(c.drift_ppm.value, 9))
    return sig


def test_parse_unix_naive_tz_independent():
    expected = parse_unix("2026-09-14T00:00:00Z")[0]
    for tz in PROCESS_TZS:
        _set_tz(tz)
        u, aware = parse_unix("2026-09-14T08:00:00", 28800.0)
        assert aware is False
        assert u == pytest.approx(expected), f"naive 解析在 TZ={tz} 下偏移"
        assert format_iso(u) == "2026-09-14T00:00:00Z"


def test_parse_unix_explicit_offset_unchanged():
    # 显式 +08:00 与 naive+声明偏移在三种 TZ 下都应一致
    for tz in PROCESS_TZS:
        _set_tz(tz)
        explicit, aware_e = parse_unix("2026-09-14T08:00:00+08:00", 0.0)
        naive, aware_n = parse_unix("2026-09-14T08:00:00", 28800.0)
        assert aware_e is True and aware_n is False
        assert explicit == pytest.approx(naive)
        # 其他显式偏移（纽约 -04:00 DST）语义不变
        ny, _ = parse_unix("2026-09-13T20:00:00-04:00", 0.0)
        assert ny == pytest.approx(explicit)


def test_full_timeline_identical_across_timezones():
    signatures = {}
    for tz in PROCESS_TZS:
        _set_tz(tz)
        result = reconcile(_naive_scenario(), budget_s=100.0)
        signatures[tz] = _timeline_signature(result)

    baseline = signatures["UTC"]
    assert baseline["feasible"] is True
    # photo/radio 中心为 00:02Z；same_event 容差 2s 使窗口最早为 00:01:58.500Z
    assert baseline["windows"]["photo"][0] == "2026-09-14T00:01:58.500Z"
    assert baseline["windows"]["photo"][1] == "2026-09-14T00:02:01.500Z"
    assert baseline["windows"]["badge"][0] == "2026-09-14T00:00:03.500Z"

    for tz in ("America/New_York", "Asia/Tokyo"):
        assert signatures[tz] == baseline, f"TZ={tz} 的时间线与 UTC 不一致"


def test_contradiction_and_budget_identical_across_timezones():
    """矛盾结论与最小修正预算也不得随 TZ 漂移。"""
    def build():
        sc = _naive_scenario()
        sc.constraints = [
            Constraint(id="wrong", type="before", a="photo", b="badge")
        ]
        return sc

    ref = None
    for tz in PROCESS_TZS:
        _set_tz(tz)
        r = reconcile(build())
        assert r.feasible is False
        info = (
            [round(x, 9) for x in (
                r.contradiction.total_slack_s,
                r.adjustment.min_required_budget_s,
            )],
            r.contradiction.cycle_event_ids,
        )
        if ref is None:
            ref = info
        else:
            assert info == ref, f"TZ={tz} 的矛盾链/预算与 UTC 不一致"
