# DPMS Bilibili 动作账本实施记录

## 背景

2026-06-24 的 Bilibili 真实账号验证中，操作者在页面上看到目标动态只完成了转发和关注，没有看到评论与点赞。这暴露出一个工程审计缺口：系统虽然会写入 `BilibiliApiActionCompleted` 事件，并且只有 `ok` 的动作才会写入 `TaskPhaseCompleted`，但日常页面与补做逻辑仍主要依赖事件 JSON 反推。

本次新增 `bilibili_action_ledger`，把每个 Bilibili API 真实业务动作的最终结果结构化保存，目标是回答：

- 该任务实际请求了哪些动作。
- 每个动作对应哪个 DPMS phase。
- Bilibili 返回的 `code/outcome/message` 是什么。
- 哪些动作真正 `ok`，可被视为已完成。
- 哪些动作没有账本记录，需要通过 action plan 或事件流继续审计。

## 安全边界

- 本次没有新增任何真实动作能力。
- 本次没有触发真实账号动作。
- 本次没有绕过验证码、平台风控或账号冷却。
- ledger 是只读审计能力，不是派发入口。
- 前端文案明确显示“只读动作审计；不会自动派发真实任务”。

## 实现内容

### Schema 与迁移

新增：

- `core/migrations/0009_bilibili_action_ledger.sql`
- `init.sql` 初始 schema 同步

表结构：

```sql
CREATE TABLE IF NOT EXISTS bilibili_action_ledger (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  task_id CHAR(36) NOT NULL,
  account_id BIGINT NOT NULL,
  lottery_id BIGINT NOT NULL,
  dynamic_id VARCHAR(64) NULL,
  action VARCHAR(32) NOT NULL,
  phase VARCHAR(32) NULL,
  code INT NULL,
  outcome VARCHAR(32) NOT NULL,
  message TEXT NULL,
  ok TINYINT DEFAULT 0,
  task_mode VARCHAR(32) NOT NULL DEFAULT 'real_run',
  source VARCHAR(32) NOT NULL DEFAULT 'api_real_run',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

关键约束：

- `UNIQUE KEY uk_bilibili_action_task_action (task_id, action)`
- `idx_bilibili_action_lottery_created`
- `idx_bilibili_action_account_created`
- `idx_bilibili_action_outcome_created`

生产 schema 校验：

- `core/app/migrations_runner.py` 已将 `bilibili_action_ledger` 加入 `PRODUCTION_REQUIRED_TABLES`。
- 必要列校验包含 `task_id/account_id/lottery_id/action/outcome/ok`。

### Worker 写入

文件：

- `worker/app/task_runner.py`
- `worker/tools/bilibili_dry_run_harness.py`
- `worker/tests/test_bilibili_api_real_run.py`

新增：

- `save_bilibili_action_ledger()`

写入时机：

1. `execute_bilibili_api_real_task()` 调用 `BilibiliApiExecutor.participate()`。
2. 遍历 `result.actions.items()`。
3. 对每个 `action/action_result` 写入 `bilibili_action_ledger`。
4. 继续写原有 `BilibiliApiActionCompleted` 事件。
5. 只有 `action_result.ok` 时继续写 `TaskPhaseCompleted`。

说明：

- 第一版记录的是业务动作最终结果，不是每个底层 HTTP retry attempt。
- ledger 写入失败会记录 `bilibili_action_ledger_write_failed` warning，但不会把已经发生的动作改判为任务失败，避免因审计写表临时失败导致重复真实动作。

### Core API 与 repair plan

文件：

- `core/app/api/lotteries.py`
- `core/tests/test_lottery_repair_plan.py`

新增：

- `normalize_action_ledger_row()`
- `bilibili_action_ledger_for_lottery()`
- `completed_real_run_actions_from_ledger()`
- `GET /api/lotteries/action-ledger`

`GET /api/lotteries/real-run/evidence` 每个 item 新增：

```json
{
  "action_ledger": [
    {
      "task_id": "...",
      "account_id": 14,
      "lottery_id": 72,
      "action": "like",
      "phase": "liked",
      "code": 0,
      "outcome": "ok",
      "message": "点赞成功",
      "ok": true,
      "created_at": "..."
    }
  ]
}
```

补做计划更新：

- `completed_real_run_actions()` 优先读取 ledger 中 `task_mode='real_run' AND ok=1 AND phase IS NOT NULL` 的记录。
- 若 ledger 没有历史数据，则回退到旧的 `events + task_runs` 事件流反推，避免 L72 这类历史任务的补做状态丢失。

### Frontend 展示

文件：

- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/i18n/dictionaries.js`
- `frontend/src/styles/workflows.css`
- `dashboard/dist/`

新增：

- `ActionLedgerSummary`
- `ledgerActionLabel()`
- `ledgerOutcomeLabel()`
- `ledgerOutcomeClass()`

展示位置：

- Activity Pool 的 `RealGateCell` 内部。
- 不放入操作列。
- 不新增派发按钮。

文案：

- `只读动作审计；不会自动派发真实任务。`
- `暂无 Bilibili API 动作账本记录。`

## 验证

### Python 编译

```powershell
python -m py_compile core\app\api\lotteries.py core\app\migrations_runner.py worker\app\task_runner.py
```

结果：通过。

### Core 定向测试

```powershell
$env:PYTHONPATH='core'
.\.venv312\Scripts\python.exe -m unittest core.tests.test_lottery_repair_plan core.tests.test_migrations_runner
```

结果：

```text
Ran 17 tests
OK
```

### Worker 定向测试

```powershell
$env:PYTHONPATH='worker'
.\.venv312\Scripts\python.exe -m unittest worker.tests.test_bilibili_api_real_run
```

结果：

```text
Ran 1 test
OK
```

### 前端构建

```powershell
cd frontend
npm run build
```

结果：通过，并更新 `dashboard/dist/` 静态资源。

### 容器验证限制

本次尝试执行：

```powershell
docker compose exec -T core-api python -m unittest tests.test_lottery_repair_plan tests.test_migrations_runner
docker compose exec -T worker python -m unittest tests.test_bilibili_api_real_run
```

当前 Docker Desktop 管道不可用：

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

因此本轮未完成容器内 migration 应用与健康检查。Docker 恢复后需要执行：

```powershell
docker compose restart core-api worker nginx
docker compose ps
```

并确认 `core-api` 启动日志包含 migration `0009` 已应用，或在 `schema_migrations` 中可查询到版本 `0009`。

## 后续建议

1. 第二阶段可把 ledger 下沉到 `worker/app/bilibili/client.py`，记录每个真实 HTTP route / retry attempt。
2. 后续可在 task 详情页增加按 `task_id` 查看 ledger 的折叠详情。
3. 后续可把 `bilibili_action_ledger` 接入 Semantic Runtime 的 Execution 层，让语义链直接解释“动作计划 → API 返回 → phase 完成”的关系。
