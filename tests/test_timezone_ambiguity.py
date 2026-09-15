"""IANA 时区歧义求解测试：回拨重叠、春季空洞、fold 组合搜索与方案比较。

覆盖：
* ``utc_candidates`` 的候选展开（重叠 2 个 / 普通 1 个 / 空洞 0 个）；
* 事件约束选取可行 fold 组合（差分约束 + 分支限界），截断状态稳定；
* 逐事件候选 UTC、采用偏移与选择依据；矛盾信息关联事件/来源/时区规则；
* 显式偏移优先；未声明时区的旧请求保持固定偏移行为；
* 时区声明随场景版本保存；``/compare`` 展示时区与 fold 差异；
* 解析结论与进程时区无关。
"""

import os
import tempfile
import time

import pytest

from app.engine import ScenarioValidationError, reconcile
from app.models import (
    CalibrationAnchor,
    Constraint,
    Event,
    Scenario,
    SourceClock,
    SourceSegmentation,
    ClockSegment,
)
from app.timezones import (
    TimezoneResolutionError,
    classify_naive,
    describe_gap,
    load_zone,
    utc_candidates,
)

NY = "America/New_York"
# 2026-11-01 纽约回拨：02:00 EDT → 01:00 EST；01:30 出现两次
OVERLAP_READING = "2026-11-01T01:30:00"
FOLD0_UNIX = 1793511000.0  # 2026-11-01T05:30:00Z（EDT，UTC-4）
FOLD1_UNIX = 1793514600.0  # 2026-11-01T06:30:00Z（EST，UTC-5）
# 2026-03-08 纽约跳时：02:00 → 03:00；02:30 不存在
GAP_READING = "2026-03-08T02:30:00"


def _ny_source(**kw) -> SourceClock:
    return SourceClock(id="cam", iana_timezone=NY, **kw)


# ----------------------------- 候选展开（单元） -----------------------------


def test_candidates_overlap_two_folds():
    cands = utc_candidates(OVERLAP_READING, load_zone(NY))
    assert len(cands) == 2
    assert [c.fold for c in cands] == [0, 1]
    assert cands[0].unix_s == pytest.approx(FOLD0_UNIX)
    assert cands[1].unix_s == pytest.approx(FOLD1_UNIX)
    assert cands[0].utc_offset_s == pytest.approx(-14400.0)
    assert cands[1].utc_offset_s == pytest.approx(-18000.0)
    assert cands[0].offset_iso == "-04:00" and cands[1].offset_iso == "-05:00"
    assert {c.tz_rule for c in cands} == {"EDT", "EST"}
    assert cands[0].iso == "2026-11-01T05:30:00Z"
    assert classify_naive(OVERLAP_READING, load_zone(NY)) == "ambiguous"


def test_candidates_gap_empty_and_description():
    tz = load_zone(NY)
    assert utc_candidates(GAP_READING, tz) == []
    assert classify_naive(GAP_READING, tz) == "gap"
    desc = describe_gap(GAP_READING, NY)
    assert "不存在" in desc and "02:00:00" in desc and "03:00:00" in desc
    assert "EST" in desc and "EDT" in desc


def test_candidates_unique_normal_time():
    cands = utc_candidates("2026-01-15T12:00:00", load_zone(NY))
    assert len(cands) == 1
    assert cands[0].fold == 0
    assert cands[0].utc_offset_s == pytest.approx(-18000.0)
    assert cands[0].tz_rule == "EST"
    # 东八区（无夏令时）任意时刻唯一候选
    sh = utc_candidates("2026-09-14T08:00:00", load_zone("Asia/Shanghai"))
    assert len(sh) == 1 and sh[0].utc_offset_s == pytest.approx(28800.0)


def test_load_zone_invalid():
    with pytest.raises(TimezoneResolutionError):
        load_zone("Not/AZone")


# ----------------------------- fold 组合求解 -----------------------------


def _overlap_scenario(constraints, events=None) -> Scenario:
    return Scenario(
        name="dst-overlap",
        sources=[_ny_source()],
        events=events
        or [
            Event(id="photo", source_id="cam", reading=OVERLAP_READING),
            Event(id="radio", source_id=None, reading="2026-11-01T06:15:00Z"),
        ],
        constraints=constraints,
    )


def test_constraint_selects_fold1():
    """same_event 容差只允许 fold=1（06:30Z）命中 06:15Z 的参考事件。"""
    sc = _overlap_scenario(
        [Constraint(id="c1", type="same_event", a="photo", b="radio", tolerance_s=1200.0)]
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert r.timezone is not None and r.timezone.status == "ok"
    res = next(x for x in r.timezone.resolutions if x.event_id == "photo")
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2
    assert res.adopted_fold == 1
    assert res.adopted_utc_offset_s == pytest.approx(-18000.0)
    assert res.adopted_unix_s == pytest.approx(FOLD1_UNIX)
    assert "被差分约束排除" in res.basis
    assert r.timezone.fold_assignments == {"photo": 1}
    assert r.timezone.stats.feasible_combinations == 1
    assert r.timezone.stats.branches_pruned == 1
    # 统一时间线落在 fold=1 的窗口上
    win = next(w for w in r.unified_timeline if w.earliest_unix_s.event_ids == ["photo"])
    assert win.earliest_unix_s.value == pytest.approx(FOLD1_UNIX, abs=2.0)


def test_fold0_preferred_when_both_feasible():
    sc = _overlap_scenario(
        [Constraint(id="c1", type="same_event", a="photo", b="radio", tolerance_s=3600.0)]
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert r.timezone.fold_assignments == {"photo": 0}
    assert r.timezone.stats.feasible_combinations == 2
    res = next(x for x in r.timezone.resolutions if x.event_id == "photo")
    assert res.adopted_fold == 0
    assert "亦可行" in res.basis


def test_multi_event_fold_combination():
    """两个重叠事件 + min_interval：唯一可行组合 a=fold0, b=fold1。"""
    sc = Scenario(
        name="dst-two",
        sources=[_ny_source()],
        events=[
            Event(id="a", source_id="cam", reading=OVERLAP_READING),
            Event(id="b", source_id="cam", reading=OVERLAP_READING),
        ],
        constraints=[Constraint(id="c", type="min_interval", a="a", b="b", min_s=1800.0)],
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert r.timezone.fold_assignments == {"a": 0, "b": 1}
    assert r.timezone.stats.feasible_combinations == 1
    assert r.timezone.stats.combinations_total == 4


def test_all_combinations_infeasible_contradiction():
    """min+max 夹击使 4 个 fold 组合全部无解，矛盾关联事件/来源/时区。"""
    sc = Scenario(
        name="dst-none",
        sources=[_ny_source()],
        events=[
            Event(id="a", source_id="cam", reading=OVERLAP_READING),
            Event(id="b", source_id="cam", reading=OVERLAP_READING),
        ],
        constraints=[
            Constraint(id="cmin", type="min_interval", a="a", b="b", min_s=1800.0),
            Constraint(id="cmax", type="max_interval", a="a", b="b", max_s=1800.0),
        ],
    )
    r = reconcile(sc)
    assert r.feasible is False
    assert r.timezone.status == "infeasible"
    assert r.timezone.stats.feasible_combinations == 0
    contra = r.contradiction
    assert contra is not None
    assert contra.related_record_ids["timezones"] == [NY]
    assert set(contra.related_record_ids["events"]) == {"a", "b"}
    assert contra.related_record_ids["sources"] == ["cam"]
    assert "fold" in contra.explanation


def test_spring_gap_contradiction_links_records():
    sc = Scenario(
        name="dst-gap",
        sources=[_ny_source()],
        events=[
            Event(id="photo", source_id="cam", reading=GAP_READING),
            Event(id="other", source_id="cam", reading="2026-03-08T03:30:00"),
        ],
        constraints=[Constraint(id="c1", type="before", a="photo", b="other")],
    )
    r = reconcile(sc)
    assert r.feasible is False
    assert r.timezone.status == "infeasible"
    assert r.timezone.gap_events == ["photo"]
    res = next(x for x in r.timezone.resolutions if x.event_id == "photo")
    assert res.status == "gap" and res.candidates == [] and res.adopted_unix_s is None
    # 无歧义事件未受空洞影响：另一事件仍有唯一候选结论
    other = next(x for x in r.timezone.resolutions if x.event_id == "other")
    assert other.status == "unique" and other.adopted_unix_s is not None
    contra = r.contradiction
    assert contra.related_record_ids["events"] == ["photo"]
    assert contra.related_record_ids["sources"] == ["cam"]
    assert contra.related_record_ids["timezones"] == [NY]
    assert "不存在" in contra.explanation and "02:00:00" in contra.explanation


def test_explicit_offset_takes_priority():
    """显式偏移读数不参与 fold 展开，即使落在重叠时段。"""
    sc = Scenario(
        name="dst-explicit",
        sources=[_ny_source()],
        events=[
            Event(id="exp", source_id="cam", reading="2026-11-01T01:30:00-05:00"),
            Event(id="amb", source_id="cam", reading=OVERLAP_READING),
        ],
        constraints=[
            Constraint(id="c", type="same_event", a="exp", b="amb", tolerance_s=1.0)
        ],
    )
    r = reconcile(sc)
    assert r.feasible is True
    res = next(x for x in r.timezone.resolutions if x.event_id == "exp")
    assert res.status == "explicit_offset"
    assert res.adopted_fold is None
    assert res.adopted_utc_offset_s == pytest.approx(-18000.0)
    assert res.adopted_unix_s == pytest.approx(FOLD1_UNIX)
    # 显式 -05:00 即 fold=1 解释；same_event 迫使歧义事件也取 fold=1
    assert r.timezone.fold_assignments == {"amb": 1}


def test_truncation_status_stable():
    """节点上限截断：状态、原因与统计在重复运行间完全一致。"""
    events = [Event(id=f"e{i}", source_id="cam", reading=OVERLAP_READING) for i in range(8)]
    sc = Scenario(name="dst-trunc", sources=[_ny_source()], events=events, constraints=[])
    r1 = reconcile(sc, tz_max_search_nodes=1)
    r2 = reconcile(sc, tz_max_search_nodes=1)
    assert r1.timezone.status == "truncated"
    assert r1.timezone.stats.truncated is True
    assert r1.timezone.stats.truncation_reason == "node_limit"
    assert r1.timezone.stats.combinations_total == 256
    assert r1.timezone.stats.model_dump() == r2.timezone.stats.model_dump()
    assert r1.feasible is False  # 截断且未找到可行组合
    # 上限放宽后同一场景可解
    r3 = reconcile(sc)
    assert r3.feasible is True and r3.timezone.status == "ok"
    assert r3.timezone.stats.feasible_combinations == 256


def test_unique_and_offset_supersede():
    """声明时区后固定偏移被忽略：declared_utc_offset_s=3600 仍按上海 +8 解析。"""
    sc = Scenario(
        name="tz-supersede",
        sources=[
            SourceClock(
                id="cam", iana_timezone="Asia/Shanghai", declared_utc_offset_s=3600.0
            )
        ],
        events=[Event(id="e", source_id="cam", reading="2026-09-14T08:00:00")],
        constraints=[],
    )
    r = reconcile(sc)
    assert r.feasible is True
    res = r.timezone.resolutions[0]
    assert res.status == "unique"
    assert res.adopted_utc_offset_s == pytest.approx(28800.0)
    assert res.adopted_unix_s == pytest.approx(
        1789344000.0  # 2026-09-14T00:00:00Z
    )
    assert any("固定偏移被忽略" in w for w in r.warnings)


# ----------------------------- 校验与兼容 -----------------------------


def test_invalid_timezone_rejected():
    sc = Scenario(
        name="bad-tz",
        sources=[SourceClock(id="cam", iana_timezone="Mars/Olympus")],
        events=[],
        constraints=[],
    )
    with pytest.raises(ScenarioValidationError, match="IANA 时区无效"):
        reconcile(sc)


def test_timezone_plus_segments_same_source_rejected():
    sc = Scenario(
        name="tz-seg",
        sources=[_ny_source()],
        events=[Event(id="e", source_id="cam", reading=OVERLAP_READING)],
        constraints=[],
        clock_segments=[
            SourceSegmentation(
                source_id="cam",
                segments=[ClockSegment(id="seg-boot", start_clock_reading="2026-11-01T00:00:00")],
            )
        ],
    )
    with pytest.raises(ScenarioValidationError, match="暂不支持组合使用"):
        reconcile(sc)


def test_legacy_fixed_offset_unchanged_without_timezone():
    """未声明时区：无 timezone 报告，naive 读数仍按固定偏移解释。"""
    sc = Scenario(
        name="legacy",
        default_utc_offset_s=28800,
        sources=[SourceClock(id="cam", declared_utc_offset_s=28800)],
        events=[Event(id="e", source_id="cam", reading=OVERLAP_READING)],
        constraints=[],
    )
    r = reconcile(sc)
    assert r.timezone is None
    assert r.feasible is True
    win = r.unified_timeline[0]
    # 固定 +8：01:30+08:00 → 2026-10-31T17:30:00Z，与纽约回拨无关
    assert win.earliest == "2026-10-31T17:30:00Z"


def test_anchor_naive_in_overlap_warns_and_deterministic():
    """锚点读数落在重叠内：确定性 fold=0 并给出警告。"""
    sc = Scenario(
        name="tz-anchor",
        sources=[_ny_source()],
        anchors=[
            CalibrationAnchor(
                id="a1",
                source_id="cam",
                clock_reading=OVERLAP_READING,
                reference_time="2026-11-01T05:30:00Z",  # fold=0 对应的真实时刻
                reference_uncertainty_s=0.5,
            )
        ],
        events=[Event(id="e", source_id="cam", reading="2026-11-01T03:30:00")],
        constraints=[],
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert any("锚点 a1" in w and "回拨重叠" in w and "fold=0" in w for w in r.warnings)
    # 锚点按 fold=0（05:30Z）拟合：事件 03:30（唯一，EST -5 → 08:30Z）偏移为 0
    win = r.unified_timeline[0]
    assert win.earliest_unix_s.value == pytest.approx(1793521800.0, abs=2.0)


def test_anchor_explicit_and_naive_mixed_offsets_fit():
    """夏令时两侧锚点（EDT/EST）与显式偏移锚点混合拟合，漂移率正确。"""
    sc = Scenario(
        name="tz-fit",
        sources=[_ny_source()],
        anchors=[
            CalibrationAnchor(
                id="summer", source_id="cam",
                clock_reading="2026-07-01T12:00:00",  # EDT -4
                reference_time="2026-07-01T16:00:00Z",
            ),
            CalibrationAnchor(
                id="winter", source_id="cam",
                clock_reading="2027-01-01T12:00:00",  # EST -5
                reference_time="2027-01-01T17:00:00Z",
            ),
        ],
        events=[Event(id="e", source_id="cam", reading="2026-10-01T12:00:00")],
        constraints=[],
    )
    r = reconcile(sc)
    assert r.feasible is True
    cm = r.clock_models[0]
    assert cm.offset_s.value == pytest.approx(0.0, abs=1e-6)
    assert cm.drift_ppm.value == pytest.approx(0.0, abs=1e-6)
    # 事件 2026-10-01T12:00 EDT → 16:00Z
    assert r.unified_timeline[0].earliest_unix_s.value == pytest.approx(
        1790870400.0, abs=1.0
    )


def test_process_tz_independence():
    """fold 求解与进程时区无关。"""
    if not hasattr(time, "tzset"):
        pytest.skip("当前平台不支持 time.tzset()")
    old = os.environ.get("TZ")
    try:
        sigs = {}
        for tz in ("UTC", "America/New_York", "Asia/Tokyo"):
            os.environ["TZ"] = tz
            time.tzset()
            r = reconcile(
                _overlap_scenario(
                    [Constraint(id="c1", type="same_event", a="photo", b="radio",
                                tolerance_s=1200.0)]
                )
            )
            res = next(x for x in r.timezone.resolutions if x.event_id == "photo")
            sigs[tz] = (
                r.feasible,
                res.adopted_fold,
                res.adopted_unix_s,
                r.timezone.stats.model_dump(),
                [w.earliest for w in r.unified_timeline],
            )
        assert sigs["UTC"] == sigs["America/New_York"] == sigs["Asia/Tokyo"]
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ----------------------------- API / 版本保存 / 比较 -----------------------------


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["FORENSIC_DB"] = tmp.name
    import importlib

    import app.main as main

    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def _api_scenario(tolerance=1200.0, tz=NY):
    return {
        "name": "dst-api",
        "sources": [{"id": "cam", "iana_timezone": tz}],
        "events": [
            {"id": "photo", "source_id": "cam", "reading": OVERLAP_READING},
            {"id": "radio", "source_id": None, "reading": "2026-11-01T06:15:00Z"},
        ],
        "constraints": [
            {"id": "c1", "type": "same_event", "a": "photo", "b": "radio",
             "tolerance_s": tolerance}
        ],
    }


def test_api_reconcile_timezone_report(client):
    r = client.post("/reconcile", json=_api_scenario())
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is True
    tz = body["timezone"]
    assert tz["status"] == "ok"
    assert tz["fold_assignments"] == {"photo": 1}
    assert tz["ambiguous_events"] == ["photo"]
    res = next(x for x in tz["resolutions"] if x["event_id"] == "photo")
    assert len(res["candidates"]) == 2
    assert res["adopted_fold"] == 1
    assert res["adopted_utc_offset_s"] == -18000.0
    assert res["basis"]


def test_api_invalid_timezone_400(client):
    payload = _api_scenario(tz="Not/AZone")
    r = client.post("/reconcile", json=payload)
    assert r.status_code == 400
    assert "IANA 时区无效" in r.json()["detail"]


def test_timezone_declaration_saved_with_version(client):
    """时区声明随场景版本保存；对已存版本校核可复现解析决策。"""
    created = client.post("/scenarios", json={"scenario": _api_scenario()})
    assert created.status_code == 200
    sid = created.json()["scenario_id"]
    got = client.get(f"/scenarios/{sid}?version=1")
    assert got.json()["payload"]["sources"][0]["iana_timezone"] == NY
    # 对已存版本校核：解析决策（fold 采用）可复现
    r1 = client.post(f"/scenarios/{sid}/reconcile?version=1")
    r2 = client.post(f"/scenarios/{sid}/reconcile")
    assert r1.json()["timezone"]["fold_assignments"] == {"photo": 1}
    assert r1.json()["timezone"] == r2.json()["timezone"]


def test_compare_shows_fold_and_timezone_differences(client):
    """/compare：左方案固定偏移、右方案声明时区 → 时区声明与 fold 差异。"""
    left = {
        "name": "fixed",
        "sources": [{"id": "cam", "declared_utc_offset_s": 0}],
        "events": [
            {"id": "photo", "source_id": "cam", "reading": OVERLAP_READING},
            {"id": "radio", "source_id": None, "reading": "2026-11-01T06:15:00Z"},
        ],
        "constraints": [],
    }
    right = _api_scenario(tolerance=3600.0)
    r = client.post("/compare", json={"left": left, "right": right})
    assert r.status_code == 200
    body = r.json()
    assert body["timezones_only_right"] == ["cam"]
    assert body["timezones_only_left"] == []
    assert body["timezone_declarations_changed"] == []
    diffs = {d["event_id"]: d for d in body["fold_differences"]}
    assert "photo" in diffs
    d = diffs["photo"]
    assert d["left_fold"] is None and d["right_fold"] == 0
    assert d["right_utc_offset_s"] == -14400.0


def test_compare_fold_change_between_versions(client):
    """同一事件在两个版本中因约束不同采用不同 fold。"""
    v1 = _api_scenario(tolerance=3600.0)  # 两候选均可行 → fold=0
    v2 = _api_scenario(tolerance=1200.0)  # 仅 fold=1 可行
    c = client.post("/scenarios", json={"scenario": v1})
    sid = c.json()["scenario_id"]
    client.post(f"/scenarios/{sid}/versions", json={"scenario": v2})
    r = client.post(
        "/compare",
        json={"left_scenario_id": sid, "left_version": 1,
              "right_scenario_id": sid, "right_version": 2},
    )
    assert r.status_code == 200
    diffs = {d["event_id"]: d for d in r.json()["fold_differences"]}
    assert diffs["photo"]["left_fold"] == 0
    assert diffs["photo"]["right_fold"] == 1
    assert diffs["photo"]["delta_s"] == pytest.approx(3600.0)


def test_compare_timezone_declaration_changed(client):
    r = client.post(
        "/compare",
        json={"left": _api_scenario(tz=NY), "right": _api_scenario(tz="America/Chicago")},
    )
    assert r.status_code == 200
    assert r.json()["timezone_declarations_changed"] == ["cam"]


# ----------------------------- 与其他模块组合 -----------------------------


def test_timezone_with_segments_on_other_source():
    """时区来源与分段来源（不同来源）组合：fold 求解后分段照常工作。"""
    sc = Scenario(
        name="tz+seg",
        sources=[
            _ny_source(),
            SourceClock(id="logger", declared_utc_offset_s=0),
        ],
        anchors=[
            CalibrationAnchor(
                id="l1", source_id="logger",
                clock_reading="2026-11-01T05:00:00+00:00",
                reference_time="2026-11-01T05:00:00Z",
            ),
            CalibrationAnchor(
                id="l2", source_id="logger",
                clock_reading="2026-11-01T07:00:00+00:00",
                reference_time="2026-11-01T07:00:00Z",
            ),
        ],
        events=[
            Event(id="photo", source_id="cam", reading=OVERLAP_READING),
            Event(id="log", source_id="logger", reading="2026-11-01T06:30:10+00:00"),
        ],
        constraints=[
            Constraint(id="c", type="same_event", a="photo", b="log", tolerance_s=60.0)
        ],
        clock_segments=[
            SourceSegmentation(
                source_id="logger",
                segments=[ClockSegment(id="seg-x", start_clock_reading="2026-11-01T04:00:00+00:00")],
            )
        ],
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert r.timezone.fold_assignments == {"photo": 1}  # 06:30Z 与日志 06:30:10Z 匹配
    assert r.segmentation is not None
    assert r.segmentation.segmented_sources == ["logger"]


def test_timezone_with_association_groups():
    """时区歧义求解与候选关联求解叠加：关联在 fold 解析后的区间上进行。"""
    sc = Scenario(
        name="tz+assoc",
        sources=[_ny_source()],
        events=[
            Event(id="photo", source_id="cam", reading=OVERLAP_READING),
            Event(id="base", source_id=None, reading="2026-11-01T06:30:05Z"),
            Event(id="cand", source_id=None, reading="2026-11-01T06:30:06Z"),
        ],
        constraints=[
            Constraint(id="c", type="same_event", a="photo", b="base", tolerance_s=30.0)
        ],
        association_groups=[
            {
                "id": "g1",
                "base_event_id": "base",
                "mode": "exactly_one",
                "candidates": [{"event_id": "cand", "tolerance_s": 5.0}],
            }
        ],
    )
    r = reconcile(sc)
    assert r.feasible is True
    assert r.timezone.fold_assignments == {"photo": 1}
    assert r.association is not None and r.association.status == "ok"
    assert r.association.hypotheses[0].pairings[0].candidate_event_id == "cand"
