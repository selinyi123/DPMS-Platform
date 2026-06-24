# DPMS Bilibili 风险感知发现与门禁解释实施记录

## 背景

2026-06-24 的真实账号验证暴露出两个相邻问题：

- L72 的真实执行只完成了 `followed / reposted`，缺失 `liked / commented`。
- 后续补做尝试被账号动作窗口拦截，账号 14 写入 `cooling` 风险事件，但前端仍只显示通用 blocker，操作者无法直观看到“当前不是缺账号，而是账号处于近期风险冷却”。

同时，历史项目 `bili-lottery-v5` 与 `bilibili-lottery-system` 中有一个对当前系统仍有价值的经验：通过关键词检索发现 Bilibili 抽奖动态。当前 DPMS 已有 `keyword` 源类型，但 Bilibili keyword 分支仍返回空结果，导致发现能力没有真正跟上新版本。

本次目标是在不绕过平台风控、不自动继续真实动作的前提下，补齐以下组件：

- Bilibili 关键词发现。
- real-run 账号近期风险冷却门禁。
- 前端 real-run 状态、账号池与风险冷却解释。

## Agent 审阅流

本次采用三路只读审阅，再由主流程落地实现：

- Frontend Review：审阅 `frontend/src/pages/Lotteries.jsx` 的 real-run 操作流，确认旧 UI 对全局开关、缺失动作与风险冷却解释不足。
- Execution Chain Review：审阅 Bilibili `required_actions`、事件流、repair plan 与真实动作执行链，确认缺失动作来自已保存 `action_plan` 与真实事件对比。
- Historical Asset Review：审阅历史仓库经验，确认关键词搜索发现、中奖跟踪、奖品元数据和通知模板是可迁移方向；本次优先落地关键词发现。

## 实现内容

### Bilibili 关键词发现

文件：

- `core/app/services/bilibili_discovery.py`
- `core/app/services/discovery.py`
- `core/tests/test_bilibili_discovery.py`

新增能力：

- `fetch_bilibili_keyword_search(keyword, pages=2, limit=30, cookie_header=None)`。
- 调用 Bilibili `x/web-interface/search/type` 的 `dynamic` 检索类型。
- 支持 WBI 参数签名。
- 解析搜索结果中的 `dynamic_id`、标题、描述、发布时间与跳转 URL。
- 复用既有 `build_action_plan()` 和 `validate_lottery_target()`，只保留可识别为抽奖候选的动态。
- `discovery.py` 的 Bilibili `keyword` 源不再返回空列表，而是按逗号、分号、换行切分关键词后逐个搜索。
- 如本地存在可用 Bilibili 账号 Cookie，则优先带 Cookie 搜索；无 Cookie 时尝试匿名搜索，失败时记录 warning，不派发任何真实动作。

安全边界：

- 该能力只用于发现候选目标。
- 不进行点赞、评论、转发、关注。
- 不尝试绕过验证码、登录限制或风控。
- 搜索失败不会阻塞核心 API 启动。

### 账号近期风险冷却门禁

文件：

- `core/app/services/real_run_readiness.py`
- `core/app/api/lotteries.py`
- `core/tests/test_real_run_readiness.py`

新增能力：

- `ACCOUNT_RISK_COOLDOWN_HOURS = 24`。
- `recent_account_risk(account_id)` 查询账号最近风险事件。
- `real_run_account_risk_summary(platform)` 汇总平台可用账号与风险冷却账号。
- `account_risk_payload(row)` 输出结构化风险说明。
- `validate_real_run_evidence()` 在存在近期风险事件时加入 `recent_account_risk_event` blocker。
- `real_run_gate_status()` 返回 `safe_accounts`、`risk_clear_accounts` 与 `account_risk`。
- real-run 派发选账号时优先选择已校准、ready 且无近期风险事件的账号。

当前 L72 只读门禁证据：

```json
{
  "lottery_id": 72,
  "allowed": false,
  "blockers": ["global_real_run_disabled", "recent_account_risk_event"],
  "next_action": "review_risk",
  "safe_accounts": 1,
  "risk_clear_accounts": 0,
  "account_risk": {
    "has_recent_risk": true,
    "cooldown_hours": 24,
    "latest_event": {
      "id": 4,
      "account_id": 14,
      "event_type": "cooling",
      "detail": {"reason": "action_window"},
      "created_at": "2026-06-24T06:16:36"
    },
    "cooldown_until": "2026-06-25T06:16:36"
  },
  "repair_plan": {
    "eligible": true,
    "missing_actions": ["liked", "commented"],
    "completed_actions": ["followed", "reposted"],
    "required_actions": ["followed", "liked", "commented", "reposted"]
  }
}
```

说明：系统能识别缺失动作，但由于全局 real-run 关闭且账号存在近期风险冷却，不允许继续真实执行。

### 前端门禁解释

文件：

- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/i18n/dictionaries.js`
- `frontend/src/styles/workflows.css`
- `dashboard/dist/`

新增能力：

- 顶部显示全局 real-run 开关状态。
- 活动行的“补做缺失”按钮在不可用时显示原因，而不是只依赖浏览器 tooltip。
- real-run 门禁列显示账号池状态：`risk_clear_accounts / safe_accounts`。
- 若账号存在近期风险事件，显示冷却截止时间。
- 修复 `completed_actions` 为空时缺失动作摘要不显示的问题。
- 增加中英文 i18n 文案：
  - `realRunSwitchOn`
  - `realRunSwitchOff`
  - `realRunAccountPool`
  - `riskCooldownUntil`

## 验证

### Python 编译检查

```powershell
python -m py_compile `
  core\app\services\bilibili_discovery.py `
  core\app\services\discovery.py `
  core\app\services\real_run_readiness.py `
  core\app\api\lotteries.py
```

结果：通过。

### 容器内定向断言

```powershell
docker compose exec -T core-api python -
```

验证内容：

- `account_risk_payload()` 能输出 `cooldown_until`。
- `missing_repair_actions()` 能从完整计划中计算 `liked / commented`。
- `parse_search_item()` 能将 Bilibili 搜索结果解析为候选目标。
- `split_keywords()` 能识别逗号、分号与换行分隔的关键词。

结果：通过。

### 前端构建

```powershell
cd frontend
npm run build
```

结果：通过，并更新 `dashboard/dist/` 静态资源。

### 本地服务验证

```powershell
docker compose restart core-api
docker compose ps core-api
```

结果：`core-api` 恢复为 healthy。

只读接口验证：

```powershell
GET http://127.0.0.1/api/lotteries/real-run/evidence?limit=100
```

结果：L72 返回 `recent_account_risk_event` blocker，且补做计划仍显示 `liked / commented` 为缺失动作。

## 未完成与后续建议

1. 当前关键词发现仍是候选目标发现，不等同于真实执行；需要继续结合规则审核、shadow-run 和 Governance gate。
2. 关键词检索与 UP 源池发现存在重复去重压力，后续可把动态 ID 去重与来源权重写入 Knowledge Runtime。
3. 当前风险冷却窗口为固定 `24` 小时，后续可接入 V6 Risk Intelligence 的账号信誉分和平台风险预测，形成动态冷却。
4. Playwright 可视化验证环境缺少浏览器二进制，本次未完成自动截图校验；前端以 `npm run build` 和接口返回为准。

## 安全边界

- 本次没有触发真实账号动作。
- 本次没有绕过验证码或风控。
- 本次没有清除风险事件。
- 账号处于 `cooling` 或近期风险冷却时，系统会阻止 real-run 和 missing-action repair。
- 全局 real-run 开关关闭时，前端按钮与后端接口均保持阻断。
