# 洁净厂房通风管段最小费用隔断服务

洁净厂房发生局部污染时，设施工程师需要关闭一组**定向通风管段**，使所有
**污染源区域**都无法到达任何**保护区域**，并把停产代价（关闭费用之和）
降到最低。本服务在**所有最低费用割**中，返回**源侧区域集合按包含关系
最小**的唯一方案，保证：无论管段提交顺序如何、服务是否重启，工程师读到
的都是同一份可直接执行、确实隔断全部污染路径的最低费用清单。

纯后端服务：FastAPI 提供普通 JSON 接口，PostgreSQL 持久化方案与采用快照，
最大流/最小割算法为服务自行实现（64 位整数容量的 Dinic），不依赖任何
图算法库，也不返回占位结果。

## 快速开始

```bash
docker compose up --build
```

- API 监听 `http://localhost:8000`（`GET /health` 健康检查）。
- PostgreSQL 数据保存在命名数据卷 `pgdata` 中，服务重启后方案与已采用
  快照仍然保留。
- 数据库连接可通过环境变量 `DATABASE_URL` 覆盖（compose 中已指向 `db`
  服务）。

## 算法说明

建模：超级源 → 每个污染源（容量 INF）、每个保护区 → 超级汇（容量 INF）、
每条管段为一条有向边（容量 = 关闭费用）。INF = 全部管段费用之和 + 1，
严格大于任何真实割，因此最小割绝不会切断 INF 边。

- 用 Dinic 算法求最大流（容量为 64 位整数，总费用上限
  2000 × 10⁹ = 2×10¹² ≪ 2⁶³）。
- 最大流完成后，残量网络中从超级源可达的节点集合，恰好是**所有最小割
  源侧的交集**——即按包含关系最小的唯一源侧；对该集合取"源侧 → 汇侧"
  的管段，得到唯一的最小切断清单。
- 结果与管段/区域的提交顺序无关：`source_zones`、`cut_segments` 均按
  字典序升序返回，`total_cost` 为精确整数。

## 数据约束

| 项 | 约束 |
| --- | --- |
| 区域 ID / 管段 ID | 匹配 `[A-Za-z0-9_-]{1,32}`，各自唯一 |
| 区域数 | ≤ 300（`zones` 缺省时从管段端点与污染源/保护区推导） |
| 管段数 | ≤ 2000 |
| 管段 | `from`、`to` 必须引用现有区域，方向不可反转；`cost` 为 0 至 10⁹ 的整数 |
| 污染源 / 保护区 | 均非空、互不相交、只能引用现有区域 |

## 接口说明

所有错误响应均为统一信封，错误代码稳定：

```json
{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": [{"code": "EMPTY_SOURCES", "field": "sources", "message": "..."}]}}
```

| 接口 | 说明 |
| --- | --- |
| `PUT /plans/{plan_id}` | 保存（新建或整版替换）方案；非法负载返回 422 且**不改写**当前方案；并发保存冲突返回 409 且**不改写**当前方案 |
| `GET /plans/{plan_id}` | 查询当前方案 |
| `POST /plans/{plan_id}/computations` | 对当前方案计算最小费用隔断，返回计算记录 |
| `GET /plans/{plan_id}/computations/{computation_id}` | 查询计算记录 |
| `POST /plans/{plan_id}/adopt` | 采用一次**成功**计算并保存该计算时刻冻结的完整快照；同一计算**至多采用一次**，即使后来被其他计算替换也不可再次采用；并发采用冲突返回 409 |
| `GET /plans/{plan_id}/adoption` | 查询当前已采用结果（完整快照） |

### 调用示例

保存方案：

```bash
curl -X PUT http://localhost:8000/plans/demo \
  -H 'content-type: application/json' \
  -d '{
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
      {"id": "p1", "from": "SRC1", "to": "MID",   "cost": 4},
      {"id": "p2", "from": "SRC2", "to": "MID",   "cost": 6},
      {"id": "p3", "from": "MID",  "to": "SAFE1", "cost": 5},
      {"id": "p4", "from": "MID",  "to": "SAFE2", "cost": 7}
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"]
  }'
```

```json
{"plan_id": "demo", "revision": 1, "plan": {"zones": ["..."], "segments": ["..."], "sources": ["SRC1", "SRC2"], "protections": ["SAFE1", "SAFE2"]}}
```

计算最小费用隔断：

```bash
curl -X POST http://localhost:8000/plans/demo/computations
```

```json
{
  "computation_id": "6dc73aa10a0d4ee897af3b6abc5d93d7",
  "plan_id": "demo",
  "plan_revision": 1,
  "status": "SUCCESS",
  "result": {"source_zones": ["SRC1", "SRC2"], "cut_segments": ["p1", "p2"], "total_cost": 10},
  "error": null
}
```

采用该计算结果（保存计算时刻冻结的完整快照；同一 `computation_id`
再次采用——即便其采用结果已被其他计算替换——返回
`409 COMPUTATION_ALREADY_ADOPTED`）：

```bash
curl -X POST http://localhost:8000/plans/demo/adopt \
  -H 'content-type: application/json' \
  -d '{"computation_id": "6dc73aa10a0d4ee897af3b6abc5d93d7"}'
```

查询已采用结果：

```bash
curl http://localhost:8000/plans/demo/adoption
```

```json
{
  "plan_id": "demo",
  "plan_revision": 1,
  "computation_id": "6dc73aa10a0d4ee897af3b6abc5d93d7",
  "adopted_at": "2026-09-22T17:03:34.619220+00:00",
  "plan": {"zones": ["..."], "segments": ["..."], "sources": ["SRC1", "SRC2"], "protections": ["SAFE1", "SAFE2"]},
  "result": {"source_zones": ["SRC1", "SRC2"], "cut_segments": ["p1", "p2"], "total_cost": 10}
}
```

### 错误代码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `INVALID_JSON` | 请求体不是合法 JSON |
| 404 | `PLAN_NOT_FOUND` / `COMPUTATION_NOT_FOUND` / `ADOPTION_NOT_FOUND` / `NOT_FOUND` | 资源不存在 |
| 409 | `PLAN_SAVE_CONFLICT` / `COMPUTATION_ALREADY_ADOPTED` / `COMPUTATION_NOT_ADOPTABLE` / `ADOPTION_CONFLICT` | 方案在保存期间被并发创建/修改，本次写入未生效，请重新读取后再保存 / 计算已被采用过（含已被替换下来的历史采用）/ 计算未成功 / 并发采用时当前生效结果已被他人改变，请重新查询后再采用 |
| 422 | `VALIDATION_ERROR` | 负载非法，`details[].code` 给出细分原因（如 `INVALID_ZONE_ID`、`DUPLICATE_SEGMENT_ID`、`UNKNOWN_ZONE`、`INVALID_COST`、`EMPTY_SOURCES`、`SOURCE_PROTECTION_OVERLAP`、`TOO_MANY_ZONES`、`TOO_MANY_SEGMENTS` 等） |
| 500 | `INTERNAL_ERROR` / `COMPUTATION_FAILED` | 服务内部错误 |

非法整版、并发保存冲突、计算失败、采用不存在或已采用过的结果，都
**不会**改写当前方案或已采用结果。

### 并发保存与修订号唯一性

方案保存采用乐观并发控制：整版替换是"仅当行仍为本事务读到的修订号
才生效"的条件更新，并发首次创建由主键唯一约束兜底。两名调度员基于
同一修订并发提交时，恰有一个请求被接受（修订号 +1），落败请求回滚并
返回 `409 PLAN_SAVE_CONFLICT`——不会暴露数据库异常（500），也不会
改写现状，修订号也不会被失败的写入落空消耗。因此每个修订号在数据库
历史上只对应一份内容：计算记录冻结的 (`plan_revision`, `plan_payload`)
与采用快照始终能追溯到唯一确定的版本。保存响应由本事务实际写入的值
直接构造，提交后不再回读，被接受的内容、修订号与响应严格一一对应。

### 快照一致性与并发采用

计算记录在创建时即**冻结**三件同属一个版本的内容：来源方案负载、来源
修订号 (`plan_revision`) 与最小割结果。采用时快照只取自计算记录，绝不
读取"当前方案"，因此先计算、再修订方案、再采用旧计算时，快照中的方案、
修订号、切断管段与总费用仍然彼此匹配，清单也确实隔断的是该版快照中的
全部污染路径。

每个成功计算至多产生一条不可变的采用历史（`adoption_events`，
`computation_id` 永久唯一）：它先被采用、再被另一个计算替换后，仍不能
再次被采用。同一方案上的并发采用由方案行锁串行化：首个采用成功，与其
并发的首次采用返回 `409 ADOPTION_CONFLICT`（不会出现 500，也不会把行
竞争误报为"计算已采用"）；成功响应与随后 `GET .../adoption` 查到的最终
记录逐项一致。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_exhaustive.py`：对不超过 8 个区域的**全部合法划分**
  （污染源、保护区非空且互不相交，共 3ⁿ − 2ⁿ⁺¹ + 1 种）与暴力枚举所有
  源侧集合对拍，并验证删除切断管段后不存在任何污染源到保护区的路径。
- `tests/test_flow.py`：并列割取源侧最小、多源多汇、零费用割、超过
  32 位（直至 2×10¹²）的总费用、自环与提交顺序无关性。
- `tests/test_api.py`：保存/计算/采用/查询全流程、稳定错误码、非法
  操作不改写既有数据、结果确定性。
- `tests/test_snapshot.py`：计算后修订再采用时快照的方案/修订/清单/费用
  逐项冻结且能隔断快照内污染路径；采用被替换后旧计算不可重用；历史采用
  次数核对；冲突不改写当前方案与生效快照。
- `tests/test_concurrency_pg.py`：**仅在真实 PostgreSQL 下运行**
  （SQLite 自动跳过），确定性地制造两个不同成功计算的并发首次采用，
  核对一胜（200）一负（409 `ADOPTION_CONFLICT`）、无 500/无误报、
  成功响应与最终记录一致、历史采用次数为 1，另含多轮并行对拍。
- `tests/test_plan_concurrency_pg.py`：**仅在真实 PostgreSQL 下运行**
  （SQLite 自动跳过），在提交点确定性挂起领先事务，核对并发首次创建
  与并发覆盖均为一胜（200）一负（409 `PLAN_SAVE_CONFLICT`）、无 500、
  失败写入不改变现有方案、响应与本次提交一一对应、随后计算与采用
  追溯到唯一确定的版本，另含多轮并行对拍。

测试默认使用 SQLite 内存库；设置 `TEST_DATABASE_URL` 可指向 PostgreSQL
进行对拍。

## 目录结构

```
app/
  main.py        FastAPI 路由与全局异常处理
  flow.py        自实现 64 位整数容量 Dinic 最大流 / 最小割
  validation.py  方案负载域校验（稳定错误码）
  services.py    保存、计算、采用、查询业务逻辑
  models.py      plans / computations / adoptions / adoption_events 表
  db.py          引擎、会话、建表（带重试）
  errors.py      统一错误信封
tests/           穷举对拍 + 单元 + 接口 + 快照 + PostgreSQL 并发验收
```
