# DPMS Bilibili 规则计划安全修复实施记录

## 背景与问题

2026-06-24 的一次 Bilibili 真实账号 real-run 中，目标 L72 的规则文本要求 `关注 / 点赞 / 评论 / 转发`，但实际执行结果只完成了 `关注 / 转发`。

复盘发现根因不是 Bilibili API 执行器缺少点赞或评论能力，而是派发任务时使用了数据库中已保存的旧 `action_plan`。该旧计划只有：

```json
["followed", "reposted"]
```

规则识别接口能够识别出完整动作：

```json
["followed", "liked", "commented", "reposted"]
```

但前端只把识别结果放入草稿，未强提示“尚未保存”；real-run 门禁也只检查 `action_plan` 是否存在、是否已审核，没有比较规则文本与保存计划是否一致。因此旧计划可以进入真实执行。

随后尝试用 `liked / commented` 作为补做计划再次派发，但 Worker 将之前的 `shadow_run` 与 `real_run` 都计入账号动作窗口，补做任务被 `action_window` 安全限制拦截，账号 14 进入 `cooling`。

## 安全边界

- 不绕过验证码。
- 不规避平台风控。
- 不在账号处于冷却或最近风险事件存在时继续真实执行。
- 本次只修复工程链路与本地数据状态，不再次派发真实动作。

## 实现内容

### Worker 真实动作窗口修复

文件：`worker/app/task_runner.py`

- 只有 `task_mode == "real_run"` 时才调用 `ensure_account_can_run()`。
- `dry_run` 与 `shadow_run` 不再消耗真实动作窗口。
- 只有 `real_run` 才递增 `accounts.daily_task_count` 并写入 `AccountExecutionStarted`。
- `dry_run` / `shadow_run` 仅刷新 `last_active_at`，避免验证流程把账号提前推入冷却。

### real-run 门禁规则计划新鲜度检查

文件：`core/app/services/real_run_readiness.py`

- 新增 `action_plan_missing_rule_actions()`。
- 若当前 `rule_text` 能识别出动作，而已保存 `action_plan.required_actions` 缺少其中任何动作，则加入 blocker：

```text
lottery_action_plan_stale
```

- blocker 的下一步动作归类为 `review_rule`。

### 前端规则计划可见性修复

文件：

- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/styles/workflows.css`
- `frontend/src/i18n/dictionaries.js`

新增展示：

- 已保存执行计划。
- 当前草稿。
- 规则识别建议。
- 草稿未保存警告。
- 建议动作缺失于已保存计划的警告。

保存按钮改为“保存当前计划”，强调真实执行使用的是已保存计划，而不是临时勾选草稿。

### 本地数据恢复

目标：L72

- URL：`https://t.bilibili.com/1217003060937621510`
- 原始规则恢复为：

```text
#互动抽奖#福利加码 ~COLG也来啦【转赞评】关注@COLG玩家社区 +@虹领金官方账号 6月30日抽一个小可爱送出【10元京东e卡】 #供电局福利社##转发抽奖#
```

- 已保存动作计划恢复为：

```json
["followed", "liked", "commented", "reposted"]
```

说明：L72 曾经已经真实完成 `followed / reposted`，当前系统尚无“只补做缺失动作”的安全修复任务类型，因此不得把 L72 简单重新完整执行。后续需要引入 partial repair / missing-action-only 工作流。

## 变更文件

- `core/app/services/real_run_readiness.py`
- `core/tests/test_real_run_readiness.py`
- `worker/app/task_runner.py`
- `worker/tests/test_bilibili_dry_run_smoke.py`
- `worker/tests/test_bilibili_shadow_run_smoke.py`
- `worker/tests/test_bilibili_api_real_run.py`
- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/styles/workflows.css`
- `frontend/src/i18n/dictionaries.js`
- `dashboard/dist/`

## 验证证据

### Core 定向测试

```powershell
$env:PYTHONPATH='core'
.\.venv312\Scripts\python.exe -m unittest core.tests.test_real_run_readiness core.tests.test_lottery_rules
```

结果：

```text
Ran 12 tests
OK
```

### Worker 定向测试

```powershell
$env:PYTHONPATH='worker'
.\.venv312\Scripts\python.exe -m unittest worker.tests.test_bilibili_dry_run_smoke worker.tests.test_bilibili_shadow_run_smoke worker.tests.test_bilibili_api_real_run
```

结果：

```text
Ran 3 tests
OK
```

备注：测试输出中仍存在既有 Pydantic V2 `class-based config` 迁移警告，不影响本次变更。

### 前端构建

```powershell
cd frontend
npm run build
```

结果：

```text
vite build completed
```

### 本地部署验证

```powershell
docker compose restart core-api worker nginx
docker compose ps
```

结果：

- `core-api` healthy
- `mysql` healthy
- `nginx` healthy
- `redis` healthy
- `worker` healthy

首页 `http://127.0.0.1/` 返回 `200`。

### L72 当前门禁状态

- `required_actions`：`followed, liked, commented, reposted`
- `shadow_ready`：`true`
- `safe_accounts`：`0`
- blockers：
  - `global_real_run_disabled`
  - `no_calibrated_ready_account`

账号 14 因补做尝试触发 `action_window`，当前不应继续真实执行。

## 已知限制

1. 当前系统没有“补做缺失动作”任务类型。
2. 对已部分执行的抽奖，重新完整 real-run 可能造成重复转发或重复交互。
3. 后续需要把 task phases 与活动动作计划结合，生成 missing-action-only repair plan。
4. 前端已经能暴露保存计划与识别建议差异，但还未提供“一键生成补做任务”。

## 下一步建议

1. 新增 `repair_run` 或 `missing_action_run` 模式。
2. 对比 `task_phases` 与 `action_plan.required_actions`，只派发未完成动作。
3. real-run gate 对 repair 模式单独要求：
   - 最近一次原任务存在。
   - 缺失动作明确。
   - 不重复执行已完成动作。
   - 账号无 24 小时风险事件。
4. 前端为 L72 这类目标显示“部分完成 / 可补做 / 不可重跑”状态。

## 对应提交

本记录随本次 Bilibili 规则计划安全修复提交进入 Git 历史。
