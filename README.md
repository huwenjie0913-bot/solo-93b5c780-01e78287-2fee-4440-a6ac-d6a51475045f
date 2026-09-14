# 多源取证时间线校核 API

把相机 EXIF、门禁记录、设备日志等**不同时钟来源**的事件，结合校时锚点、
线性漂移与“先于 / 同一事件 / 至少 / 至多间隔”等约束，求解统一时间线；
约束无法同时成立时给出**最小矛盾链**与关联原始记录，并提供限定修正幅度的
**时钟偏移建议**与两个方案的差异比较。所有持续时间以**秒**为单位，统一时间轴
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
| POST | `/reconcile?budget_s=` | 校核场景，返回统一时间线；带 `budget_s` 时附偏移建议 |
| POST | `/scenarios` | 创建场景（v1，入 SQLite） |
| GET | `/scenarios` | 列出场景（最新版本） |
| GET | `/scenarios/{id}/versions` | 列出某场景全部版本 |
| GET | `/scenarios/{id}?version=` | 读取指定版本（缺省最新） |
| POST | `/scenarios/{id}/versions` | 追加新版本 |
| POST | `/scenarios/{id}/reconcile?version=&budget_s=` | 校核已存版本 |
| POST | `/compare?budget_s=` | 比较内联/已存的两个方案 |

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
5. 每项计算结果（`Quantity`）都带 **单位、来源/锚点/事件 ID 与 `derived_by`
   推导路径**。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`tests/test_timezone.py` 通过 `time.tzset()` 在 UTC / America/New_York /
Asia/Tokyo 三种进程时区下比对完整时间线，确保结论不随部署环境漂移。
