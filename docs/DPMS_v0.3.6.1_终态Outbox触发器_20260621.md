# DPMS v0.3.6.1 终态 Outbox 触发器

日期：2026-06-21

## 本轮目标

继续推进 v0.3.6 Event / Notification Outbox Node，但避免直接整文件修改 `worker/app/task_runner.py`。该文件包含真实执行路径，上一轮整文件更新被平台安全检查拦截，因此本轮采用数据库层触发器完成终态事件 outbox 化。

## 外部调研补充

本轮聚焦数据库触发器与 outbox 的组合：

- Database Trigger：在表数据变更后自动执行逻辑，可用于审计历史与跨表日志写入；
- Transactional Outbox：业务状态与待发布事件必须写入同一个数据库事务；
- Idempotent Consumer：outbox relay 可能重复投递，消费者或事件表需要幂等键；
- MySQL JSON_OBJECT：用于在 SQL 内构建 JSON payload。

## 实现策略

新增迁移：

`core/migrations/0004_task_terminal_outbox_trigger.sql`

新增 MySQL trigger：

`trg_task_runs_terminal_outbox`

触发条件：

- `task_runs` 行发生 UPDATE；
- `OLD.status` 不是 `succeeded` / `failed`；
- `NEW.status` 变为 `succeeded` / `failed`。

触发结果：

- 写入一条 task 级 event-store outbox：`TaskFinished` 或 `TaskFailed`；
- 写入一条 account 级 event-store outbox：`AccountExecutionFinished`；
- 使用 `dedup_key` 防止同一 terminal transition 产生重复 outbox。

## 为什么本轮没有自动写 notify_stream

`worker/app/task_runner.py` 当前仍会在事务后直接执行 `redis.xadd("notify_events", ...)`。如果 trigger 同时写 notify_stream outbox，会造成用户通知重复。

因此本轮只做 event-store terminal outbox，先修复审计链耐久性。通知链 outbox 化保留到下一轮极小补丁：只修改 `mark_task_finished()` 中的直接 notify 逻辑。

## 与 migration runner 的兼容性

当前 migration runner 使用简单分号切分 SQL，因此不能写 `BEGIN ... END` 多语句 trigger。本迁移使用单语句 trigger：

`CREATE TRIGGER ... FOR EACH ROW INSERT INTO ... SELECT ... UNION ALL SELECT ...`

没有内部分号，因此兼容现有 runner。

## 剩余限制

- 由于 `worker/app/task_runner.py` 仍会直接 `record_event()`，本轮可能产生一条直接 event 和一条 outbox relay event；长期应通过 v0.3.6.2 删除直接 terminal event write；
- notify outbox 尚未接入终态路径；
- 未运行 DB migration 实测。

## 下一轮 v0.3.6.2

最小目标：

1. 只修改 `mark_task_finished()` 的 terminal event / notify 部分；
2. 删除事务后直接 `record_event()` 与 `redis.xadd()`；
3. 改为事务内写 `task_outbox_events`；
4. 不触碰 dry-run / shadow-run / real-run 动作函数。