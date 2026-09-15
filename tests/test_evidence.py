"""证据保全包模块测试：Merkle 树、交接链、单主链校验与结构化拒收。"""

import base64
import hashlib
import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("FORENSIC_DB", tmp.name)
    monkeypatch.setenv("EVIDENCE_MAX_FILES", "100")
    monkeypatch.setenv("EVIDENCE_MAX_TOTAL_BYTES", str(64 * 1024))
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_entry(path: str, data: bytes):
    return {
        "path": path,
        "size": len(data),
        "digest": sha(data),
        "content_base64": b64(data),
    }


def package(version="v1", prev=None, files=None, handovers=None):
    if files is None:
        files = [
            file_entry("docs/笔录.txt", "询问笔录".encode("utf-8")),
            file_entry("photos/现场.jpg", b"\xff\xd8\xfffake-jpeg"),
        ]
    if handovers is None:
        handovers = [
            {
                "seq": 1,
                "timestamp": "2026-09-15T08:00:00+08:00",
                "handler": "张警官",
                "note": "初次接收",
            },
            {
                "seq": 2,
                "timestamp": "2026-09-15T10:30:00+08:00",
                "handler": "李鉴定人",
            },
        ]
    return {
        "version": version,
        "previous_root_hash": prev,
        "files": files,
        "handovers": handovers,
    }


def _body(resp):
    return resp.json() if hasattr(resp, "json") else resp


def codes(resp):
    return {e["code"] for e in _body(resp)["errors"]}


def locations(resp):
    return {e["location"] for e in _body(resp)["errors"]}


# ----------------------------- 正常路径 -----------------------------


def test_genesis_commit_and_determinism(client):
    payload = package()

    # 预检：accepted=true 但不写入
    pre = client.post("/evidence/packages/precheck", json=payload)
    assert pre.status_code == 200, pre.text
    pre_json = pre.json()
    assert pre_json["accepted"] is True
    assert pre_json["current_head"] is None
    assert client.get("/evidence/chain/head").json()["head"] is None

    cand = pre_json["candidate"]
    root = cand["root_hash"]
    assert len(root) == 64
    # 同一请求重复预检：根哈希完全一致（确定性）
    pre2 = client.post("/evidence/packages/precheck", json=payload).json()
    assert pre2["candidate"]["root_hash"] == root

    # 入库
    r = client.post("/evidence/packages", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["root_hash"] == root  # 预检与入库复算一致
    assert r.json()["chain_head"]["length"] == 1
    assert r.json()["committed_at"]

    # 链头
    head = client.get("/evidence/chain/head").json()["head"]
    assert head["version"] == "v1"
    assert head["root_hash"] == root


def test_merkle_levels_and_utf8_sort(client):
    files = [
        file_entry("中/文件A.txt", b"A"),
        file_entry("a/file.txt", b"a-file"),
        file_entry("a/目录.md", b"catalog"),
    ]
    r = client.post("/evidence/packages", json=package(version="v1", files=files))
    assert r.status_code == 201, r.text
    data = r.json()
    # 按 UTF-8 字节序：a/file.txt < a/目录.md < 中/文件A.txt
    ordered = [f["path"] for f in data["files"]]
    keys = [p.encode("utf-8") for p in ordered]
    assert keys == sorted(keys)
    assert ordered == ["a/file.txt", "a/目录.md", "中/文件A.txt"]
    # 逐层 Merkle：叶子 3 个 → 两层 → 根
    levels = data["merkle_levels"]
    assert len(levels) == 3
    assert levels[0]["level"] == 0 and len(levels[0]["nodes"]) == 3
    assert len(levels[-1]["nodes"]) == 1
    assert levels[-1]["nodes"][0] == data["merkle_root"]
    # 每个文件都带逐层证明
    for f in data["files"]:
        assert f["proof"], f["path"]
        assert f["proof"][-1]["computed_hash"] == data["merkle_root"]


def test_append_versions_and_head(client):
    r1 = client.post("/evidence/packages", json=package("v1"))
    root1 = r1.json()["root_hash"]

    h2 = [
        {"seq": 3, "timestamp": "2026-09-15T12:00:00Z", "handler": "王检察官"},
    ]
    r2 = client.post(
        "/evidence/packages", json=package("v2", prev=root1, handovers=h2)
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["predecessor_version"] == "v1"
    assert r2.json()["chain_head"]["length"] == 2
    assert r2.json()["handover_last_seq"] == 3

    # 列表与详情
    lst = client.get("/evidence/packages").json()
    assert [p["version"] for p in lst] == ["v1", "v2"]
    detail = client.get("/evidence/packages/v2").json()
    assert detail["root_hash"] == r2.json()["root_hash"]
    assert detail["chain_length"] == 2
    assert len(detail["files"]) == 2


def test_inclusion_proof_verified(client):
    payload = package()
    r = client.post("/evidence/packages", json=payload)
    root = r.json()["root_hash"]

    pr = client.get("/evidence/packages/v1/proof", params={"path": "docs/笔录.txt"})
    assert pr.status_code == 200, pr.text
    proof = pr.json()
    assert proof["verified"] is True
    assert proof["root_hash"] == root
    assert proof["leaf_hash"]
    assert proof["steps"]
    # canonical 叶子编码：键排序、紧凑
    assert proof["canonical_leaf"].startswith('{"digest":"')
    # 输入位置保留（docs 同时位于输入序与 UTF-8 字节序首位）
    assert proof["input_index"] == 0
    assert proof["sorted_index"] == 0  # docs < photos

    # 查询路径给 NFD 分解形式，服务端 NFC 归一后仍可命中
    nfd_path = "docs/笔录.txt"  # "录" 用分解序列
    import unicodedata

    nfd_path = unicodedata.normalize("NFD", nfd_path)
    if nfd_path != "docs/笔录.txt":
        pr2 = client.get("/evidence/packages/v1/proof", params={"path": nfd_path})
        assert pr2.status_code == 200
        assert pr2.json()["verified"] is True

    # 不存在文件 / 不存在版本
    assert client.get("/evidence/packages/v1/proof", params={"path": "nope"}).status_code == 404
    assert client.get("/evidence/packages/nope/proof", params={"path": "x"}).status_code == 404


def test_version_diff(client):
    f1 = [file_entry("keep.txt", b"v1"), file_entry("old.txt", b"old")]
    r1 = client.post("/evidence/packages", json=package("v1", files=f1))
    f2 = [file_entry("keep.txt", b"v2-content"), file_entry("new.txt", b"new")]
    h2 = [{"seq": 3, "timestamp": "2026-09-15T12:00:00Z", "handler": "王检察官"}]
    r2 = client.post(
        "/evidence/packages",
        json=package("v2", prev=r1.json()["root_hash"], files=f2, handovers=h2),
    )
    assert r2.status_code == 201, r2.text

    d = client.get("/evidence/packages/v1/diff/v2").json()
    assert [f["path"] for f in d["files_only_left"]] == ["old.txt"]
    assert [f["path"] for f in d["files_only_right"]] == ["new.txt"]
    changed = d["files_changed"]
    assert len(changed) == 1 and changed[0]["path"] == "keep.txt"
    # 差异项保留两侧输入位置
    assert changed[0]["left"]["input_index"] == 0
    assert d["handovers_left"]["count"] == 2
    assert d["handovers_right"]["first_seq"] == 3
    # 缺失版本 → 404
    assert client.get("/evidence/packages/v1/diff/nope").status_code == 404


# ----------------------------- 单文件校验失败（不写入） -----------------------------


def test_missing_content_rejected_not_written(client):
    payload = package()
    payload["files"][1]["content_base64"] = None
    pre = client.post("/evidence/packages/precheck", json=payload)
    assert pre.json()["accepted"] is False
    assert "missing_content" in codes(pre)
    assert "files[1].content_base64" in locations(pre)

    r = client.post("/evidence/packages", json=payload)
    assert r.status_code == 422
    assert "missing_content" in codes(r)
    assert client.get("/evidence/chain/head").json()["head"] is None  # 未写入


def test_digest_and_size_mismatch(client):
    payload = package()
    payload["files"][0]["digest"] = "a" * 64
    payload["files"][1]["size"] = 999
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert pre["accepted"] is False
    err_by_loc = {e["location"]: e for e in pre["errors"]}
    dm = err_by_loc["files[0].digest"]
    assert dm["code"] == "digest_mismatch"
    assert dm["expected"] == sha("询问笔录".encode("utf-8"))
    assert dm["actual"] == "a" * 64
    assert err_by_loc["files[1].size"]["code"] == "size_mismatch"


def test_invalid_base64_and_digest_format(client):
    payload = package()
    payload["files"][0]["content_base64"] = "@@@not-base64@@@"
    payload["files"][1]["digest"] = "xyz"
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert {"invalid_base64", "digest_invalid"} <= codes(pre)


def test_path_bounds_and_nfc_collision(client):
    for bad in ["../escape.txt", "/abs/path", "win\\path", "a//b", "trailing/", ""]:
        files = [{
            "path": bad,
            "size": 1,
            "digest": sha(b"x"),
            "content_base64": b64(b"x"),
        }]
        pre = client.post("/evidence/packages/precheck", json=package(files=files)).json()
        assert "path_out_of_bounds" in codes(pre) or "path_empty" in codes(pre), bad

    import unicodedata

    nfc = unicodedata.normalize("NFC", "café.txt")
    nfd = unicodedata.normalize("NFD", "café.txt")
    assert nfc != nfd
    payload = package(files=[file_entry(nfd, b"1"), file_entry(nfc, b"2")])
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert not pre["accepted"]
    coll = next(e for e in pre["errors"] if e["code"] == "path_collision")
    assert coll["location2"].startswith("files[")


# ----------------------------- 交接记录 -----------------------------


def test_handover_sequence_and_time(client):
    # 包内序号不连续
    bad_seq = package(handovers=[
        {"seq": 1, "timestamp": "2026-09-15T08:00:00Z", "handler": "a"},
        {"seq": 3, "timestamp": "2026-09-15T09:00:00Z", "handler": "b"},
    ])
    pre = client.post("/evidence/packages/precheck", json=bad_seq).json()
    err = next(e for e in pre["errors"] if e["code"] == "handover_sequence_gap")
    assert err["location"] == "handovers[1].seq"
    assert err["expected"] == "2"

    # 时刻倒退
    back = package(handovers=[
        {"seq": 1, "timestamp": "2026-09-15T09:00:00Z", "handler": "a"},
        {"seq": 2, "timestamp": "2026-09-15T08:00:00Z", "handler": "b"},
    ])
    pre = client.post("/evidence/packages/precheck", json=back).json()
    assert "handover_time_regression" in codes(pre)

    # naive 时刻（无时区）非法
    naive = package(handovers=[
        {"seq": 1, "timestamp": "2026-09-15T09:00:00", "handler": "a"},
    ])
    pre = client.post("/evidence/packages/precheck", json=naive).json()
    assert "timestamp_invalid" in codes(pre)

    # 空交接
    pre = client.post("/evidence/packages/precheck",
                      json=package(handovers=[])).json()
    assert "handovers_empty" in codes(pre)


def test_cross_package_chain_rules(client):
    r1 = client.post("/evidence/packages", json=package("v1"))
    root1 = r1.json()["root_hash"]

    # 未知前序（断链）
    r = client.post(
        "/evidence/packages",
        json=package("vx", prev="a" * 64),
    )
    assert r.status_code == 409
    assert "unknown_predecessor" in codes(r)

    # 主链已存在后再来创世包
    r = client.post("/evidence/packages", json=package("genesis2", prev=None))
    assert r.status_code == 409
    assert "genesis_exists" in codes(r)

    # 合法接入 v2
    h2 = [{"seq": 3, "timestamp": "2026-09-15T12:00:00Z", "handler": "王"}]
    r2 = client.post("/evidence/packages", json=package("v2", prev=root1, handovers=h2))
    assert r2.status_code == 201, r2.text

    # 双重后继：另一个版本也想接在 v1 后
    r = client.post(
        "/evidence/packages",
        json=package("v2-fork", prev=root1, handovers=h2),
    )
    assert r.status_code == 409
    assert "double_successor" in codes(r)
    fork_err = next(e for e in r.json()["errors"] if e["code"] == "double_successor")
    assert "v2" in fork_err["expected"]
    # 链头仍是 v2，未写入 fork
    assert client.get("/evidence/chain/head").json()["head"]["version"] == "v2"

    # 即使同时存在序号错误（422 类），链冲突仍必须以 409 暴露
    fork_bad = package(
        "v2-fork2",
        prev=root1,
        handovers=[{"seq": 99, "timestamp": "2026-09-15T12:00:00Z", "handler": "王"}],
    )
    r = client.post("/evidence/packages", json=fork_bad)
    assert r.status_code == 409
    assert {"double_successor", "handover_sequence_gap"} <= codes(r)

    # 重复版本号
    r = client.post("/evidence/packages", json=package("v2", prev=root1, handovers=h2))
    assert r.status_code == 409
    assert "duplicate_version" in codes(r)

    # 承接序号必须连续：v2 末条为 3，新包应从 4 开始
    h_bad = [{"seq": 3, "timestamp": "2026-09-15T13:00:00Z", "handler": "赵"}]
    r = client.post(
        "/evidence/packages",
        json=package("v3-bad", prev=r2.json()["root_hash"], handovers=h_bad),
    )
    assert r.status_code == 422
    err = next(e for e in r.json()["errors"] if e["code"] == "handover_sequence_gap")
    assert err["expected"] == "4"

    # 跨包时刻倒退：v2 末次交接为 12:00Z，v3 早于它
    h_back = [{"seq": 4, "timestamp": "2026-09-15T11:00:00Z", "handler": "赵"}]
    r = client.post(
        "/evidence/packages",
        json=package("v3-back", prev=r2.json()["root_hash"], handovers=h_back),
    )
    assert r.status_code == 422
    assert "handover_time_regression" in codes(r)


def test_capacity_limits(client, monkeypatch):
    monkeypatch.setenv("EVIDENCE_MAX_FILES", "1")
    payload = package(files=[file_entry("a", b"1"), file_entry("b", b"2")])
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert "capacity_exceeded_files" in codes(pre)

    monkeypatch.setenv("EVIDENCE_MAX_TOTAL_BYTES", "3")
    payload = package(files=[file_entry("big", b"xxxx")])
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert "capacity_exceeded_bytes" in codes(pre)


def test_multiple_errors_collected(client):
    """一次请求返回全部冲突，而不是首个失败即止。"""
    payload = package()
    payload["files"][0]["digest"] = "bad"
    payload["files"][1]["content_base64"] = None
    payload["handovers"] = []
    pre = client.post("/evidence/packages/precheck", json=payload).json()
    assert {"digest_invalid", "missing_content", "handovers_empty"} <= codes(pre)


def test_proof_rejects_tampered_sibling():
    """逐层证明对兄弟节点/根的任何改动都验证失败。"""
    from app.evidence import build_levels, build_proof_steps, leaf_hash, verify_proof

    leaves = [leaf_hash(f"f{i}.bin", i, sha(bytes([i]))) for i in range(5)]
    hashes = [lh for lh, _ in leaves]
    levels = build_levels(hashes)
    root = levels[-1][0]
    steps = build_proof_steps(levels, 2)
    assert verify_proof(hashes[2], steps, root) is True

    # 篡改某层兄弟哈希
    bad_steps = [s.model_copy(deep=True) for s in steps]
    first_sibling = next(s for s in bad_steps if s.sibling_hash)
    flipped = ("0" if first_sibling.sibling_hash[0] != "0" else "1") + first_sibling.sibling_hash[1:]
    first_sibling.sibling_hash = flipped
    assert verify_proof(hashes[2], bad_steps, root) is False

    # 声称的根不同也失败
    assert verify_proof(hashes[2], steps, "f" * 64) is False


def test_handover_chain_hashes_linked(client):
    r = client.post("/evidence/packages", json=package("v1")).json()
    hs = r["handovers"]
    assert hs[0]["prev_hash"] == "0" * 64
    assert hs[1]["prev_hash"] == hs[0]["hash"]
    assert r["handover_head_hash"] == hs[-1]["hash"]
    # 时刻归一为 UTC Z：+08:00 08:00 → 00:00Z
    assert hs[0]["timestamp"] == "2026-09-15T00:00:00Z"
