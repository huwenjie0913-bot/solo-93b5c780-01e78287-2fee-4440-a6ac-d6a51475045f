"""证据保全包路由：预检、入库、链头、版本列表/详情、版本对比、单文件包含证明。

预检与入库对**同一请求**执行同一套确定性复算（``evaluate_package``）：
预检只读链快照，入库在 ``BEGIN IMMEDIATE`` 写锁内重建快照后再算一遍，
因此预检通过到入库之间若主链被他人推进，入库仍会被正确拒绝并给出
结构化原因，不会写入半成品。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .evidence import (
    EvidencePackageIn,
    InclusionProof,
    PackageComputed,
    PrecheckResult,
    build_proof_steps,
    configured_limits,
    evaluate_package,
    leaf_hash,
    nfc,
    normalize_root_hash,
    verify_proof,
)
from .evidence_storage import EvidenceStore, EvidenceVersionNotFound

router = APIRouter(prefix="/evidence", tags=["证据保全包"])


def _store() -> EvidenceStore:
    return EvidenceStore(
        os.environ.get("FORENSIC_DB", os.path.join("data", "forensic.db"))
    )


def _rejection(
    errors: list[EvidenceError], status_code: int = 422
) -> JSONResponse:
    """结构化拒收响应：accepted=false + 全部原因（含输入位置），不写入。"""

    return JSONResponse(
        status_code=status_code,
        content={
            "accepted": False,
            "errors": [e.model_dump() for e in errors],
            "candidate": None,
            "current_head": None,
        },
    )


def _chain_error_codes(errors: list) -> set[str]:
    return {e.code for e in errors} & {
        "double_successor",
        "duplicate_version",
        "genesis_exists",
        "unknown_predecessor",
    }


# ----------------------------- 预检 / 入库 -----------------------------


@router.post(
    "/packages/precheck",
    response_model=PrecheckResult,
    summary="预检证据包（不写入）：复算根哈希并返回全部冲突位置",
)
def precheck(pkg: EvidencePackageIn) -> PrecheckResult:
    max_files, max_total_bytes = configured_limits()
    store = _store()
    try:
        prev_root, _ = normalize_root_hash(pkg.previous_root_hash)
        snap = store.snapshot(pkg.version.strip(), prev_root)
        current_head = store.head()
    finally:
        store.close()
    accepted, errors, candidate = evaluate_package(
        pkg, snap, max_files=max_files, max_total_bytes=max_total_bytes
    )
    return PrecheckResult(
        accepted=accepted,
        errors=errors,
        candidate=candidate,
        current_head=current_head,
    )


@router.post(
    "/packages",
    response_model=PackageComputed,
    status_code=201,
    summary="校验并接入证据包（通过全部条件才写入主链）",
)
def commit(pkg: EvidencePackageIn) -> PackageComputed:
    max_files, max_total_bytes = configured_limits()
    store = _store()
    try:
        with store.write_lock():
            # 锁内重建快照：消除“预检后、入库前”主链被推进的 TOCTOU 窗口
            prev_root, _ = normalize_root_hash(pkg.previous_root_hash)
            snap = store.snapshot(pkg.version.strip(), prev_root)
            accepted, errors, candidate = evaluate_package(
                pkg, snap, max_files=max_files, max_total_bytes=max_total_bytes
            )
            if not accepted:
                chain_codes = _chain_error_codes(errors)
                # 双重后继/重复版本/创世冲突/断链属于链状态冲突 → 409，其余 422
                status = 409 if chain_codes else 422
                raise _HttpRejection(_rejection(errors, status_code=status))
            assert candidate is not None
            store.insert(candidate)
        return candidate
    except _HttpRejection as exc:
        return exc.response  # type: ignore[return-value]
    finally:
        store.close()


class _HttpRejection(Exception):
    """在写事务内携带结构化 JSONResponse 一路抛到锁外（保证回滚）。"""

    def __init__(self, response: JSONResponse):
        self.response = response
        super().__init__("evidence package rejected")


# ----------------------------- 链头 / 版本读取 -----------------------------


@router.get("/chain/head", summary="主链链头")
def chain_head():
    store = _store()
    try:
        return {"head": store.head()}
    finally:
        store.close()


@router.get("/packages", summary="按接入顺序列出全部证据包版本")
def list_packages():
    store = _store()
    try:
        return store.list_packages()
    finally:
        store.close()


@router.get(
    "/packages/{version}",
    summary="读取证据包详情（逐层 Merkle、交接链哈希、逐文件证明）",
)
def get_package(version: str):
    store = _store()
    try:
        return store.get_detail(version)
    except EvidenceVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


# ----------------------------- 版本对比 -----------------------------


@router.get(
    "/packages/{version}/diff/{other_version}",
    summary="比较两个已入库版本（文件增删改、交接序号区间，含输入位置）",
)
def diff_packages(version: str, other_version: str):
    store = _store()
    try:
        return store.diff(version, other_version)
    except EvidenceVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


# ----------------------------- 单文件包含证明 -----------------------------


@router.get(
    "/packages/{version}/proof",
    response_model=InclusionProof,
    summary="单文件包含证明：用逐层兄弟节点独立复算并验证包根哈希",
)
def inclusion_proof(
    version: str,
    path: str = Query(..., description="文件路径；服务端做 NFC 归一后匹配（与入库同规则）"),
):
    store = _store()
    try:
        try:
            detail = store.get_detail(version)
            norm = nfc(path)
            file_row = store.get_file_row(version, norm)
        except EvidenceVersionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()

    sorted_index = file_row["sorted_index"]
    input_index = file_row["input_index"]
    levels_raw = [m.nodes for m in detail.merkle_levels]
    _, canonical_leaf = leaf_hash(norm, file_row["size"], file_row["digest"])
    steps = build_proof_steps(levels_raw, sorted_index)
    # 独立复算叶子与逐层哈希，验证而不直接信任存储的逐层值
    verified = verify_proof(file_row["leaf_hash"], steps, detail.merkle_root)
    # 找到对应输入位置（提交时 files[input_index]）
    stored = next(f for f in detail.files if f.path == norm)
    return InclusionProof(
        version=version,
        path=norm,
        path_input=path,
        input_index=input_index,
        sorted_index=sorted_index,
        canonical_leaf=canonical_leaf,
        leaf_hash=stored.leaf_hash,
        steps=steps,
        merkle_root=detail.merkle_root,
        root_hash=detail.root_hash,
        verified=verified,
    )
