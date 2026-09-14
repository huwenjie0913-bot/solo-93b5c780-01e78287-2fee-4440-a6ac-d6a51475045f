"""FastAPI 入口：校核、场景版本管理、方案比较。

启动：
    uvicorn app.main:app --reload
默认数据库：./data/forensic.db（可用环境变量 FORENSIC_DB 覆盖）。
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__
from .engine import ScenarioValidationError, diff_plans, reconcile
from .models import (
    PlanDifference,
    ReconcileResult,
    Scenario,
    ScenarioSummary,
    ScenarioVersion,
)
from .storage import Store

app = FastAPI(
    title="多源取证时间线校核 API",
    version=__version__,
    description=(
        "把相机 EXIF、门禁记录、设备日志等不同时钟来源的事件，结合校时锚点、"
        "线性漂移与“先于/同一事件/至少/至多间隔”约束，求解统一时间线；"
        "不可行时给出最小矛盾链，并提供限定修正幅度的偏移建议与方案差异比较。"
        "**所有持续时间均以秒为单位，统一时间轴为 UTC。**"
    ),
)


def _store() -> Store:
    return Store(os.environ.get("FORENSIC_DB", os.path.join("data", "forensic.db")))


@app.get("/health", tags=["系统"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.post(
    "/reconcile",
    response_model=ReconcileResult,
    tags=["校核"],
    summary="求解统一时间线（不入库）",
)
def reconcile_endpoint(
    scenario: Scenario,
    budget_s: Optional[float] = Query(
        None,
        ge=0,
        description="限定每个时钟来源允许的修正幅度上限（秒）；给定后返回偏移建议报告",
    ),
) -> ReconcileResult:
    try:
        return reconcile(scenario, budget_s=budget_s)
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ----------------------------- 场景版本 -----------------------------


class CreateVersionBody(BaseModel):
    scenario: Scenario
    note: Optional[str] = None
    scenario_id: Optional[str] = None


class AddVersionBody(BaseModel):
    scenario: Scenario
    note: Optional[str] = None
    parent_version: Optional[int] = None


@app.post("/scenarios", response_model=ScenarioVersion, tags=["场景版本"], summary="创建场景（v1）")
def create_scenario(body: CreateVersionBody) -> ScenarioVersion:
    store = _store()
    try:
        return store.create(body.scenario, body.scenario_id, body.note)
    finally:
        store.close()


@app.get("/scenarios", response_model=list[ScenarioSummary], tags=["场景版本"], summary="列出场景")
def list_scenarios() -> list[ScenarioSummary]:
    store = _store()
    try:
        return store.list_scenarios()
    finally:
        store.close()


@app.get(
    "/scenarios/{scenario_id}/versions",
    response_model=list[ScenarioSummary],
    tags=["场景版本"],
    summary="列出某场景的全部版本",
)
def list_versions(scenario_id: str) -> list[ScenarioSummary]:
    store = _store()
    try:
        return store.list_versions(scenario_id)
    finally:
        store.close()


@app.get(
    "/scenarios/{scenario_id}",
    response_model=ScenarioVersion,
    tags=["场景版本"],
    summary="读取场景指定版本（缺省为最新）",
)
def get_scenario(scenario_id: str, version: Optional[int] = None) -> ScenarioVersion:
    store = _store()
    try:
        return store.get(scenario_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


@app.post(
    "/scenarios/{scenario_id}/versions",
    response_model=ScenarioVersion,
    tags=["场景版本"],
    summary="为已有场景追加新版本",
)
def add_version(scenario_id: str, body: AddVersionBody) -> ScenarioVersion:
    store = _store()
    try:
        return store.add_version(scenario_id, body.scenario, body.parent_version, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


@app.post(
    "/scenarios/{scenario_id}/reconcile",
    response_model=ReconcileResult,
    tags=["校核"],
    summary="对已存版本执行校核（version 缺省为最新）",
)
def reconcile_stored(
    scenario_id: str,
    version: Optional[int] = None,
    budget_s: Optional[float] = Query(None, ge=0),
) -> ReconcileResult:
    store = _store()
    try:
        sv = store.get(scenario_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()
    try:
        return reconcile(sv.payload, budget_s=budget_s)
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ----------------------------- 方案比较 -----------------------------


class CompareBody(BaseModel):
    """比较左右两个方案：直接给场景，或给已存场景 ID（可带 version）。"""

    left: Optional[Scenario] = None
    right: Optional[Scenario] = None
    left_scenario_id: Optional[str] = None
    right_scenario_id: Optional[str] = None
    left_version: Optional[int] = None
    right_version: Optional[int] = None
    budget_s: Optional[float] = Field(
        None, ge=0, description="两侧计算最小修正预算时统一使用的修正幅度上限（秒）"
    )


def _resolve(body: CompareBody, side: str) -> tuple[Scenario, str]:
    inline = getattr(body, side)
    sid = getattr(body, f"{side}_scenario_id")
    ver = getattr(body, f"{side}_version")
    if inline is not None and sid is not None:
        raise HTTPException(
            status_code=400, detail=f"{side} 侧只能提供内联场景或已存场景 ID 之一"
        )
    if inline is not None:
        return inline, f"inline:{side}"
    if sid is not None:
        store = _store()
        try:
            sv = store.get(sid, ver)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            store.close()
        return sv.payload, f"{sid}@v{sv.version}"
    raise HTTPException(status_code=400, detail=f"缺少 {side} 侧方案")


@app.post("/compare", response_model=PlanDifference, tags=["方案比较"], summary="比较两个校核方案")
def compare(body: CompareBody) -> PlanDifference:
    left_s, left_ref = _resolve(body, "left")
    right_s, right_ref = _resolve(body, "right")
    try:
        left_r = reconcile(left_s, budget_s=body.budget_s)
        right_r = reconcile(right_s, budget_s=body.budget_s)
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return diff_plans(left_r, right_r, left_ref, right_ref, left_s, right_s)
