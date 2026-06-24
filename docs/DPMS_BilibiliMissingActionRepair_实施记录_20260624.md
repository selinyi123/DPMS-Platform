# DPMS Bilibili 缺失动作补做实施记录

## 背景

L72 真实执行事故复盘后，系统已经修复了规则计划过期与验证任务误入动作窗口的问题。但仍存在一个业务工程缺口：当一个真实任务已经完成部分动作时，不能简单重新完整执行，否则可能重复关注、重复转发或制造额外账号风险。

因此本次新增 missing-action repair 能力，只补做已确认缺失的动作。

## 实现目标

- 从事件流中识别真实 real-run 已完成动作。
- 将 `action_plan.required_actions` 与已完成动作对比。
- 生成只包含缺失动作的 `repair_action_plan`。
- 补做任务仍然作为 `real_run` 安全级别执行。
- 补做派发必须经过管理员确认、全局 real-run 开关、熔断器、真实门禁、Governance policy 和账号安全检查。
- 前端显示“已完成动作 / 缺失动作”，并只在存在缺失动作时显示补做按钮。

## 核心设计

不新增全局 `task_mode`。

补做派发使用：

```text
task_mode = real_run
```

但任务消息中的 `action_plan` 替换为：

```json
{
  "source": "missing_action_repair",
  "required_actions": ["liked", "commented"],
  "full_required_actions": ["followed", "liked", "commented", "reposted"],
  "completed_actions": ["followed", "reposted"]
}
```

这样 Worker 仍走原有真实执行链路，但执行器只会收到缺失动作，不会重复已完成动作。

## 变更内容

### 后端

文件：`core/app/api/lotteries.py`

新增：

- `ordered_actions()`
- `missing_repair_actions()`
- `completed_real_run_actions()`
- `build_lottery_repair_plan()`
- `GET /api/lotteries/{lottery_id}/repair-plan`
- `POST /api/lotteries/{lottery_id}/repair-dispatch`

`/real-run/evidence` 输出增加：

```json
{
  "repair_plan": {
    "eligible": true,
    "reason": "missing_actions_available",
    "required_actions": ["followed", "liked", "commented", "reposted"],
    "completed_actions": ["followed", "reposted"],
    "missing_actions": ["liked", "commented"]
  }
}
```

已完成动作来源：

- `events`
- `TaskPhaseCompleted`
- join `task_runs`
- 仅统计 `task_mode = 'real_run'`

这避免 dry-run / shadow-run 的模拟阶段污染真实补做计划。

### 前端

文件：

- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/i18n/dictionaries.js`
- `frontend/src/styles/workflows.css`

新增：

- real gate 列显示已完成动作与缺失动作。
- 活动存在缺失动作时显示“补做缺失”按钮。
- 按钮只有在真实门禁允许且存在安全账号时才可用。
- 派发路径调用 `POST /lotteries/{id}/repair-dispatch`，并带管理员确认头。

## L72 当前验证

只读接口：

```powershell
GET http://127.0.0.1/api/lotteries/72/repair-plan
```

返回：

```json
{
  "eligible": true,
  "reason": "missing_actions_available",
  "required_actions": ["followed", "liked", "commented", "reposted"],
  "completed_actions": ["followed", "reposted"],
  "missing_actions": ["liked", "commented"]
}
```

当前 `/real-run/evidence` 显示：

```text
repair_eligible = true
completed = followed,reposted
missing = liked,commented
blockers = global_real_run_disabled
```

说明：系统已具备补做计划生成能力，但由于全局 real-run 开关关闭，不会自动执行。

## 验证

### Core 定向测试

```powershell
$env:PYTHONPATH='core'
.\.venv312\Scripts\python.exe -m unittest core.tests.test_lottery_repair_plan core.tests.test_real_run_readiness core.tests.test_outbox
```

结果：

```text
Ran 19 tests
OK
```

### Worker 定向测试

```powershell
$env:PYTHONPATH='worker'
.\.venv312\Scripts\python.exe -m unittest worker.tests.test_bilibili_api_real_run worker.tests.test_bilibili_dry_run_smoke worker.tests.test_bilibili_shadow_run_smoke
```

结果：

```text
Ran 3 tests
OK
```

### 前端构建

```powershell
cd frontend
npm run build
```

结果：通过。

### 本地部署

```powershell
docker compose restart core-api nginx
docker compose ps
```

结果：

- `core-api` healthy
- `mysql` healthy
- `nginx` healthy
- `redis` healthy
- `worker` healthy

## 安全边界

- 本次没有触发真实账号动作。
- 补做派发不绕过任何 real-run 门禁。
- 若账号存在近期风险事件，Governance policy 仍会阻断。
- 若全局 real-run 开关关闭，补做按钮不可用，接口也会拒绝派发。

## 已知限制

1. `task_phases` 当前只保存每个 task 的最新阶段，补做计划依赖 `events` 事件流。
2. 若历史 real-run 没有成功写入 `TaskPhaseCompleted` 事件，系统无法可靠推导已完成动作。
3. 当前补做动作粒度为活动级；后续可进一步加入“动作级证据截图 / API 回执”。

## 对应提交

本记录随 Bilibili missing-action repair 能力提交进入 Git 历史。
