"""场景版本的时区解析决策留痕测试。

写入路径（POST /scenarios、POST /scenarios/{id}/versions）在保存版本时把
IANA 时区解析结果（时区声明、逐事件候选 UTC、采用的偏移/fold 与选择依据、
fold_assignments）随版本持久化；读取版本接口返回已保存的解析结果；
没有解析结果的旧版本（未声明时区 / 旧库行）继续可读（字段为 null）。
"""

import importlib
import json
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.models import Scenario
from app.storage import Store

NY = "America/New_York"
OVERLAP_READING = "2026-11-01T01:30:00"


def _tz_scenario(tolerance=1200.0, tz=NY):
    """纽约回拨重叠场景：same_event 容差决定 photo 的 fold。"""
    return {
        "name": "dst-persist",
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


@pytest.fixture()
def db_path():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # 由 Store 自行创建
    yield tmp.name
    if os.path.exists(tmp.name):
        os.unlink(tmp.name)


def make_client(db: str):
    """以指定数据库文件启动（重载 app.main，模拟一次进程启动）。"""
    os.environ["FORENSIC_DB"] = db
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


# ----------------------------- 保存与读取 -----------------------------


def test_create_persists_timezone_resolution(db_path):
    with make_client(db_path) as client:
        created = client.post("/scenarios", json={"scenario": _tz_scenario()})
        assert created.status_code == 200
        body = created.json()
        tz = body["timezone_resolution"]
        assert tz is not None
        assert tz["status"] == "ok"
        assert tz["timezone_sources"] == ["cam"]
        assert tz["fold_assignments"] == {"photo": 1}
        res = next(x for x in tz["resolutions"] if x["event_id"] == "photo")
        assert len(res["candidates"]) == 2
        assert res["adopted_fold"] == 1
        assert res["adopted_utc_offset_s"] == -18000.0
        assert "回拨重叠" in res["basis"]

        sid = body["scenario_id"]
        got = client.get(f"/scenarios/{sid}?version=1")
        assert got.status_code == 200
        assert got.json()["timezone_resolution"] == tz


def test_add_version_persists_per_version_resolution(db_path):
    """v1 与 v2 约束不同 → 各自持久化自己的 fold 决策。"""
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": _tz_scenario(tolerance=3600.0)})
        sid = c.json()["scenario_id"]
        assert c.json()["timezone_resolution"]["fold_assignments"] == {"photo": 0}
        v2 = client.post(
            f"/scenarios/{sid}/versions", json={"scenario": _tz_scenario(tolerance=1200.0)}
        )
        assert v2.status_code == 200
        assert v2.json()["timezone_resolution"]["fold_assignments"] == {"photo": 1}

        g1 = client.get(f"/scenarios/{sid}?version=1").json()
        g2 = client.get(f"/scenarios/{sid}?version=2").json()
        assert g1["timezone_resolution"]["fold_assignments"] == {"photo": 0}
        assert g2["timezone_resolution"]["fold_assignments"] == {"photo": 1}
        # 两版本的逐事件候选一致，采用 fold 不同
        r1 = next(x for x in g1["timezone_resolution"]["resolutions"]
                  if x["event_id"] == "photo")
        r2 = next(x for x in g2["timezone_resolution"]["resolutions"]
                  if x["event_id"] == "photo")
        assert r1["candidates"] == r2["candidates"]
        assert (r1["adopted_fold"], r2["adopted_fold"]) == (0, 1)


def test_persisted_resolution_matches_reconcile(db_path):
    """已保存的解析结果与对该版本重新校核的 timezone 报告一致。"""
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": _tz_scenario()})
        sid = c.json()["scenario_id"]
        saved = client.get(f"/scenarios/{sid}").json()["timezone_resolution"]
        fresh = client.post(f"/scenarios/{sid}/reconcile").json()["timezone"]
        assert saved == fresh


def test_gap_scenario_resolution_persisted(db_path):
    """春季跳时空洞场景的 infeasible 解析结果同样随版本保存。"""
    sc = _tz_scenario()
    sc["events"][0]["reading"] = "2026-03-08T02:30:00"  # 不存在的本地时间
    sc["constraints"] = []
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": sc})
        assert c.status_code == 200
        tz = c.json()["timezone_resolution"]
        assert tz["status"] == "infeasible"
        assert tz["gap_events"] == ["photo"]
        assert tz["contradiction"]["related_record_ids"]["timezones"] == [NY]
        sid = c.json()["scenario_id"]
        assert client.get(f"/scenarios/{sid}").json()["timezone_resolution"] == tz


# ----------------------------- 重启一致性 -----------------------------


def test_resolution_consistent_across_restart(db_path):
    """关闭并重开（重载应用、同一库文件）后读取结果逐字节一致。"""
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": _tz_scenario()})
        sid = c.json()["scenario_id"]
        before = client.get(f"/scenarios/{sid}").json()

    # 模拟进程重启：重新加载应用，同一数据库文件
    with make_client(db_path) as client2:
        after = client2.get(f"/scenarios/{sid}").json()
        assert after["timezone_resolution"] == before["timezone_resolution"]
        # 重启后对同一版本重新校核，结论仍与保存时一致
        fresh = client2.post(f"/scenarios/{sid}/reconcile").json()["timezone"]
        assert after["timezone_resolution"] == fresh


def test_store_level_restart_roundtrip(db_path):
    """存储层：Store 关闭后重开，timezone_resolution 完整还原。"""
    scenario = Scenario(**_tz_scenario())
    from app.engine import reconcile

    tz_report = reconcile(scenario).timezone
    store = Store(db_path)
    created = store.create(scenario, timezone_resolution=tz_report)
    store.close()

    store2 = Store(db_path)
    got = store2.get(created.scenario_id, 1)
    store2.close()
    assert got.timezone_resolution is not None
    assert got.timezone_resolution.model_dump() == tz_report.model_dump()
    assert got.timezone_resolution.fold_assignments == {"photo": 1}


# ----------------------------- 旧版本/旧库兼容 -----------------------------


def test_version_without_timezone_stays_readable(db_path):
    """未声明时区的版本：timezone_resolution 为 null，读取不受影响。"""
    sc = {
        "name": "legacy-fixed",
        "sources": [{"id": "cam", "declared_utc_offset_s": 28800}],
        "events": [{"id": "e", "source_id": "cam", "reading": "2026-09-14T08:00:00"}],
        "constraints": [],
    }
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": sc})
        assert c.status_code == 200
        assert c.json()["timezone_resolution"] is None
        got = client.get(f"/scenarios/{c.json()['scenario_id']}")
        assert got.status_code == 200
        assert got.json()["timezone_resolution"] is None
        assert got.json()["payload"]["sources"][0]["declared_utc_offset_s"] == 28800


def test_legacy_db_without_column_migrates(db_path):
    """旧库（无 timezone_resolution 列）打开时自动迁移，旧行可读且为 null。"""
    # 用旧 schema 手工建库并写入一行
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE scenarios ("
        " id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,"
        " created_at TEXT NOT NULL, parent_version INTEGER, note TEXT,"
        " payload TEXT NOT NULL, PRIMARY KEY (id, version))"
    )
    payload = json.dumps(
        {"name": "old-row", "sources": [], "anchors": [], "events": [], "constraints": []}
    )
    conn.execute(
        "INSERT INTO scenarios VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy1", 1, "old-row", "2026-01-01T00:00:00Z", None, None, payload),
    )
    conn.commit()
    conn.close()

    store = Store(db_path)  # 触发迁移
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(scenarios)")}
    assert "timezone_resolution" in cols
    got = store.get("legacy1", 1)
    assert got.name == "old-row"
    assert got.timezone_resolution is None
    # 迁移后新写入的版本正常持久化解析结果
    scenario = Scenario(**_tz_scenario())
    from app.engine import reconcile

    v2 = store.add_version(
        "legacy1", scenario, timezone_resolution=reconcile(scenario).timezone
    )
    assert v2.version == 2
    assert store.get("legacy1", 2).timezone_resolution is not None
    assert store.get("legacy1", 1).timezone_resolution is None
    store.close()


def test_invalid_scenario_still_storable_without_resolution(db_path):
    """声明了时区但校核校验失败的场景仍可入库（旧行为），解析结果为 null。"""
    sc = _tz_scenario()
    sc["events"].append(
        {"id": "bad", "source_id": "ghost", "reading": "2026-11-01T01:30:00"}
    )
    with make_client(db_path) as client:
        c = client.post("/scenarios", json={"scenario": sc})
        assert c.status_code == 200
        assert c.json()["timezone_resolution"] is None
        got = client.get(f"/scenarios/{c.json()['scenario_id']}")
        assert got.json()["timezone_resolution"] is None
        # 校核接口仍然按旧行为报 400
        r = client.post("/reconcile", json=sc)
        assert r.status_code == 400


def test_compare_behavior_unchanged_by_persistence(db_path):
    """/compare 不读取已保存的解析结果，行为与持久化前一致。"""
    with make_client(db_path) as client:
        c1 = client.post("/scenarios", json={"scenario": _tz_scenario(tolerance=3600.0)})
        sid = c1.json()["scenario_id"]
        client.post(
            f"/scenarios/{sid}/versions", json={"scenario": _tz_scenario(tolerance=1200.0)}
        )
        r = client.post(
            "/compare",
            json={"left_scenario_id": sid, "left_version": 1,
                  "right_scenario_id": sid, "right_version": 2},
        )
        assert r.status_code == 200
        diffs = {d["event_id"]: d for d in r.json()["fold_differences"]}
        assert diffs["photo"]["left_fold"] == 0
        assert diffs["photo"]["right_fold"] == 1
