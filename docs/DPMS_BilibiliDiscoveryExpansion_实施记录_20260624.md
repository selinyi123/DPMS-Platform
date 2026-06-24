# DPMS Bilibili 自动发现扩散实施记录

## 背景

用户要求系统不只依赖手工录入目标，而要能够自动搜索 Bilibili 抽奖活动，并最终服务于“真实账号、真实活动、受控执行”的工作流。

此前 DPMS 已具备 Bilibili UP 动态发现、账号 Cookie/二维码登录、账号校准、shadow-run、API real-run 通道和 Governance real-run gate，但自动发现源主要停留在“抽奖合集动态”层面。合集动态可以提供入口，却不适合作为真实执行目标直接参与。

## 代码提交

- 代码提交：`e872913 Expand Bilibili discovery sources`

## 变更范围

### Bilibili 合集源扩散

- `core/app/services/discovery.py`
  - 新增 `expanded_sources` 发现统计字段。
  - 新增 `expand_bilibili_collection_sources()`。
  - 从 Bilibili 合集动态正文中识别 `【UP 名称】、【UID】` 结构。
  - 自动写入新的 `tracked_sources`：
    - `platform = bilibili`
    - `source_type = up`
    - `source_value = UID`
  - 单次扫描最多扩散 `80` 个 UP 源。
  - 跳过当前父源 UID，避免把合集工具人自身重复加入。
  - 在“充电感谢名单 / 本月充电 / 上月充电 / 感谢您对”等尾部名单前截断，避免把普通用户误认为抽奖源。

### 规则文本与动作计划刷新

- `core/app/services/discovery.py`
  - 对已存在的 `canonical_url` 不再只跳过。
  - 重复目标会刷新：
    - `title`
    - `rule_text`
    - `action_plan`
    - `published_at`
    - `value_score`
  - 这使后续扫描可以逐步修复旧采集数据，而不需要手工清库。

### Bilibili 文本编码修复

- `core/app/services/lottery_rules.py`
  - 新增 `repair_mojibake()`。
  - 支持将 UTF-8 被 Latin-1/CP1252 误解码后的文本恢复为中文。
  - `normalize_text()` 在规则匹配前自动修复疑似乱码。

- `core/app/services/bilibili_discovery.py`
  - 动态文本进入候选对象前执行 `repair_mojibake()`。

### 多平台规则解析基线整理

- `core/app/services/lottery_rules.py`
  - 扩展 Bilibili、Weibo、Xiaohongshu、Douyin 的正常中文规则模式。
  - Bilibili 支持 `转赞评` 同时映射点赞、评论、转发。
  - 小红书 `收藏` 继续作为 unsupported action，强制人工复核。
  - 微博 `@好友/朋友` 继续作为 unsupported action，避免自动化误执行平台敏感互动。

### 测试

- `core/tests/test_bilibili_collection_expansion.py`
  - 覆盖合集正文中 UP 源抽取、去重、父源排除。

- `core/tests/test_bilibili_discovery.py`
  - 覆盖正常中文动态解析。
  - 覆盖 Latin-1 乱码自动修复。

- `core/tests/test_lottery_rules.py`
  - 更新为正常中文规则样例。

## 本地部署验证

### 容器状态

`docker compose ps` 显示以下服务均为 healthy：

- `core-api`
- `mysql`
- `nginx`
- `redis`
- `worker`

### 自动发现验证

第一次扫描：

```json
{
  "status": "scanned",
  "sources": 4,
  "scanned": 4,
  "found": 14,
  "inserted": 0,
  "expanded_sources": 80,
  "expired": 0,
  "failed": 2
}
```

第二次扫描：

```json
{
  "status": "scanned",
  "sources": 84,
  "scanned": 80,
  "found": 70,
  "inserted": 70,
  "expanded_sources": 0,
  "expired": 0,
  "failed": 0
}
```

扫描后 Bilibili 活跃源池：

- `up` 源：`81`
- `url_list` 源：`1`

Bilibili 待处理目标：

- pending 目标约 `90+`
- 高价值目标 `73`

### 影子运行验证

验证目标：

- Lottery ID：`72`
- URL：`https://t.bilibili.com/1217003060937621510`
- 目标类型：Bilibili dynamic
- 模式：`shadow_run`
- 账号：`13`
- Task ID：`a5d91fe9-e09b-4b3b-92dc-a9282574c307`

结果：

- `task_runs.status = succeeded`
- Worker 日志事件：`shadow_run_task_completed`
- Redis `lottery_tasks` pending：`0`

### 真实执行门禁状态

`/api/governance/real-run/72` 返回：

- `valid_lottery_target = true`
- `action_plan_reviewed = true`
- `calibrated_account_available = true`
- `real_adapter_enabled = true`
- `recent_complete_probe = true`
- `recent_shadow_run = true`
- `no_recent_account_risk = true`
- `circuit_breaker_closed = true`
- `global_real_run_enabled = false`

最终结果：

- `outcome = block`
- 唯一阻塞项：`global_real_run_enabled`
- 下一步：`enable_real_run`

## 当前能力范围

本节点完成后，DPMS 在 Bilibili 上具备以下闭环：

1. 从已配置的 Bilibili 源扫描动态。
2. 从抽奖合集动态中自动识别 UP UID。
3. 将识别出的 UP 自动加入追踪源池。
4. 二次扫描真实 UP 动态并生成候选抽奖目标。
5. 自动解析规则动作计划。
6. 将目标进入 strategy queue。
7. 使用已校准账号执行 shadow-run。
8. 由 Governance real-run gate 判断是否允许真实执行。

## 安全边界

本节点没有开启真实账号动作。

系统仍遵守以下边界：

- 不绕过验证码。
- 不接入打码平台。
- 不隐藏自动化行为。
- 不规避平台风控。
- 不在用户未确认具体目标和动作前执行真实关注、点赞、评论、转发。
- 触发验证码、限流、风控或登录失效时，行为是停止、记录、通知、冷却账号。

## 当前限制

- Bilibili 当前具备 1 个已校准安全账号，真实执行仍需人工开启全局开关并确认具体目标。
- Weibo、Douyin、Xiaohongshu 当前缺少已校准安全账号和真实动作适配器证据。
- 代理池当前无 active proxy exit，生产级多账号运行前仍需补齐账号隔离出口。
- 最近 24 小时内存在 1 条风险事件，策略队列仍会降低推荐执行模式。

## 验证命令

```powershell
$env:PYTHONPATH='core'; .\.venv312\Scripts\python.exe -m unittest core.tests.test_bilibili_collection_expansion core.tests.test_bilibili_discovery core.tests.test_lottery_rules
```

结果：`17` 项通过。

```powershell
$env:PYTHONPATH='core'; .\.venv312\Scripts\python.exe -m unittest discover core\tests
```

结果：`341` 项通过。

```powershell
.\.venv312\Scripts\python.exe -m py_compile core\app\services\discovery.py core\app\services\bilibili_discovery.py core\app\services\lottery_rules.py
```

结果：通过。

```powershell
git diff --check
```

结果：通过。

## 后续事项

1. 在 UI 中区分“合集入口动态”和“真实 UP 活动动态”，避免策略队列顶部混入合集入口。
2. 增加“自动发现源扩散图”，展示父源、扩散 UP、候选目标数量。
3. 在用户明确确认具体 Bilibili 目标和 required actions 后，再执行 real-run。
4. 为生产账号补齐代理出口和隔离策略。
