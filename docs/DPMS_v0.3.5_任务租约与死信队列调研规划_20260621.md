# DPMS v0.3.5 任务租约与死信队列调研规划

日期：2026-06-21

## 本轮外部调研范围

本轮没有重复上一轮 Browser Use / Skyvern / Stagehand / Redis XAUTOCLAIM / Alembic 调研，改为聚焦 DPMS 当前阻塞 v0.3.5 的运行时一致性问题：任务所有权、worker lease、heartbeat、dead-letter、幂等消费、长任务恢复。

## 外部参考结论

### Temporal：长任务必须有 heartbeat / timeout 模型

Temporal 文档将长任务失败检测拆为 Start-To-Close、Schedule-To-Close 与 Activity Heartbeat，并强调长任务应使用 heartbeat timeout 来判断 worker 是否还活着，而不是只看队列消息是否 pending。

DPMS 采用的落地方式：

- task_runs.worker_id 记录任务所有者；
- task_runs.lease_expires_at 记录任务租约；
- Worker 在执行阶段刷新 lease；
- Recovery 只在 owning worker heartbeat stale 或 lease 过期时恢复任务。

### BullMQ：active job 需要 worker 定期续锁，否则视为 stalled

BullMQ 的 stalled job 模型说明：worker 处理 job 时持有 lock，并需要定期通知队列自己仍在处理；如果不能续锁，job 会回到 waiting 或进入 failed。

DPMS 采用的落地方式：

- task_runs.stream_message_id 绑定 Redis stream message；
- task_runs.lease_expires_at 模拟 job lock TTL；
- recovery 只回收 lease 失效的任务；
- recovery_count 超限后标记 failed，避免无限恢复循环。

### Celery：ack 时机依赖任务幂等性，坏消息不能阻塞 worker

Celery 文档强调消息在 ack 前仍可能重投，任务最好是幂等的；如果任务参数错误、worker 死亡或长时间阻塞，需要明确超时、ack 与重投策略。

DPMS 采用的落地方式：

- Worker 在处理前做 task message schema validation；
- 坏消息写入 failed_task_messages 表与 Redis dead-letter stream；
- 原消息被 xack，避免同一坏消息永久拖垮 task_loop；
- terminal task 被 ack 但不再执行。

### Transactional Outbox / Idempotent Consumer

Transactional Outbox 与 Idempotent Consumer 模式说明：分布式消息系统通常需要 at-least-once 投递和幂等消费配合，否则会出现 DB 状态与消息状态不一致。

DPMS 本轮只完成了消费者侧幂等和 dead-letter；后续应将 event_store / notify_events 全部收敛为 outbox dispatcher。

## 本轮版本目标

版本节点：v0.3.5 Recovery Lease + Dead-letter Node

目标：让 DPMS 从“Redis pending recovery”升级到“worker-owned lease recovery”。

## 已落地设计

### 数据结构

新增迁移：`core/migrations/0002_worker_lease_deadletter.sql`

新增字段：

- `task_runs.worker_id`
- `task_runs.stream_message_id`
- `task_runs.lease_expires_at`

新增表：

- `failed_task_messages`

### Worker 行为

- 使用稳定 `WORKER_ID` 作为 Redis consumer name；
- `mark_task_started()` 写入 worker_id、stream_message_id、lease_expires_at；
- dry-run / shadow-run / real-run 阶段刷新 lease；
- Redis 消息进入执行前先做 schema validation；
- 坏消息进入 dead-letter 并 xack；
- terminal task 不再重开。

### Recovery 行为

- recovery 不再只看全局 worker heartbeat；
- recovery 根据 task_runs.worker_id 找 owning worker；
- owning worker heartbeat fresh 且 lease 未过期时跳过恢复；
- terminal task 直接 xack；
- recovery_count 超限后标记 task failed。

## 后续版本规划

### v0.3.6 Event / Notification Outbox Node

- 统一 event_store 与 notify_events 的 outbox 投递；
- `mark_task_finished()` 不再直接依赖外部 Redis / event call 成功；
- 增加 event_outbox / notify_outbox dispatcher 监控。

### v0.3.7 Runtime Schema Boundary Node

- 把 main.py 中的大量 ensure_runtime_schema 逐步迁出；
- 所有新增结构进入 migrations；
- production 禁止 runtime ALTER。

### v0.3.8 Browser Context Lifecycle Node

- persistent context TTL；
- idle eviction；
- per-account memory attribution；
- browser leak metrics。

### v0.3.9 Operator Key / RBAC Node

- operator_api_keys；
- key hash；
- role-based action gates；
- x-admin-token 仅作为 bootstrap owner token。

## 当前验收标准

v0.3.5 完成后必须满足：

1. worker-A 死亡，worker-B 存活，不会错误跳过 worker-A 的任务恢复；
2. 坏 Redis message 不会让 task_loop 无限报错；
3. terminal task 不会被重新执行；
4. persistent Chromium 进程会纳入内存统计；
5. real-run 能力没有扩大。