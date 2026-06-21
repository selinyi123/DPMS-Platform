# DPMS v0.3.6 事件 / 通知 Outbox 调研规划

日期：2026-06-21

## 本轮调研范围

本轮不重复 v0.3.5 的 worker lease、heartbeat、dead-letter 调研，转向 DPMS 当前剩余一致性风险：任务终态已写入 DB 后，event_store 或 notify_events 投递失败造成审计链、通知链与任务终态不一致。

## 外部参考

### Transactional Outbox

核心思想：业务状态和待发布消息写入同一个数据库事务，单独的 message relay 再从 outbox 表投递到消息系统。这样避免业务 DB 成功但消息发送丢失。

DPMS 设计映射：

- `task_runs` terminal update 与 `task_outbox_events` insert 应在同一事务；
- dispatcher 后台重试投递到 `events` 或 Redis `notify_events`；
- `dedup_key` 防止重复终态事件 / 通知。

### Idempotent Consumer

Outbox relay 可能在“消息已发出但 sent 状态尚未记录”时崩溃，因此消费者必须能处理重复消息。DPMS 本轮先对 outbox row 做 `dedup_key`，并在 events 写入使用 event id 幂等。

### Debezium Outbox Event Router

Debezium 的 outbox router 提供了更标准的 CDC 方案：应用写 outbox 表，Debezium 监听数据库变更并路由为事件。DPMS 当前不引入 Kafka / Debezium，先采用 polling publisher；后续如果系统拆分，可升级为 CDC outbox。

## 本轮已落地基础设施

### 新增迁移

`core/migrations/0003_task_event_outbox.sql`

新增表：

- `task_outbox_events`

字段：

- `event_kind`: `event_store` / `notify_stream`
- `aggregate`, `aggregate_id`, `event_type`
- `stream_key`
- `payload`
- `status`: `pending` / `sending` / `sent` / `failed`
- `dedup_key`
- `attempts`, `last_error`, `sent_at`

### 新增 Worker dispatcher

`worker/app/services/task_outbox.py`

能力：

- 写 event-store outbox；
- 写 notify-stream outbox；
- pending -> sending claim；
- 投递成功后标记 sent；
- 失败后按 attempts 重试；
- sending 超时回收 pending；
- event-store 投递使用 `ON DUPLICATE KEY UPDATE id = id` 保持幂等。

### Worker main 启动 dispatcher

`worker/app/main.py` 增加 `start_task_outbox_dispatcher(shutdown_event)` 后台任务，并纳入 graceful cancel。

## 未完全落地项

由于 `worker/app/task_runner.py` 包含真实执行路径，整文件更新被平台安全检查拦截。本轮没有强行绕过；终态路径从直接 `record_event` / `redis.xadd` 切换到 `enqueue_event_outbox` / `enqueue_notify_outbox` 将拆成下一轮最小补丁。

## 下一轮 v0.3.6.1 最小补丁目标

1. 只修改 `mark_task_finished()`：
   - 事务内写 `task_runs` / `lotteries` / `accounts` / `notify_logs`；
   - 同事务写 `task_outbox_events` 三条 outbox：TaskFinished/TaskFailed、AccountExecutionFinished、notify_events；
   - 删除事务后的直接 `record_event` 与 `redis.xadd`。
2. 保持 dry-run / shadow-run / real-run 逻辑完全不变。
3. 合并后再进入 v0.3.7 runtime schema boundary。

## 版本路线

- v0.3.6: outbox 基础设施；
- v0.3.6.1: terminal path 接入 outbox；
- v0.3.7: runtime schema / migrations 边界收敛；
- v0.3.8: persistent context TTL 与浏览器生命周期；
- v0.3.9: operator key / RBAC。