"""FastAPI 端到端接口测试（含 SQLite 场景版本库与方案比较）。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["FORENSIC_DB"] = tmp.name
    # 每个用例重新导入 app 以便 _store 读取环境变量
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def scenario_payload():
    return {
        "name": "门廊事件",
        "sources": [
            {"id": "door", "declared_utc_offset_s": 28800, "base_uncertainty_s": 1.0},
            {"id": "cam", "declared_utc_offset_s": 28800, "base_uncertainty_s": 1.0},
        ],
        "anchors": [
            {
                "id": "ntp-door",
                "source_id": "door",
                "clock_reading": "2026-09-14T08:00:00+08:00",
                "reference_time": "2026-09-14T00:00:05Z",
                "reference_uncertainty_s": 0.5,
            }
        ],
        "events": [
            {"id": "badge", "source_id": "door",
             "reading": "2026-09-14T08:00:00+08:00", "reading_uncertainty_s": 0.0},
            {"id": "photo", "source_id": "cam",
             "reading": "2026-09-14T08:02:00+08:00", "reading_uncertainty_s": 0.0},
        ],
        "constraints": [
            {"id": "c-order", "type": "before", "a": "badge", "b": "photo"}
        ],
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_reconcile_and_units(client):
    r = client.post("/reconcile", json=scenario_payload())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    assert len(data["unified_timeline"]) == 2
    first = data["unified_timeline"][0]
    assert first["earliest_unix_s"]["unit"] == "unix_seconds_utc"
    assert first["width_s"]["unit"] == "s"
    assert first["earliest"].endswith("Z")
    # 每个量化结果都带来源/事件/推导路径
    assert first["earliest_unix_s"]["derived_by"]
    assert first["earliest_unix_s"]["event_ids"]


def test_reconcile_contradiction_and_adjustment(client):
    payload = scenario_payload()
    payload["constraints"] = [
        {"id": "c-wrong", "type": "before", "a": "photo", "b": "badge"}
    ]
    # 无预算：返回矛盾链 + 最小所需预算
    r = client.post("/reconcile", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is False
    con = data["contradiction"]
    assert set(con["cycle_event_ids"]) == {"badge", "photo"}
    assert con["related_record_ids"]["sources"]
    assert data["adjustment"]["min_required_budget_s"] > 0

    # 预算足够：可行建议
    r = client.post("/reconcile", json=payload, params={"budget_s": 200})
    data = r.json()
    assert data["adjustment"]["feasible"] is True
    assert len(data["adjustment"]["adjustments"]) == 2
    for adj in data["adjustment"]["adjustments"]:
        assert adj["suggested_shift_s"]["unit"] == "s"
        lo, hi = adj["shift_feasible_range_s"]
        assert lo <= adj["suggested_shift_s"]["value"] <= hi


def test_validation_error_400(client):
    payload = scenario_payload()
    payload["events"][0]["source_id"] = "missing"
    r = client.post("/reconcile", json=payload)
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_scenario_versioning_and_stored_reconcile(client):
    payload = scenario_payload()
    r = client.post("/scenarios", json={"scenario": payload, "note": "初版"})
    assert r.status_code == 200, r.text
    sid = r.json()["scenario_id"]
    assert r.json()["version"] == 1

    # 追加版本（加入过强约束）
    payload["constraints"].append(
        {"id": "c-min", "type": "min_interval", "a": "badge", "b": "photo",
         "min_s": 10000}
    )
    r = client.post(
        f"/scenarios/{sid}/versions", json={"scenario": payload, "note": "加严"}
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["parent_version"] == 1

    versions = client.get(f"/scenarios/{sid}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]

    # 校核指定版本
    r = client.post(f"/scenarios/{sid}/reconcile", params={"version": 1})
    assert r.status_code == 200
    assert r.json()["feasible"] is True
    r = client.post(f"/scenarios/{sid}/reconcile")  # 最新版
    assert r.json()["feasible"] is False

    # 404
    assert client.get("/scenarios/nope").status_code == 404


def test_compare_inline_plans(client):
    left = scenario_payload()
    right = scenario_payload()
    right["constraints"] = [
        {"id": "c-wrong", "type": "before", "a": "photo", "b": "badge"}
    ]
    r = client.post(
        "/compare",
        json={"left": left, "right": right, "budget_s": 200},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["feasible_left"] is True
    assert d["feasible_right"] is False
    assert d["constraints_only_left"] == ["c-order"]
    assert d["constraints_only_right"] == ["c-wrong"]
    assert d["min_budget_right_s"] is not None


def test_compare_stored_refs(client):
    p = scenario_payload()
    r = client.post("/scenarios", json={"scenario": p})
    sid = r.json()["scenario_id"]
    r = client.post(
        "/compare",
        json={"left": p, "left_scenario_id": None,
              "right_scenario_id": sid, "right_version": 1},
    )
    assert r.status_code == 200
    assert r.json()["feasible_left"] == r.json()["feasible_right"]
