# DPMS Bilibili 分级风险冷却实施记录

## 背景

2026-06-25 的本地真实环境验证中，L101 已完成 Bilibili 真实目标校验、动作计划审核与 shadow-run 观察，但 real-run 门禁仍被 `recent_account_risk_event` 阻断。进一步检查发现，阻断来源是账号 14 在 2026-06-24 06:16:36 记录的一条 `cooling` 风险事件，原因是 `action_window`。

旧策略把所有账号风险事件统一冷却 **24 小时**。这对验证码、登录失效、页面风控、封禁等硬风险是合理的，但对 `action_window` 这类本地频率窗口事件过于保守，会导致一次节流事件使后续抽奖停摆一天。

本次变更目标是：在不绕过验证码、不清空风险事件、不关闭安全门禁的前提下，把风险冷却从“一刀切 24 小时”改为“按原因分级冷却”。

## 安全边界

- 本次没有清除、篡改或隐藏任何历史 `risk_events`。
- 本次没有绕过验证码、页面安全验证、平台频率限制或账号登录状态检查。
- 本次没有自动打开 `REAL_RUN_ENABLED` 全局开关。
- 本次没有触发真实账号动作。
- 已过期的短冷却只是不再阻断门禁；真实执行仍需全局开关、账号校准、shadow-run、动作计划、Governance policy 和管理员确认同时满足。

## 分级冷却策略

| 风险原因 | 冷却时间 | 说明 |
| --- | ---: | --- |
| `action_window` | 4 小时 | 本地动作窗口过密，属于节流暂停，不应锁死一天。 |
| `sliding_window_exceeded` | 4 小时 | 旧风险引擎的滑动窗口超限，同样按短冷却处理。 |
| `daily_limit` | 24 小时 | 达到每日任务上限，保持完整日冷却。 |
| `page_risk_signal` | 24 小时 | 页面出现验证码、安全验证、账号异常等硬风险信号。 |
| `redirected_to_login` | 24 小时 | 登录态失效或被重定向到登录页。 |
| `execution_timeout` | 24 小时 | 执行态超时，需要保守恢复。 |
| `bilibili_*_captcha` | 24 小时 | Bilibili API 返回验证码类结果。 |
| `bilibili_*_limit` | 24 小时 | Bilibili API 返回平台限制类结果。 |
| `bilibili_*_risk` | 24 小时 | Bilibili API 返回风险类结果。 |
| 未识别原因 | 24 小时 | Fail-safe 默认策略。 |

## 实现内容

### Core 风险冷却算法

文件：

- `core/app/services/real_run_readiness.py`

新增：

- `ACCOUNT_RISK_COOLDOWN_BY_REASON`
- `account_risk_cooldown_hours()`
- `account_risk_cooldown_until()`
- `account_risk_is_active()`
- `current_db_time()`

关键变化：

1. `recent_account_risk(account_id)` 不再直接查询“最近 24 小时最新一条风险事件”。
2. 新逻辑先读取最大观察窗口内的风险事件，再按每条事件的 `detail.reason` 计算冷却截止时间。
3. 只有当前时间仍小于冷却截止时间的事件才会返回 `has_recent_risk=true`。
4. 已按分级策略过期的旧事件仍保留在数据库中，但不再阻断 real-run 门禁。

### 账号选择逻辑

文件：

- `core/app/api/lotteries.py`

旧逻辑：

```sql
AND NOT EXISTS (
  SELECT 1 FROM risk_events r
  WHERE r.account_id = a.id
    AND r.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
)
```

新逻辑：

1. 先按账号可用性、校准状态和 `daily_task_count` 找候选账号。
2. 再调用 `recent_account_risk(account_id)` 使用统一分级冷却策略判断。
3. 推荐账号与普通候选账号都使用同一套判断，避免 real-run evidence 与自动选号规则不同步。

### 测试基线

文件：

- `core/tests/test_real_run_readiness.py`

新增断言：

- `action_window` 冷却时间为 4 小时。
- `page_risk_signal`、`redirected_to_login` 和未知原因仍为 24 小时。
- `account_risk_payload()` 输出的 `cooldown_until` 按分级策略计算。

## 本地部署态验证

### 编译验证

```powershell
python -m py_compile core\app\services\real_run_readiness.py core\app\api\lotteries.py
docker compose exec -T core-api python -m py_compile /app/app/services/real_run_readiness.py /app/app/api/lotteries.py
```

结果：通过。

### 函数级验证

在 `core-api` 容器内验证：

```text
action_window -> cooldown_hours=4, cooldown_until=2026-06-24T10:16:36
page_risk_signal -> cooldown_hours=24
```

结果：通过。

### 数据库连接验证

在 `core-api` 容器内连接数据库后调用：

```python
await recent_account_risk(14)
await real_run_account_risk_summary("bilibili")
```

结果：

```json
{
  "has_recent_risk": false,
  "cooldown_hours": 24
}
```

```json
{
  "ready_accounts": 1,
  "runnable_accounts": 1,
  "latest_recent_risk": {
    "has_recent_risk": false,
    "cooldown_hours": 24
  }
}
```

说明：账号 14 的旧 `action_window` 风险事件已经按 4 小时策略过期。

### L101 real-run 门禁验证

重启 `core-api` 后查询 `GET /api/lotteries/real-run/evidence?limit=120`，L101 当前状态：

```json
{
  "lottery_id": 101,
  "allowed": false,
  "blockers": ["global_real_run_disabled"],
  "next_action": "enable_real_run",
  "real_run_enabled": false,
  "safe_accounts": 1,
  "risk_clear_accounts": 1,
  "account_risk": {
    "has_recent_risk": false,
    "cooldown_hours": 24
  },
  "probe_ready": true,
  "shadow_ready": true,
  "action_plan_ready": true
}
```

结论：旧的 `recent_account_risk_event` blocker 已解除；当前唯一阻断项是全局真实执行开关。

## 仍保留的限制

1. 全局 `REAL_RUN_ENABLED` 默认保持关闭。
2. Bilibili API 返回验证码、限制、风险或登录失效时仍会使账号进入保守冷却。
3. `daily_limit` 仍按 24 小时处理，避免在同一天继续堆叠真实动作。
4. 本次未修改 Worker 的实时动作窗口阈值：默认仍为 10 分钟最多 2 次 real-run 任务启动，小红书为 1 次。
5. 本次未修改自动恢复策略；账号状态恢复仍由现有健康检查、校准流程和人工操作共同约束。

## 后续建议

1. 在前端风险解释中显示 `cooldown_hours`，让操作者知道这是短冷却还是硬风险长冷却。
2. 将 `ACCOUNT_RISK_COOLDOWN_BY_REASON` 后续迁移到 Governance policy，使不同平台可以版本化配置。
3. 增加后台指标：按原因统计短冷却、硬风险冷却、过期冷却事件数量。
4. 在真实执行前继续保持人工确认：只有在 `global_real_run_disabled` 被有意打开且门禁无其他 blocker 时，才允许真实账号动作。
