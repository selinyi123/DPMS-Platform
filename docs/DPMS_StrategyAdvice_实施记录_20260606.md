# DPMS 复盘与策略建议实施记录

## 背景

DPMS 目标工作流要求从执行结果、风险事件、目标池和通知状态中形成复盘与策略建议，而不是只展示运行日志。本次实现为 V5 Strategy Runtime 的轻量入口。

## 本次实现

- 在 `GET /api/metrics/readiness` 中新增 `strategy_advice` 字段。
- 策略复盘窗口固定为最近 7 天。
- 复盘数据包括：
  - `task_runs` 按 `task_mode` 与 `status` 聚合。
  - 最近风险账号排行。
  - 高价值待派发目标数量。
  - 超过 30 分钟仍处于 queued/running 的活跃任务数量。
- 输出策略建议列表，每条包含：
  - `code`
  - `priority`
  - `target`
  - `title`
  - `detail`
  - `evidence`
- 前端总览页新增“复盘与策略建议 / Review & strategy advice”面板。
- 策略建议通过稳定 `code` 在前端做中英文双语本地化。

## 当前建议规则

- `configure_notifications`：没有外部通知通道时阻止自主运行。
- `run_shadow_before_real`：有待派发目标但最近 7 天没有成功 shadow-run。
- `promote_dry_to_shadow`：dry-run 成功后建议推进 shadow-run。
- `complete_real_gate`：已有 shadow-run 证据但 real-run 门禁未完成。
- `review_failed_runs`：近期存在失败任务时要求复盘证据。
- `cooldown_risky_accounts`：近期或 7 天窗口内存在风险账号。
- `prioritize_high_value_targets`：存在高价值待派发目标且有安全账号。
- `recover_stale_tasks`：存在超时 queued/running 任务。
- `continue_controlled_real_run`：真实执行成功且无紧急阻塞时保持受控节奏。

## 验证

- 后端语法检查通过：`py_compile core/app/api/metrics.py`。
- 前端生产构建通过：`npm run build`。
- Docker 已重建并重启 `core-api`。
- 容器健康状态：`core-api`、`worker`、`nginx`、`mysql`、`redis` 均为 healthy。
- `GET /api/metrics/readiness` 已返回 `strategy_advice`。
- 当前数据下返回建议：
  - `promote_dry_to_shadow`
  - `cooldown_risky_accounts`
