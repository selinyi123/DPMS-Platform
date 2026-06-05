# DPMS Shadow-Run 实施记录

## 背景

DPMS 目标工作流要求在 `probe` 与 `real-run` 之间提供 `shadow-run`。该模式用于在真实账号上下文中验证目标页面、登录态、风险信号和可见动作入口，但不执行关注、点赞、评论、转发等会改变平台状态的真实动作。

## 本次实现

- 新增任务模式枚举：`dry_run`、`shadow_run`、`real_run`。
- 保留旧字段 `dry_run` 作为兼容层，新增 `task_mode` 用于准确表达任务模式。
- 派发接口 `POST /api/lotteries/{lottery_id}/dispatch` 支持 `mode` 字段。
- 前端任务编排页提供 Dry / Shadow / Real 三段式派发控制。
- 账号运行态和执行记录显示 `task_mode`，避免 shadow-run 被误标为 dry-run。
- Worker 新增 `execute_shadow_run`：
  - 使用账号浏览器上下文和 Cookie 登录态。
  - 打开目标 URL。
  - 执行页面风险检测。
  - 检查各阶段选择器是否可见。
  - 记录 `TaskShadowRunObserved` 事件。
  - 保存 `shadow_run_screenshot` 证据。
  - 不执行任何真实动作。
- Shadow-run 成功后活动状态回到 `pending`，不会标记为 `participated`。

## 安全边界

- `shadow_run` 不需要打开全局 real-run 开关。
- `shadow_run` 会检查平台熔断器；熔断器阻断时拒绝派发。
- `real_run` 仍要求管理员权限、确认头、请求体确认、全局开关、熔断器和真实动作适配器。
- 证据截图只允许从 `/profiles/task-failures` 与 `/profiles/shadow-runs` 安全目录读取。

## 验证

- 前端生产构建通过：`npm run build`。
- Python 语法检查通过：`py_compile` 本次改动文件。
- Docker 已重建并重启：`docker compose up -d --build`。
- 容器健康状态：`core-api`、`worker`、`nginx`、`mysql`、`redis` 均为 healthy。
- `/api/lotteries/tasks/runs` 已返回 `task_mode` 字段。
- FastAPI OpenAPI schema 已暴露 `TaskModeEnum = dry_run | shadow_run | real_run`。
