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
    TimezoneReport,
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


def _timezone_resolution_for_save(scenario: Scenario) -> Optional[TimezoneReport]:
    """写库前计算时区解析结果（时区声明、候选 UTC、采用偏移/ fold 与依据）。

    未声明 IANA 时区的场景返回 None（不持久化，读取为 null）；声明了时区但
    场景本身未通过校核校验（如引用不存在的来源）时也返回 None——保持旧行为：
    非法场景仍可入库，校核接口才报错。解析结果与
    ``POST /scenarios/{id}/reconcile`` 默认参数下的 ``timezone`` 报告一致。
    """
    if not any(s.iana_timezone for s in scenario.sources):
        return None
    try:
        return reconcile(scenario).timezone
    except ScenarioValidationError:
        return None


# 关联求解搜索上限的查询参数（在 max_hypotheses / max_search_nodes 内返回前 top_k 个假设）
AssocTopK = Query(3, ge=1, le=100, description="返回的关联假设个数上限 K")
AssocMaxHypotheses = Query(
    100, ge=1, le=100_000, description="关联求解收集的可行假设总数上限"
)
AssocMaxSearchNodes = Query(
    10_000, ge=1, le=1_000_000, description="关联求解展开的搜索节点数上限"
)
TzMaxSearchNodes = Query(
    10_000, ge=1, le=1_000_000, description="IANA 时区 fold 组合搜索展开的节点数上限"
)


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
    top_k: int = AssocTopK,
    max_hypotheses: int = AssocMaxHypotheses,
    max_search_nodes: int = AssocMaxSearchNodes,
    tz_max_search_nodes: int = TzMaxSearchNodes,
) -> ReconcileResult:
    try:
        return reconcile(
            scenario,
            budget_s=budget_s,
            assoc_top_k=top_k,
            assoc_max_hypotheses=max_hypotheses,
            assoc_max_search_nodes=max_search_nodes,
            tz_max_search_nodes=tz_max_search_nodes,
        )
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
        return store.create(
            body.scenario,
            body.scenario_id,
            body.note,
            timezone_resolution=_timezone_resolution_for_save(body.scenario),
        )
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
        return store.add_version(
            scenario_id,
            body.scenario,
            body.parent_version,
            body.note,
            timezone_resolution=_timezone_resolution_for_save(body.scenario),
        )
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
    top_k: int = AssocTopK,
    max_hypotheses: int = AssocMaxHypotheses,
    max_search_nodes: int = AssocMaxSearchNodes,
    tz_max_search_nodes: int = TzMaxSearchNodes,
) -> ReconcileResult:
    store = _store()
    try:
        sv = store.get(scenario_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()
    try:
        return reconcile(
            sv.payload,
            budget_s=budget_s,
            assoc_top_k=top_k,
            assoc_max_hypotheses=max_hypotheses,
            assoc_max_search_nodes=max_search_nodes,
            tz_max_search_nodes=tz_max_search_nodes,
        )
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
    top_k: int = Field(3, ge=1, le=100, description="两侧关联求解返回的假设个数上限 K")
    max_hypotheses: int = Field(
        100, ge=1, le=100_000, description="两侧关联求解收集的可行假设总数上限"
    )
    max_search_nodes: int = Field(
        10_000, ge=1, le=1_000_000, description="两侧关联求解展开的搜索节点数上限"
    )
    tz_max_search_nodes: int = Field(
        10_000, ge=1, le=1_000_000, description="两侧 IANA 时区 fold 组合搜索的节点数上限"
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
        left_r = reconcile(
            left_s,
            budget_s=body.budget_s,
            assoc_top_k=body.top_k,
            assoc_max_hypotheses=body.max_hypotheses,
            assoc_max_search_nodes=body.max_search_nodes,
            tz_max_search_nodes=body.tz_max_search_nodes,
        )
        right_r = reconcile(
            right_s,
            budget_s=body.budget_s,
            assoc_top_k=body.top_k,
            assoc_max_hypotheses=body.max_hypotheses,
            assoc_max_search_nodes=body.max_search_nodes,
            tz_max_search_nodes=body.tz_max_search_nodes,
        )
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return diff_plans(left_r, right_r, left_ref, right_ref, left_s, right_s)
