"""手工冒烟：分段时钟模型端到端。"""
import sys

sys.path.insert(0, ".pylibs/lib/python3.11/site-packages")

from app.models import (
    CalibrationAnchor, ClockSegment, Constraint, Event, Scenario, SourceClock,
    SourceSegmentation,
)
from app.engine import reconcile


def iso(t):
    # t 为相对 2026-09-13T00:00:00Z 的秒数
    from datetime import datetime, timezone, timedelta
    return (datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(seconds=t)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


# dev 钟：day0/day1 准确（无漂移），day1 后被向前拨 300s（钟面比真值快 300s）
anchors = [
    CalibrationAnchor(id="a0", source_id="dev",
                      clock_reading=iso(0), reference_time=iso(0)),
    CalibrationAnchor(id="a1", source_id="dev",
                      clock_reading=iso(86400), reference_time=iso(86400)),
    # 跳变后锚点：钟面 day2 00:05:00 对应真值 day2 00:00:00
    CalibrationAnchor(id="a2", source_id="dev",
                      clock_reading=iso(2 * 86400 + 300), reference_time=iso(2 * 86400)),
    CalibrationAnchor(id="a3", source_id="dev",
                      clock_reading=iso(3 * 86400 + 300), reference_time=iso(3 * 86400)),
]
events = [
    Event(id="e-before", source_id="dev", reading=iso(86400 + 100)),
    Event(id="e-after", source_id="dev", reading=iso(2 * 86400 + 300 + 100)),
]

sc = Scenario(
    name="跳变",
    sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
    anchors=anchors,
    events=events,
    constraints=[
        Constraint(id="c-order", type="before", a="e-before", b="e-after"),
    ],
    clock_segments=[
        SourceSegmentation(source_id="dev", jump_threshold_s=60.0),
    ],
)

r = reconcile(sc)
print("feasible:", r.feasible)
print("segmentation status:", r.segmentation.status)
print("schemes:", r.segmentation.schemes_total, "truncated:", r.segmentation.schemes_truncated)
for d in r.segmentation.detected_jumps:
    print("detected:", d.between_anchor_ids, round(d.residual_jump_s, 2), "est:", d.estimated_jump_s)
for sch in r.segmentation.schemes:
    print("scheme rank", sch.rank, sch.key, "feasible", sch.feasible,
          "fit", round(sch.fit_score_s, 3), "chosen", sch.chosen)
    for s in sch.segments:
        print("  seg", s.segment_id, "off", round(s.offset_s.value, 3),
              "drift_ppm", round(s.drift_ppm.value, 3), "n", s.anchor_count,
              "jump", s.jump_s, "resid", s.jump_residual_s)
    for a in sch.assignments:
        print("  assign", a.event_id, a.feasible_segment_ids, "->", a.assigned_segment_id, a.ambiguous)

for w in r.unified_timeline:
    eid = w.earliest_unix_s.event_ids[0]
    print("timeline", eid, w.representative, "segments", w.segment_ids, "assigned", w.assigned_segment_id)

# 期望：e-before 真值中心 = day1+100；e-after 真值中心 = day2+100
from app.timescale import parse_unix
t0 = parse_unix(iso(0))[0]
mid = {w.earliest_unix_s.event_ids[0]: (w.earliest_unix_s.value + w.latest_unix_s.value) / 2
       for w in r.unified_timeline}
assert abs(mid["e-before"] - (t0 + 86400 + 100)) < 0.01, mid
assert abs(mid["e-after"] - (t0 + 2 * 86400 + 100)) < 0.01, mid
jump = r.segmentation.schemes[0].jumps[0].jump_s
assert jump is not None and abs(jump - 300.0) < 0.01, jump
print("SMOKE OK")
