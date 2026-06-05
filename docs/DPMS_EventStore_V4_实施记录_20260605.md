# DPMS Event Store V4 实施记录

## 1. 本轮目标

本轮将 DPMS 从单纯执行态推进到具备“运行时记忆”的 V4 Event Runtime 最小闭环。

目标不是替换现有审计日志、任务记录和通知日志，而是新增统一事件历史，用于回答以下问题：

- Worker 何时启动或停止。
- 账号何时创建、导入凭据、校准、进入执行或恢复。
- 抽奖目标何时导入、派发、探针、完成或失败。
- 通知密钥何时保存、清除、通知何时排队、发送或失败。
- 风险引擎何时把账号转入冷却、需登录或恢复可用。

## 2. 新增后端路径

| 路径 | 说明 |
| --- | --- |
| `core/app/event_store/__init__.py` | Core 侧 Event Store 包入口 |
| `core/app/event_store/service.py` | Core 侧事件写入、载荷解析、行标准化 |
| `core/app/api/events.py` | 事件查询 API |
| `worker/app/event_store/__init__.py` | Worker 侧 Event Store 包入口 |
| `worker/app/event_store/service.py` | Worker 侧事件写入和兜底建表 |

## 3. 修改后端路径

| 路径 | 修改点 |
| --- | --- |
| `core/app/main.py` | 新增 `events` 表初始化；注册 `/api/events` 路由 |
| `init.sql` | 新增 `events` 表，保证全新数据库初始化一致 |
| `core/app/api/accounts.py` | 账号创建、二维码登录、Cookie 导入、状态变更、代理绑定、删除、校准排队写事件 |
| `core/app/api/lotteries.py` | 发现源、目标导入、活动创建、任务派发、探针、结果更新、适配器配置写事件 |
| `core/app/api/notify.py` | 通知密钥保存/清除、通知排队、发送成功/失败写事件 |
| `core/app/services/risk_engine.py` | 限速、账号冷却、需登录、健康复查摘要写事件 |
| `worker/app/main.py` | Worker 启停写事件；启动时兜底确认 `events` 表存在 |
| `worker/app/task_runner.py` | 任务开始、阶段推进、完成/失败、失败截图证据写事件 |
| `worker/app/account_calibrator.py` | 账号校准开始、成功、失败、需登录风险写事件 |
| `worker/app/login_broker.py` | QR 登录打开、等待扫码、成功、过期、失败、扫码创建账号写事件 |
| `worker/app/adapter_probe.py` | 适配器探针开始、成功、失败写事件 |

## 4. 新增前端路径

| 路径 | 说明 |
| --- | --- |
| `frontend/src/pages/EventTimeline.jsx` | 事件时间线页面，支持聚合、事件类型、关联 ID 筛选 |

## 5. 修改前端路径

| 路径 | 修改点 |
| --- | --- |
| `frontend/src/App.jsx` | 新增“事件时间线”导航入口 |
| `frontend/src/uiContext.jsx` | 新增中英文事件页文案和导航文案 |
| `frontend/src/index.css` | 新增事件筛选布局、事件载荷显示样式 |

## 6. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/events/` | 查询最新事件，支持 `aggregate`、`aggregate_id`、`event_type`、`correlation_id`、`limit` |
| `GET` | `/api/events/{event_id}` | 查询单个事件 |
| `GET` | `/api/events/accounts/{account_id}` | 查询账号事件 |
| `GET` | `/api/events/lotteries/{lottery_id}` | 查询抽奖事件 |
| `GET` | `/api/events/tasks/{task_id}` | 查询任务事件 |

## 7. 事件表

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | `CHAR(36)` | 是 | 无 | 事件 UUID |
| `aggregate` | `VARCHAR(64)` | 是 | 无 | 聚合类型 |
| `aggregate_id` | `VARCHAR(128)` | 是 | 无 | 聚合实体 ID |
| `event_type` | `VARCHAR(128)` | 是 | 无 | 事件类型 |
| `payload` | `JSON` | 否 | `{}` | 事件载荷 |
| `correlation_id` | `VARCHAR(128)` | 否 | `NULL` | 同一任务、导入、登录会话或通知日志的关联 ID |
| `causation_id` | `VARCHAR(128)` | 否 | `NULL` | 上游事件 ID |
| `actor_type` | `VARCHAR(32)` | 是 | `system` | 操作者类型 |
| `actor_id` | `VARCHAR(128)` | 否 | `NULL` | 操作者 ID |
| `source_service` | `VARCHAR(64)` | 是 | `core-api` / `worker` | 写入事件的服务 |
| `occurred_at` | `TIMESTAMP` | 是 | 当前时间 | 事件发生时间 |

## 8. 已验证结果

- `python -m compileall core\app worker\app` 通过。
- `npm run build` 通过。
- Docker 中 `core-api`、`worker`、`mysql`、`redis`、`nginx` 均为 healthy。
- `GET /api/events/?aggregate=worker&limit=5` 返回真实 `WorkerStarted` / `WorkerStopped` 事件。
- Playwright 已验证中文事件页、英文事件页、筛选控件、事件表渲染。
- Playwright 控制台检查无 warning/error。

## 9. 截图

| 路径 | 说明 |
| --- | --- |
| `output/playwright/dpms-event-timeline-zh.png` | 中文事件时间线 |
| `output/playwright/dpms-event-timeline-en.png` | 英文事件时间线 |

## 10. 后续建议

下一步进入 V4.5 Knowledge Runtime 前，建议先补两项：

1. 将事件时间线嵌入账号详情、任务详情和抽奖详情，而不仅是全局列表。
2. 增加事件聚合摘要，例如账号 24 小时事件数、任务失败链路、平台风险事件热力。
