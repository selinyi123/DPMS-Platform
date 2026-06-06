# DPMS 策略队列实施记录

## 背景

DPMS 目标工作流要求支持“评估价值/风险 → 自动选择账号 → 自动排程”。此前系统已有目标上传、账号筛选、dry-run、shadow-run 与 real-run 门禁，但缺少一个明确的策略队列来回答：

- 哪些目标应该优先处理。
- 每个目标建议使用 `dry_run`、`shadow_run` 还是 `real_run`。
- 为什么该目标被推荐或阻塞。

## 本次实现

- 新增 `GET /api/lotteries/strategy/queue`。
- 策略队列只读，不自动派发任务。
- 策略输入包括：
  - 目标 `value_score`。
  - 是否存在已校准安全账号。
  - 是否已有 active run。
  - 最近 dry-run 成功次数。
  - 最近 shadow-run 成功次数。
  - 失败任务数量。
  - 平台 24 小时风险事件数量。
  - 适配器配置状态。
  - 最新探针是否满足真实动作门禁。
  - real-run 全局开关。
  - 熔断器状态。
- 输出字段包括：
  - `rank`
  - `strategy_score`
  - `recommended_mode`
  - `reason_codes`
  - `blockers`
  - `safe_accounts`
  - `dry_success`
  - `shadow_success`
  - `failed_runs`
  - `recent_platform_risk`

## 推荐规则

- 已有 active run：`blocked`。
- 无安全账号：`blocked`。
- 熔断器开启：`blocked`。
- 没有 dry-run 成功记录：推荐 `dry_run`。
- 没有 shadow-run 成功记录：推荐 `shadow_run`。
- 适配器、探针、real-run 全局开关全部满足：推荐 `real_run`。
- 真实执行门禁未满足：继续推荐 `shadow_run`。

## 前端变化

- 任务编排页新增“策略队列 / Strategy queue”面板。
- 展示排名、目标、策略分、建议模式、安全账号数量、原因与阻塞项。
- 可按建议模式派发。
- 推荐为 `real_run` 时仍走原有后端确认与权限门禁。
- 推荐为 `blocked` 时禁用派发按钮。
- 文案支持中文与英文。

## 验证

- 后端语法检查通过：`py_compile core/app/api/lotteries.py`。
- 前端生产构建通过：`npm run build`。
- Docker 已重建并重启 `core-api`。
- 容器健康状态：`core-api`、`worker`、`nginx`、`mysql`、`redis` 均为 healthy。
- `GET /api/lotteries/strategy/queue` 已返回标准结构。
- 当前数据库没有 pending/claimed 目标，因此队列为空：`{"items": [], "count": 0}`。
