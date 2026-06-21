# DPMS v0.3.6.2 终态通知 Outbox 闭环

日期：2026-06-21

## 本轮目标

在不修改 Playwright 执行动作、不扩大 real-run 能力的前提下，将任务终态的事件与通知统一收敛到 `task_outbox_events`。

## 本轮调研补充

本轮不重复 v0.3.5 worker lease / dead-letter，也不重复 v0.3.6 polling outbox 基础设施，聚焦三个工程问题：

1. outbox relay 是 at-least-once，因此必须有 dedup key；
2. trigger 可用于同事务派生审计 / outbox 记录；
3. 在迁移旧路径时，需要短期抑制 direct write，避免 direct path 和 outbox relay 双写。

## 已落地内容

### 1. 替换终态 trigger

新增迁移：

`core/migrations/0005_terminal_notify_outbox_trigger.sql`

逻辑：

- `DROP TRIGGER IF EXISTS trg_task_runs_terminal_outbox`；
- 重建 `trg_task_runs_terminal_outbox`；
- 当 `task_runs.status` 从非终态进入 `succeeded` / `failed` 时，写入三类 outbox：
  - task event：`TaskFinished` / `TaskFailed`；
  - account event：`AccountExecutionFinished`；
  - notify stream：`NotifyEventRequested` -> `notify_events`。

### 2. 抑制旧 terminal event direct write

修改：

`worker/app/event_store/service.py`

当 worker 直接调用：

- `TaskFinished`
- `TaskFailed`
- `AccountExecutionFinished`

时不再直接写 `events`，而是记录 `terminal_event_direct_write_skipped`。真正事件由 trigger -> task_outbox_events -> dispatcher 写入。

### 3. 抑制旧 terminal notify direct push

修改：

`worker/app/db.py`

当旧代码直接执行：

`redis.xadd("notify_events", {... task_id, status=succeeded/failed ...})`

时返回 `covered-by-task-outbox`，不再直接推送，避免与 outbox relay 重复。

### 4. 允许 outbox dispatcher 投递 terminal notify

修改：

`worker/app/services/task_outbox.py`

`_deliver_notify()` 使用 `_from_task_outbox=True` 调用 RedisClient，明确表明该写入来自 outbox relay，应允许投递。

## 当前终态链路

```text
task_runs.status -> succeeded / failed
↓
MySQL trigger
↓
task_outbox_events
↓
worker task_outbox dispatcher
↓
events / notify_events
```

旧 direct path：

```text
record_event(TaskFinished/TaskFailed/AccountExecutionFinished) -> skipped
redis.xadd(notify_events terminal task notice) -> covered-by-task-outbox
```

## 保留限制

- DB migration 尚未在容器中实跑；
- 当前仍依赖 MySQL trigger，后续如果 migration runner 支持 raw SQL script，可改成更可读的 multi-statement trigger；
- 非 terminal 事件仍保留 direct record_event；
- 非 terminal notify 仍保留 direct notify_events。

## 下一轮 v0.3.7

建议进入 Runtime Schema Boundary：

1. 将 `main.py` 中的 runtime schema ensure 逐步迁移到 migrations；
2. production 禁止运行时 ALTER；
3. 建立 migration smoke test；
4. 清理本轮临时分支。