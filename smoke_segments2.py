"""冒烟 2：边界不确定区多归属 + 矛盾链 + 显式声明段。"""
import sys

sys.path.insert(0, ".pylibs/lib/python3.11/site-packages")

from datetime import datetime, timezone, timedelta

from app.models import (
    CalibrationAnchor, ClockSegment, Constraint, Event, Scenario, SourceClock,
    SourceSegmentation,
)
from app.engine import reconcile, diff_plans


def iso(t):
    return (datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(seconds=t)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    ) + "Z"


# 显式段：初始段 + day1 00:00 起重启（钟被向后拨 600s：钟面比真值慢 600s）
anchors = [
    CalibrationAnchor(id="a0", source_id="dev", clock_reading=iso(0), reference_time=iso(0)),
    CalibrationAnchor(id="a1", source_id="dev",
                      clock_reading=iso(86400 - 600), reference_time=iso(86400)),
    CalibrationAnchor(id="a2", source_id="dev",
                      clock_reading=iso(2 * 86400 - 600), reference_time=iso(2 * 86400)),
]
# 事件 e-edge 钟面恰在边界 ±60s 内（边界钟面 86400，读数 86430，真值归属决定先后）
events = [
    Event(id="e-ref", source_id=None, reading=iso(86400 + 50)),  # 真值 day1+50
    Event(id="e-edge", source_id="dev", reading=iso(86430)),     # 钟面 day1+30
]
sc = Scenario(
    name="边界模糊",
    sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
    anchors=anchors,
    events=events,
    constraints=[
        # 若 e-edge 归初始段：真值 ≈ day1+30（先于 e-ref day1+50），约束可行
        # 若归重启段：真值 ≈ day1+630（后于 e-ref），约束不可行
        Constraint(id="c-order", type="before", a="e-edge", b="e-ref"),
    ],
    clock_segments=[
        SourceSegmentation(
            source_id="dev",
            segments=[
                ClockSegment(id="seg-boot", start_clock_reading=iso(86400),
                             boundary_uncertainty_s=60.0),
            ],
        ),
    ],
)
r = reconcile(sc)
print("feasible:", r.feasible, "status:", r.segmentation.status)
sch = r.segmentation.schemes[0]
print("chosen key:", sch.key)
for a in sch.assignments:
    print("assign:", a.event_id, a.feasible_segment_ids, "->", a.assigned_segment_id,
          "ambiguous:", a.ambiguous)
    print("   basis:", a.basis)
assert r.feasible
edge = next(a for a in sch.assignments if a.event_id == "e-edge")
assert edge.ambiguous
assert edge.assigned_segment_id == "seg-initial"
win = {w.earliest_unix_s.event_ids[0]: w for w in r.unified_timeline}["e-edge"]
assert win.assigned_segment_id == "dev:seg-initial"
assert set(win.segment_ids) == {"dev:seg-initial", "dev:seg-boot"}
jump = sch.jumps[0]
print("jump:", jump.jump_s, jump.origin)
assert abs(jump.jump_s - (-600.0)) < 1e-6
print("AMBIGUITY OK")

# 矛盾：强制 e-ref 先于 e-edge；若两个归属都不允许则全方案不可行
sc.constraints = [Constraint(id="c-rev", type="before", a="e-ref", b="e-edge")]
# 初始段归属下真值 day1+30 < day1+50，与 e-ref 先于 e-edge 矛盾；
# 重启段归属下 day1+630 > day1+50，可行 → 求解器应选重启段
r2 = reconcile(sc)
print("feasible2:", r2.feasible)
sch2 = r2.segmentation.schemes[0]
edge2 = next(a for a in sch2.assignments if a.event_id == "e-edge")
print("assigned2:", edge2.assigned_segment_id)
assert r2.feasible
assert edge2.assigned_segment_id == "seg-boot"
print("AMBIGUITY-REVERSE OK")

# 真正不可行：两归属都被钉死（加 min_interval 超大间隔，两种归属均矛盾）
sc.constraints = [
    Constraint(id="c-big", type="min_interval", a="e-edge", b="e-ref", min_s=100000.0),
]
r3 = reconcile(sc)
print("feasible3:", r3.feasible, "status3:", r3.segmentation.status)
assert not r3.feasible
con = r3.contradiction
print("contradiction segments:", con.segment_ids, con.related_record_ids.get("clock_segments"))
print("contradiction anchors:", con.related_record_ids.get("anchors"))
assert con.related_record_ids.get("clock_segments")
assert con.related_record_ids.get("anchors")
print("CONTRADICTION OK")

# 比较：左右方案边界移动
sc_left = Scenario(
    name="左",
    sources=[SourceClock(id="dev", declared_utc_offset_s=0)],
    anchors=anchors,
    events=events,
    clock_segments=[SourceSegmentation(
        source_id="dev",
        segments=[ClockSegment(id="seg-boot", start_clock_reading=iso(86000),
                                boundary_uncertainty_s=0.0)],
    )],
)
rl = reconcile(sc_left)
rr = reconcile(Scenario(**{**sc_left.model_dump(),
                          "name": "右",
                          "clock_segments": [SourceSegmentation(
                              source_id="dev",
                              segments=[ClockSegment(id="seg-boot", start_clock_reading=iso(86400),
                                                      boundary_uncertainty_s=60.0)],
                          )]}))
d = diff_plans(rl, rr, "l", "r", sc_left, rr and [
    s for s in [Scenario(**{**sc_left.model_dump(), "clock_segments": [SourceSegmentation(
        source_id="dev",
        segments=[ClockSegment(id="seg-boot", start_clock_reading=iso(86400),
                                boundary_uncertainty_s=60.0)],
    )]})]][0])
chg = d.segment_boundary_moves[0]
print("move:", chg.source_id, chg.moves[0].delta_s, chg.moves[0].segment_id)
assert abs(chg.moves[0].delta_s - 400.0) < 1e-9
assert d.best_scheme_left and d.best_scheme_right
print("COMPARE MOVE OK")
