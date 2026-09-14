"""Pydantic 输入/输出模型。

所有时间戳均为 ISO 8601 字符串；带偏移（如 ``2026-09-14T08:00:00+08:00``）
时按该偏移换算到统一时间（UTC Unix 秒），不带偏移（naive）时回退到
``Scenario.default_utc_offset_s``。

所有持续时间字段统一以**秒**为单位。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ----------------------------- 输入模型 -----------------------------


class SourceClock(BaseModel):
    """一个时钟来源（相机 EXIF / 门禁主机 / 设备日志等）。

    ``declared_utc_offset_s``：该来源原始读数声称所处的 UTC 偏移（秒），
    例如东八区为 28800。仅用于解析 naive 读数，不代表时钟准确。
    ``drift_ppm``：先验走时漂移量级（ppm，无量纲），无校时锚点时用于
    估计该时钟的外推不确定度。
    """

    id: str = Field(..., description="来源 ID，如 camera-01 / access-ctrl-A")
    name: Optional[str] = Field(None, description="可读名称")
    kind: Optional[str] = Field(None, description="来源类型，如 exif / access_log / device_log")
    declared_utc_offset_s: float = Field(0.0, description="来源读数声称的 UTC 偏移（秒）")
    drift_ppm: float = Field(0.0, ge=0.0, description="先验漂移量级（ppm）")
    base_uncertainty_s: float = Field(
        0.0,
        ge=0.0,
        description="时钟绝对误差的基线不确定度（秒），即使有锚点也叠加",
    )


class CalibrationAnchor(BaseModel):
    """校时锚点：同一时刻下“来源时钟读数”与“参考真实时刻”的对应关系。"""

    id: str = Field(..., description="锚点 ID，如 ntp-sync-1")
    source_id: str = Field(..., description="所属时钟来源 ID")
    clock_reading: str = Field(..., description="来源时钟读数，ISO 8601")
    reference_time: str = Field(
        ...,
        description="参考真实时刻（UTC 或带偏移 ISO 8601），如 NTP/GPS 校时结果"
    )
    reference_uncertainty_s: float = Field(
        0.0, ge=0.0, description="参考时刻自身的不确定度半宽（秒）"
    )
    note: Optional[str] = None


class Event(BaseModel):
    """一条带不确定范围的事件记录。"""

    id: str = Field(..., description="事件 ID，如 photo-0042")
    source_id: Optional[str] = Field(
        None,
        description="记录该事件的时钟来源 ID；为 null 表示时间戳已是统一参考时间",
    )
    reading: str = Field(..., description="来源时钟读数或参考时间，ISO 8601")
    reading_uncertainty_s: float = Field(
        0.0, ge=0.0, description="读数本身（记录粒度/抖动）的不确定半宽（秒）"
    )
    label: Optional[str] = Field(None, description="事件描述")
    payload: Optional[dict] = Field(None, description="保留的原始记录字段")


class Constraint(BaseModel):
    """事件之间的差分约束。单位均为秒。"""

    id: str = Field(..., description="约束 ID")
    type: Literal["before", "same_event", "min_interval", "max_interval"] = Field(
        ..., description="约束类型"
    )
    a: str = Field(..., description="事件 A 的 ID")
    b: str = Field(..., description="事件 B 的 ID")
    min_s: Optional[float] = Field(
        None, ge=0.0, description="type=min_interval：A 先于 B 至少这么多秒"
    )
    max_s: Optional[float] = Field(
        None, ge=0.0, description="type=max_interval：A 先于 B 至多这么多秒"
    )
    tolerance_s: float = Field(
        0.0, ge=0.0, description="same_event 的同一判定容差（秒），允许 |tA-tB|≤容差"
    )
    note: Optional[str] = None


class AssociationCandidate(BaseModel):
    """关联组中基准事件的一个候选“同一事件”对象。"""

    event_id: str = Field(..., description="候选事件 ID")
    tolerance_s: float = Field(
        0.0, ge=0.0, description="匹配容差（秒）：选中后要求 |t_base − t_cand| ≤ 容差"
    )
    cost: float = Field(
        0.0, ge=0.0, description="选中该候选时计入假设总代价的代价（≥0，缺省 0）"
    )
    note: Optional[str] = None


class AssociationGroup(BaseModel):
    """一个候选关联组：为基准事件从候选列表中择一（或至多择一）配对。

    ``exactly_one``：必须恰好选中一个候选；``at_most_one``：可选中一个，
    也可以一个都不选（跳过）。候选事件为全局资源：同一候选事件
    不能被多个关联组同时选中（不可跨组复用）。
    """

    id: str = Field(..., description="关联组 ID，如 g-badge")
    base_event_id: str = Field(..., description="基准事件 ID")
    mode: Literal["exactly_one", "at_most_one"] = Field(
        "exactly_one", description="选择基数：恰好一个 / 至多一个"
    )
    candidates: list[AssociationCandidate] = Field(
        default_factory=list, description="候选事件列表（exactly_one 时不能为空）"
    )
    note: Optional[str] = None


class Scenario(BaseModel):
    """一次校核的完整场景输入。"""

    name: str = Field(..., description="场景名称")
    description: Optional[str] = None
    default_utc_offset_s: float = Field(
        0.0,
        description="解析 naive 时间戳时采用的 UTC 偏移（秒），默认 0（UTC）",
    )
    sources: list[SourceClock] = Field(default_factory=list)
    anchors: list[CalibrationAnchor] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    association_groups: list[AssociationGroup] = Field(
        default_factory=list,
        description="候选事件关联组；缺省为空，行为与旧版本完全一致",
    )


# ----------------------------- 输出模型 -----------------------------


class Quantity(BaseModel):
    """带单位、来源与推导路径的量化结果。"""

    value: float = Field(..., description="数值")
    unit: str = Field(..., description="单位，如 s / ppm / iso8601")
    source_ids: list[str] = Field(default_factory=list, description="关联的来源 ID")
    anchor_ids: list[str] = Field(default_factory=list, description="关联的锚点 ID")
    event_ids: list[str] = Field(default_factory=list, description="关联的事件 ID")
    derived_by: str = Field(..., description="推导方法标识")
    detail: Optional[str] = Field(None, description="人可读推导说明")


class EventWindow(BaseModel):
    earliest: str
    latest: str
    earliest_unix_s: Quantity
    latest_unix_s: Quantity
    representative: str = Field(..., description="建议采用的统一时刻（区间中点），ISO 8601")
    width_s: Quantity
    source_id: Optional[str]


class ConstraintSlack(BaseModel):
    constraint_id: str
    type: str
    a: str
    b: str
    satisfiable: bool
    slack_s: Optional[float] = Field(None, description="约束余量（秒，≥0；为 0 表示紧约束）")
    detail: str


class Contradiction(BaseModel):
    """最小矛盾链（差分系统中的负环）。"""

    cycle_event_ids: list[str] = Field(..., description="矛盾链上的事件，按环顺序排列")
    cycle_constraint_ids: list[str] = Field(
        ..., description="闭合该环所经过的原始约束/区间边 ID"
    )
    total_slack_s: float = Field(..., description="环上约束余量之和（<0，超出量）")
    related_record_ids: dict[str, list[str]] = Field(
        default_factory=dict,
        description="关联的原始记录：events / constraints / sources / anchors",
    )
    explanation: str


class ClockModel(BaseModel):
    """单个来源时钟的拟合结果。"""

    source_id: str
    offset_s: Quantity = Field(..., description="参考历元处 clock - true 的偏移（秒）")
    drift_ppm: Quantity = Field(..., description="线性漂移率（ppm，clock 相对 true）")
    reference_epoch: str
    anchor_count: int
    fit_rms_residual_s: Optional[float] = None
    extrapolation: bool = Field(False, description="事件是否落在锚点覆盖范围之外")
    warnings: list[str] = Field(default_factory=list)


class EventIntervalInput(BaseModel):
    """换算到统一时间后的事件一元区间（构造差分系统的输入）。"""

    event_id: str
    source_id: Optional[str]
    lo: float
    hi: float
    quantity: Quantity


class SourceAdjustment(BaseModel):
    source_id: str
    suggested_shift_s: Quantity = Field(..., description="建议对该时钟偏移施加的修正量（秒）")
    shift_feasible_range_s: tuple[float, float] = Field(
        ..., description="给定预算下该修正量本身的可行区间（秒）"
    )
    clock_model_offset_s: float = Field(..., description="当前模型偏移（秒）")


class AdjustmentReport(BaseModel):
    """限定修正幅度的偏移建议。"""

    budget_s: Optional[float] = Field(None, description="调用时给定的修正幅度上限（秒）")
    feasible: bool = Field(..., description="预算内是否可使全部约束成立")
    min_required_budget_s: Optional[float] = Field(
        None, description="使约束成立所需的最小 L∞ 修正预算（秒，二分搜索结果）"
    )
    adjustments: list[SourceAdjustment] = Field(default_factory=list)
    residual_contradiction: Optional[Contradiction] = None
    method: str


class AssociationPairing(BaseModel):
    """一个假设中被选中的配对（基准事件 ↔ 候选事件）。"""

    group_id: str
    base_event_id: str
    candidate_event_id: str
    constraint_id: str = Field(
        ..., description="该配对生成的 same_event 约束 ID（assoc:<组ID>），可在约束余量中追溯"
    )
    tolerance_s: float
    cost: float
    time_residual_s: float = Field(
        ..., description="统一时间线下两事件可行窗口的最小间距（秒，0 表示窗口相交）"
    )


class ScoreComponent(BaseModel):
    """假设评分的单项组成（一个选中配对的贡献）。"""

    group_id: str
    candidate_event_id: str
    cost: float
    time_residual_s: float


class HypothesisScore(BaseModel):
    """可追溯的假设评分：总代价 + 时间残差 + 逐项组成。"""

    total_cost: float = Field(..., description="全部选中配对的候选代价之和")
    time_residual_s: float = Field(..., description="全部选中配对的时间残差之和（秒）")
    components: list[ScoreComponent] = Field(default_factory=list)
    derived_by: str
    detail: str


class AssociationHypothesis(BaseModel):
    """一个可行的关联假设：选中的配对 + 统一时间线 + 约束余量 + 评分。"""

    rank: int = Field(..., description="按（总代价, 时间残差, 发现序）稳定排序后的名次，从 1 开始")
    pairings: list[AssociationPairing] = Field(default_factory=list)
    score: HypothesisScore
    unified_timeline: list[EventWindow] = Field(default_factory=list)
    constraint_slack: list[ConstraintSlack] = Field(default_factory=list)


class GroupElimination(BaseModel):
    """搜索中某关联组被剪枝的统计与样本矛盾链。"""

    group_id: str
    base_event_id: str
    pruned_branches: int = Field(
        ..., description="该组层面上被差分约束校核剪掉的候选分支总数（跨整棵搜索树累计）"
    )
    total_options: int = Field(..., description="该组的可选项数（候选数，at_most_one 含跳过项）")
    sample_contradiction: Optional[Contradiction] = Field(
        None, description="该组首个被剪枝分支的矛盾链样本"
    )


class AssociationSearchStats(BaseModel):
    """关联求解的搜索统计。"""

    groups_total: int
    nodes_expanded: int = Field(..., description="展开的搜索节点数（含根节点）")
    branches_pruned: int = Field(..., description="被差分约束校核剪枝的分支数")
    reuse_conflicts: int = Field(
        ..., description="因候选事件跨组复用冲突而被跳过的分支数"
    )
    leaves_feasible: int = Field(..., description="找到的可行完整假设数（≤ max_hypotheses）")
    top_k: int
    max_hypotheses: int
    max_search_nodes: int
    truncated: bool
    truncation_reason: Optional[str] = Field(
        None, description="截断原因：node_limit / hypothesis_limit；未截断为 null"
    )


class AssociationResult(BaseModel):
    """候选事件关联求解结果。"""

    status: Literal["ok", "infeasible", "truncated"] = Field(
        ...,
        description="ok=搜索完成且有可行假设；infeasible=搜索完成但无解；truncated=达到搜索上限被截断",
    )
    hypotheses: list[AssociationHypothesis] = Field(
        default_factory=list, description="按（总代价, 时间残差）稳定排序的前 K 个可行假设"
    )
    total_feasible_found: int = Field(..., description="搜索中发现的可行假设总数（≤ max_hypotheses）")
    returned_k: int = Field(..., description="本次返回的假设数")
    eliminated_groups: list[GroupElimination] = Field(
        default_factory=list, description="搜索中发生过分支剪枝的关联组及样本矛盾链"
    )
    contradiction: Optional[Contradiction] = Field(
        None, description="代表性矛盾链（最深被剪枝分支；基础场景不可行时为基础矛盾）"
    )
    stats: AssociationSearchStats
    method: str


class ReconcileResult(BaseModel):
    scenario_name: str
    feasible: bool
    unified_timeline: list[EventWindow] = Field(default_factory=list)
    clock_models: list[ClockModel] = Field(default_factory=list)
    event_intervals: list[EventIntervalInput] = Field(default_factory=list)
    constraint_slack: list[ConstraintSlack] = Field(default_factory=list)
    contradiction: Optional[Contradiction] = None
    adjustment: Optional[AdjustmentReport] = None
    association: Optional[AssociationResult] = Field(
        None, description="声明了关联组时的候选关联求解结果；未声明为 null"
    )
    warnings: list[str] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    scenario_id: str
    version: int
    name: str
    created_at: str
    parent_version: Optional[int] = None
    note: Optional[str] = None


class ScenarioVersion(ScenarioSummary):
    payload: Scenario


class PlanDifference(BaseModel):
    """两个方案（场景/版本）的差异比较。"""

    left_ref: str
    right_ref: str
    events_only_left: list[str]
    events_only_right: list[str]
    constraints_only_left: list[str]
    constraints_only_right: list[str]
    sources_only_left: list[str]
    sources_only_right: list[str]
    association_groups_only_left: list[str] = Field(
        default_factory=list, description="仅左方案声明的关联组 ID"
    )
    association_groups_only_right: list[str] = Field(
        default_factory=list, description="仅右方案声明的关联组 ID"
    )
    association_groups_changed: list[str] = Field(
        default_factory=list, description="两侧都存在但规则定义不同的关联组 ID"
    )
    hypotheses_left: Optional[int] = Field(
        None, description="左方案关联求解返回的假设数（无关联组为 null）"
    )
    hypotheses_right: Optional[int] = Field(
        None, description="右方案关联求解返回的假设数（无关联组为 null）"
    )
    best_cost_left: Optional[float] = Field(
        None, description="左方案最优假设总代价（无可行假设为 null）"
    )
    best_cost_right: Optional[float] = Field(
        None, description="右方案最优假设总代价（无可行假设为 null）"
    )
    event_window_deltas_s: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="共有事件的最早/最晚/中点时刻差（右-左，秒）",
    )
    feasible_left: bool
    feasible_right: bool
    min_budget_left_s: Optional[float] = None
    min_budget_right_s: Optional[float] = None
    explanation: str
