# 多源取证时间线校核 API

把相机 EXIF、门禁记录、设备日志等**不同时钟来源**的事件，结合校时锚点、
线性漂移与“先于 / 同一事件 / 至少 / 至多间隔”等约束，求解统一时间线；
约束无法同时成立时给出**最小矛盾链**与关联原始记录，并提供限定修正幅度的
**时钟偏移建议**与两个方案的差异比较。门禁刷卡、摄像头抓拍、设备报警等
只能按时间窗推断是否同一件事的记录，可声明**候选事件关联组**，由求解器
在容差与代价下搜索最优配对假设。所有持续时间以**秒**为单位，统一时间轴
为 **UTC Unix 秒**。

- Python 3.11+ / FastAPI / Pydantic v2
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
| POST | `/reconcile?budget_s=&top_k=&max_hypotheses=&max_search_nodes=` | 校核场景，返回统一时间线；带 `budget_s` 时附偏移建议；声明了关联组时附关联假设 |
| POST | `/scenarios` | 创建场景（v1，入 SQLite） |
| GET | `/scenarios` | 列出场景（最新版本） |
| GET | `/scenarios/{id}/versions` | 列出某场景全部版本 |
| GET | `/scenarios/{id}?version=` | 读取指定版本（缺省最新） |
| POST | `/scenarios/{id}/versions` | 追加新版本 |
| POST | `/scenarios/{id}/reconcile?version=&budget_s=&top_k=` | 校核已存版本 |
| POST | `/compare?budget_s=` | 比较内联/已存的两个方案（含关联规则差异） |

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
  `stats.reuse_conflicts`）。
- **求解**：按各组可选项数升序（候选数优先）确定分支顺序，组内按
  （代价, 事件 ID) 排序；每选中一个候选即把 `|t_base − t_cand| ≤ 容差`
  的差分边加入图，复用 Bellman-Ford 差分约束校核剪枝，负环即提取矛盾链。
- **上限与排序**：`max_hypotheses`（默认 100）限制收集的可行假设总数，
  `max_search_nodes`（默认 10000）限制展开的搜索节点数；在限值内按
  （总代价, 时间残差, 发现序） 稳定排序返回前 `top_k`（默认 3）个假设。
  时间残差 = 配对两事件在统一时间线可行窗口下的最小间距（0 表示窗口相交）。
- **假设内容**：选中的配对（含生成的 `assoc:<组ID>` 约束 ID，可在约束
  余量中追溯）、该配对下的统一时间线、全部约束余量、可追溯评分
  （总代价 + 时间残差 + 逐项组成）。
- **无解/截断**：`status` 为 `infeasible` 或 `truncated` 时，
  `eliminated_groups` 报告各组被剪枝的分支数与样本矛盾链，
  `contradiction` 给出最深剪枝处的代表性矛盾链，`stats` 给出节点数、
  剪枝数、复用冲突数与截断原因（`node_limit` / `hypothesis_limit`）。

关联规则随场景版本一并保存，`/compare` 会报告两侧仅在一边声明的关联组、
规则定义发生变化的组，以及各自返回的假设数与最优假设总代价。
未声明 `association_groups` 的旧请求行为完全不变（`association` 为 null）。

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
   逐层复用差分约束校核剪枝，候选事件全局不可复用；在假设数与搜索节点
   上限内按（总代价, 时间残差）稳定排序返回前 K 个可行假设。
6. 每项计算结果（`Quantity`）都带 **单位、来源/锚点/事件 ID 与 `derived_by`
   推导路径**。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`tests/test_timezone.py` 通过 `time.tzset()` 在 UTC / America/New_York /
Asia/Tokyo 三种进程时区下比对完整时间线，确保结论不随部署环境漂移。
