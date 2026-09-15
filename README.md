# 多源取证时间线校核 API

把相机 EXIF、门禁记录、设备日志等**不同时钟来源**的事件，结合校时锚点、
线性漂移与“先于 / 同一事件 / 至少 / 至多间隔”等约束，求解统一时间线；
约束无法同时成立时给出**最小矛盾链**与关联原始记录，并提供限定修正幅度的
**时钟偏移建议**与两个方案的差异比较。来源可声明 **IANA 时区**，秋季回拨
重叠内的 naive 读数展开为全部合法 UTC 候选（fold=0/1），由事件约束选取
可行组合；春季跳时空洞内的读数判为不存在的本地时间。门禁刷卡、摄像头抓拍、
设备报警等只能按时间窗推断是否同一件事的记录，可声明**候选事件关联组**，
由求解器在容差与代价下搜索最优配对假设。所有持续时间以**秒**为单位，
统一时间轴为 **UTC Unix 秒**。

- Python 3.11+ / FastAPI / Pydantic v2 / zoneinfo + tzdata
- 场景版本保存在 SQLite，可复查、可派生
- 无外部数值库依赖：差分约束用 Bellman-Ford / 多源最短路实现

## 快速开始

```bash
# 1) 创建虚拟环境并安装
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 仅运行服务
pip install -r requirements-dev.txt      # 如需运行测试

# 2) 启动（场景库默认写到 ./data/forensic.db，可用 FORENSIC_DB 覆盖）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

也可用包方式安装：`pip install .`（开发用 `pip install -e '.[dev]'`）。

健康检查：`GET http://localhost:8000/health`，交互文档：`/docs`。

### Docker

```bash
docker build -t forensic-timeline-api .
docker run -p 8000:8000 -v "$PWD/data:/data" forensic-timeline-api
```

容器内数据库路径为 `/data/forensic.db`（环境变量 `FORENSIC_DB`）。

## 核心接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reconcile?budget_s=&top_k=&max_hypotheses=&max_search_nodes=&tz_max_search_nodes=` | 校核场景，返回统一时间线；带 `budget_s` 时附偏移建议；声明了关联组时附关联假设；声明了 `clock_segments` 时附分段时钟报告；声明了 `iana_timezone` 时附时区歧义求解报告 |
| POST | `/scenarios` | 创建场景（v1，入 SQLite） |
| GET | `/scenarios` | 列出场景（最新版本） |
| GET | `/scenarios/{id}/versions` | 列出某场景全部版本 |
| GET | `/scenarios/{id}?version=` | 读取指定版本（缺省最新） |
| POST | `/scenarios/{id}/versions` | 追加新版本 |
| POST | `/scenarios/{id}/reconcile?version=&budget_s=&top_k=` | 校核已存版本 |
| POST | `/compare?budget_s=` | 比较内联/已存的两个方案（含关联规则、分段方案与时区/fold 差异） |

### 请求示例

```json
{
  "name": "门廊事件",
  "default_utc_offset_s": 28800,
  "sources": [
    {"id": "door", "declared_utc_offset_s": 28800, "base_uncertainty_s": 1.0},
    {"id": "cam",  "declared_utc_offset_s": 28800, "base_uncertainty_s": 1.0,
     "drift_ppm": 50}
  ],
  "anchors": [
    {"id": "ntp-door", "source_id": "door",
     "clock_reading": "2026-09-14T08:00:00+08:00",
     "reference_time": "2026-09-14T00:00:05Z",
     "reference_uncertainty_s": 0.5}
  ],
  "events": [
    {"id": "badge", "source_id": "door",
     "reading": "2026-09-14T08:00:00+08:00", "reading_uncertainty_s": 0.5},
    {"id": "photo", "source_id": "cam",
     "reading": "2026-09-14T08:02:00+08:00", "reading_uncertainty_s": 1.0}
  ],
  "constraints": [
    {"id": "c-order", "type": "before", "a": "badge", "b": "photo"},
    {"id": "c-gap", "type": "max_interval", "a": "badge", "b": "photo", "max_s": 300}
  ]
}
```

## 时区处理约定

- 带显式偏移的时间戳（`+08:00`、`Z`）按其偏移换算，**与部署进程时区无关**。
- naive 时间戳只按来源的 `declared_utc_offset_s` 换算
  （无来源事件回退到场景的 `default_utc_offset_s`），同样**不受进程 `TZ` 影响**；
  响应中会给出“无时区”警告。
- 统一时间线输出带 `Z` 的 UTC ISO 8601 与 UTC Unix 秒两种表示。

## IANA 时区歧义求解（回拨重叠 / 跳时空洞）

秋季夏令时回拨会让同一设备的本地时间出现两次（如纽约 11 月第一个周日的
01:00–02:00），固定 UTC 偏移会把取证记录放错一小时；春季跳时则让一段
本地时间根本不存在。来源可声明 IANA 时区名，由 `zoneinfo` 与随应用安装的
`tzdata` 求解歧义（与进程 `TZ` 无关）：

```json
{"id": "cam", "iana_timezone": "America/New_York"}
```

- **候选展开**：声明时区后，naive 读数按 PEP 495 往返校验展开为全部合法
  UTC 候选——普通时刻 1 个、回拨重叠 2 个（fold=0/1，偏移相差一小时）、
  跳时空洞 0 个。显式偏移读数始终优先，不参与展开。
- **fold 组合搜索**：每个重叠事件的两个候选各生成一组一元区间分支，
  接入差分约束 + 分支限界（与关联求解同一套 Bellman-Ford 剪枝），由事件
  约束选出可行的 fold 组合；多个组合均可行时按 fold=0 优先取代表解。
  `tz_max_search_nodes`（默认 10000）限制展开节点数，达到上限且仍有节点
  未探索时报告稳定的 `truncated` / `node_limit` 状态。
- **逐事件结论**：`timezone.resolutions[]` 返回每个涉及时区事件的全部
  候选 UTC（含偏移与规则缩写，如 EDT/EST）、代表解采用的 fold/偏移与
  选择依据（另一候选被约束排除还是亦可行）。
- **矛盾**：本地时间不存在（`gap`）或全部 fold 组合均被约束排除时，
  `contradiction` 关联原始事件、来源与适用时区规则
  （`related_record_ids.timezones`），结果判不可行。
- **锚点**：naive 锚点读数按 fold=0 确定性解释（重叠/空洞时给出警告），
  保证时钟模型单值；夏令时两侧的锚点（EDT/EST）可正确拟合同一线性模型。
- **组合与限制**：时区声明可与候选关联、分段时钟（作用于不同来源）组合；
  同一来源暂不支持同时声明时区与分段规则（校验报错）。声明时区后该来源的
  `declared_utc_offset_s` 被忽略（给出警告）。

时区声明随场景版本一并保存；校核是确定性的，对已存版本重新校核即可复现
解析决策。`/compare` 通过 `timezones_only_*`、
`timezone_declarations_changed` 与 `fold_differences` 展示两方案的时区
声明与 fold 采用差异。未声明 `iana_timezone` 的旧请求行为完全不变
（`timezone` 为 null）。

## 候选事件关联求解

门禁刷卡、摄像头抓拍、设备报警往往只能按时间窗推断是否属于同一件事；
把不确定的配对写成确定的 `same_event` 约束容易把错误配对固化。在场景中
声明 `association_groups` 后，`/reconcile`（含已存版本校核）会额外返回
`association` 字段，给出按代价排序的可行配对假设：

```json
"association_groups": [
  {
    "id": "g-badge",
    "base_event_id": "badge",
    "mode": "exactly_one",
    "candidates": [
      {"event_id": "photo-0042", "tolerance_s": 5.0, "cost": 1.0},
      {"event_id": "photo-0047", "tolerance_s": 5.0, "cost": 2.5}
    ]
  }
]
```

- **基数**：`exactly_one` 必须选中一个候选；`at_most_one` 可选中一个或
  整组跳过（跳过项参与分支，代价为 0）。
- **不可跨组复用**：同一候选事件不能被两个关联组同时选中（冲突分支计入
  `stats.reuse_conflicts`，并记录“占用方 ↔ 被阻塞组”的复用冲突链）。
- **求解**：按各组可选项数升序（候选数优先）确定分支顺序，组内按
  （代价, 事件 ID) 排序；每选中一个候选即把 `|t_base − t_cand| ≤ 容差`
  的差分边加入图，复用 Bellman-Ford 差分约束校核剪枝，负环即提取矛盾链。
- **上限与排序**：`max_search_nodes`（默认 10000）限制展开的搜索节点数，
  达到上限且仍有节点未探索时才标记 `truncated`；`max_hypotheses`（默认 100）
  限制**保留**的可行假设数——搜索不因此提前停止，而是按
  （总代价, 时间残差, 发现序） 保留全局最优者，保证返回的前 `top_k`
  （默认 3）个假设恒为全局最优前 K；搜索穷尽时即使可行假设数恰好等于
  上限也返回 `ok`。时间残差 = 配对两事件在统一时间线可行窗口下的
  最小间距（0 表示窗口相交）。
- **假设内容**：选中的配对（含生成的 `assoc:<组ID>` 约束 ID，可在约束
  余量中追溯）、该配对下的统一时间线、全部约束余量、可追溯评分
  （总代价 + 时间残差 + 逐项组成）。
- **无解/截断**：`status` 为 `infeasible` 或 `truncated` 时，
  `eliminated_groups` 报告各组被淘汰的分支数（差分约束剪枝与复用冲突分列）
  及样本矛盾链，`contradiction` 给出最深淘汰处的代表性矛盾链
  （负环或复用冲突链），`stats` 给出节点数、剪枝数、复用冲突数与
  截断原因（`node_limit`）。

关联规则随场景版本一并保存，`/compare` 会报告两侧仅在一边声明的关联组、
规则定义发生变化的组，以及各自返回的假设数与最优假设总代价。
未声明 `association_groups` 的旧请求行为完全不变（`association` 为 null）。

## 分段时钟模型（重启 / 人工校时 / 断电跳变）

单条线性漂移会把一次**时钟跳变**摊进整段取证时间线。声明 `clock_segments`
后，校核对指定来源启用**分段时钟模型**；未声明分段配置的请求继续使用
原有单线性模型，行为完全不变（`segmentation` 为 null）。

```json
"clock_segments": [
  {
    "source_id": "dev",
    "segments": [
      {"id": "seg-boot",
       "start_clock_reading": "2026-09-14T00:00:00Z",
       "boundary_uncertainty_s": 60.0}
    ]
  }
]
```

- **段标识**：每个声明项是一个跳变**边界**，段 ID 即“边界后段”的标识
  （如 `seg-boot`）；首个边界之前还有一个由系统生成稳定 ID 的隐式
  **初始段** `seg-initial`（与声明 ID 冲突时追加序号）。因此只声明一个
  边界时，边界前后是 `seg-initial` / `seg-boot` 两个不同的物理段。
- **段级拟合与跳变量**：每段用各自锚点独立 OLS 拟合偏移与漂移；
  无锚点的段沿用前段模型（报告标注）。跳变量定义为“同一真实时刻下，
  后段钟面与前段外推钟面之差”，正值=钟被向前拨。
- **按钟面读数归段**：事件用其**钟面读数**（非反演真值）与边界比较。
  读数落在边界 ± `boundary_uncertainty_s` 内的事件保留跨段候选归属
  （如同时可能属于 `seg-initial` 与 `seg-boot`），用分支限界 +
  Bellman-Ford 差分约束剪枝搜索可行归段解——两个归属会产生不同的
  事件区间并**实际参与约束求解**，由约束决定采用哪个归属。
- **残差自动检测**：也可不显式给段，只给 `jump_threshold_s`
  （来源级）或场景级 `auto_jump_threshold_s`。系统对连续锚点做单线性
  拟合，当相邻锚点残差跳变 `|Δ残差| ≥ 阈值` 时生成跳变候选
  （返回在 `detected_jumps`），枚举其采纳子集与显式边界组合成候选
  分段方案，方案在 `max_segment_schemes`（默认 32）上限内按
  （可行性, 锚点残差 RMS, 段数, 跨段归属成本）稳定排序。
- **校核结果**：`segmentation.schemes[]` 返回每个方案的段级参数
  （偏移/漂移/锚点数/残差）、跳变量、逐事件归段依据（`assignments`，
  含名义段、全部可行归属、采用段、模糊标记）、排序名次与方案键 `key`。
  排名 1 的方案为代表方案，其统一时间线在每个事件上给出
  `segment_ids`（可行归属）与 `assigned_segment_id`（采用段）。
- **矛盾溯源**：若所有分段方案都无法满足约束，代表性矛盾链的
  `segment_ids` 与 `related_record_ids.clock_segments / anchors`
  会指出涉及的时钟段与锚点。
- **版本与比较**：分段规则随场景版本入库；`/compare` 报告分段规则的
  新增来源（`segment_sources_only_*`）、定义变化
  （`segment_rules_changed`）、边界的新增/删除/生效时刻移动
  （`segment_boundary_moves`，含秒级 `delta_s`）以及代表方案键
  `best_scheme_left/right` 与 `best_scheme_changed`。

## 计算模型

1. **时钟拟合**：每个来源的锚点 (真实时刻, 钟面读数) 用 OLS 拟合线性模型
   `clock = t_ref + a + β·(true − t_ref)`，报告偏移 `a`（秒）与漂移 `(β−1)·1e6`
   （ppm）；单锚点只能定偏移，无锚点时按先验（基线误差 + `drift_ppm` 外推）给区间。
2. **事件区间**：钟面读数经模型反演为真实时刻中心，合成半宽含读数粒度、
   锚点参考不确定度、拟合预测区间、基线误差与锚点覆盖范围外的漂移外推。
3. **差分约束系统（DCS）**：一元区间与 `before / same_event / min_interval /
   max_interval` 统一写成 `x_v − x_u ≤ w`；Bellman-Ford 判可行性、求每事件
   最早/最晚时刻与约束余量；不可行时沿前驱回溯提取负环作为**最小矛盾链**。
4. **偏移建议**：在事件间约束图上按来源分组做多源最短路，消去事件变量，得到
   仅含各来源整体平移量 `s_j` 的差分系统；叠加 `|s_j| ≤ budget_s` 做可行性判定，
   二分（40 次）求最小 L∞ 修正预算，并给出预算内建议值与可行区间。
5. **候选关联求解**：声明了关联组时，按候选数优先的分支顺序做分支限界，
   逐层复用差分约束校核剪枝，候选事件全局不可复用；搜索在节点上限内穷尽，
   按（总代价, 时间残差）稳定保留最优的 max_hypotheses 个假设并返回前 K。
6. **分段时钟模型**：声明了 `clock_segments` 时，跳变边界（显式声明或由
   连续锚点残差阈值自动检测）把每台设备的钟切成独立段；每段独立 OLS 拟合
   偏移/漂移并计算段间跳变量，事件按钟面读数归段，边界不确定区内的事件
   保留多个跨段归属并做分支限界 + 差分约束求解；候选分段方案按
   （可行性, 残差 RMS, 段数, 归属成本）排序，矛盾链标注涉及的段与锚点。
7. **IANA 时区歧义求解**：声明了 `iana_timezone` 的来源，naive 读数经
   `zoneinfo`/`tzdata` 按 PEP 495 往返校验展开为全部合法 UTC 候选
   （回拨重叠 2 个、跳时空洞 0 个）；歧义事件的 fold 组合接入差分约束 +
   分支限界，由事件约束选取可行组合（fold=0 优先为代表解），节点上限
   截断时报告稳定状态；逐事件返回候选 UTC、采用的偏移与选择依据。
8. 每项计算结果（`Quantity`）都带 **单位、来源/锚点/事件 ID 与 `derived_by`
   推导路径**。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`tests/test_timezone.py` 通过 `time.tzset()` 在 UTC / America/New_York /
Asia/Tokyo 三种进程时区下比对完整时间线，确保结论不随部署环境漂移；
`tests/test_timezone_ambiguity.py` 覆盖回拨重叠的 fold 选取、春季跳时空洞、
截断状态稳定性与 `/compare` 的时区差异展示。
