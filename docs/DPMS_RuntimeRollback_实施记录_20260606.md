# DPMS 运行态回滚实施记录

## 背景

DPMS 工作流要求支持 `灰度 -> 回滚`。此前系统具备 real-run 全局开关和熔断器，但缺少统一的运行态紧急回滚入口，也缺少 Worker 对 reload 信号的实际监听。

## 本次实现

- 新增 `POST /api/metrics/runtime/rollback`。
- 回滚请求模型：`RuntimeRollbackRequest`。
- 回滚动作：
  - 关闭 `real_run_enabled`。
  - 打开全局熔断器：`circuit_breakers.scope = global`。
  - 撤销仍处于 `queued` 的 `real_run` 任务。
  - 将对应抽奖目标释放回 `pending`。
  - 向 `notify_events` 写入回滚通知事件。
  - 发布 `worker:reload = runtime_rollback`。
  - 写入审计日志与 Event Store：`RuntimeRollbackApplied`。
- `GET /api/metrics/runtime/settings` 新增 `global_circuit_breaker` 状态。
- owner 确认重新启用 real-run 时，会关闭全局熔断器，避免回滚后只能手动改数据库恢复。
- Worker 新增 `reload_signal_loop`，监听 `worker:reload` 并触发优雅退出；Docker 会根据 `restart: unless-stopped` 自动拉起新 Worker。
- 运维页新增“运行态紧急回滚 / Runtime emergency rollback”控制区。
- 回滚 UI 支持中英文、原因输入、二次确认。

## 安全边界

- 回滚接口要求 owner 权限。
- 回滚接口要求 `x-confirm-action: true`。
- 回滚不会伪造运行中任务的成功结果。
- 已排队的 real-run 会被标记失败并释放目标；正在运行中的 real-run 只统计并通过 Worker reload 走进程级恢复。
- 回滚会打开全局熔断器，后续 real-run 派发会被门禁阻断，直到 owner 显式重新启用 real-run。

## 验证

- 后端语法检查通过：`py_compile core/app/api/metrics.py core/app/models/schemas.py worker/app/main.py`。
- 前端生产构建通过：`npm run build`。
- Docker 已重建并重启 `core-api`、`worker`、`nginx`。
- 容器健康状态：`core-api`、`worker`、`nginx`、`mysql`、`redis` 均为 healthy。
- `GET /api/metrics/runtime/settings` 已返回 `global_circuit_breaker`。
- OpenAPI 已暴露 `/api/metrics/runtime/rollback` 与 `RuntimeRollbackRequest`。
- 新前端产物 `index-D55fmL4C.js` 可从 Nginx 访问。

## 未执行项

- 本次未实际触发回滚，以避免改变当前运行环境状态。
