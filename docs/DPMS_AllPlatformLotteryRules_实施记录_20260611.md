# DPMS 全平台抽奖规则与抖音模块实施记录

## 背景与问题

微博/小红书已具备选择器驱动 + 门禁保护的执行链路，但规则解析仍是 Bilibili 专属代码：

- `bilibili_discovery.py` 中的 `parse_lottery_rule`/`ACTION_PATTERNS`/`LOTTERY_PATTERNS` 仅按 Bilibili 用语设计，微博"转发抽奖"、小红书"评论区抽 1 位"等表述无法被准确识别。
- 微博"@N位好友"、小红书"收藏"等平台特有的抽奖参与方式，在适配器中没有对应阶段，规则解析若强行映射到 `followed/liked/commented/reposted` 会产生误判。
- 抖音仍停留在 `planned.py` 的通用占位适配器，目标校验、规范化器、选择器完整性判定、身份校准均未覆盖。
- 小红书风控相对微博/Bilibili 更严格，但限速与风险词表未做平台区分。
- 运营人员录入规则文本后没有"建议动作计划"的辅助工具，只能手填复选框。

## 目标与安全边界

延续既有安全边界：反爬与账号安全仅指合规限速、风险识别、账号隔离，以及触发验证码/审核时停止并通知，不用于绕过验证码或规避平台保护机制。本次工作严格在该边界内：

- 新增的平台风险词表与限速参数均只触发既有 `set_account_status(account_id, "cooling"/"login_required", reason)` 停止并通知路径，不引入任何绕过或隐藏逻辑。
- 抖音按微博/小红书的既有模式（选择器驱动适配器 + 结构化选择器要求 + 身份校准降级策略）补齐，未引入新的安全机制类型。

## 实现内容

### 1. 共享的平台感知规则解析器

新增 `core/app/services/lottery_rules.py`，提取并扩展原 Bilibili 专属的 `parse_lottery_rule`：

- 为 bilibili / weibo / xiaohongshu / douyin 分别定义 `*_ACTION_PATTERNS`、`*_LOTTERY_PATTERNS`、`*_AMBIGUOUS_PATTERNS`：
  - 微博增加"转发本微博"、"转起"、"抽N位"等转发抽奖常见表述。
  - 小红书增加"评论区抽"、"双击点赞"、"包邮"、"送出"等。
  - 抖音增加"评论抽"、"双击点赞"等。
- 新增 `unsupported_actions` 概念与 `PLATFORM_UNSUPPORTED_ACTION_PATTERNS`：
  - 微博"@N位好友/艾特好友" → `mention_friends`（适配器无对应阶段，需人工 @ 好友）。
  - 小红书"收藏" → `favorited`（适配器无对应阶段，需人工收藏）。
  - 命中 `unsupported_actions` 时不会被错误映射到 `followed/liked/commented/reposted`，而是强制 `review_required = True` 并将 `confidence` 下调 0.15。
- `parse_lottery_rule(text, platform="bilibili")` 返回结构新增 `platform`、`unsupported_actions` 字段，未知平台回退到 Bilibili 模式集合（向后兼容）。
- `bilibili_discovery.py` 移除重复的 `ACTION_PATTERNS`/`LOTTERY_PATTERNS`/`AMBIGUOUS_PATTERNS`/`parse_lottery_rule`/`normalize_text`，改为 `from app.services.lottery_rules import parse_lottery_rule` 并调用 `parse_lottery_rule(rule_text, "bilibili")`，行为保持不变（回归测试覆盖）。

### 2. 抖音平台补齐至与微博/小红书同等水平

- `core/app/utils/lottery_targets.py` 新增 `validate_douyin_target`：接受 `douyin.com/video/{数字id}`、`iesdouyin.com/share/video/{数字id}`、`v.douyin.com` 短链；拒绝首页与用户主页。
- `core/app/utils/canonicalizer.py` 新增 `DouyinCanonicalizer`：将 web 视频页与 `iesdouyin.com` 分享页归一到同一 `canonical://douyin/video/{id}`；`PLATFORM_CANONICALIZERS` 新增 `"douyin"` 映射。
- 两侧 `adapter_config.py` 的 `STRUCTURED_SELECTOR_PLATFORMS` 新增 `"douyin"`，要求 followed/liked/reposted 有 click 选择器、commented 为含 input+submit 的结构化组。
- 新增 `worker/app/adapters/douyin.py`：基于共享的 `SelectorFlowAdapter`，按抖音页面控件特征（`data-e2e='follow-icon'`/`like-icon`/`comment-input`/`share-icon`）声明默认探针选择器；`registry.py` 改为从该模块导入 `DouyinAdapter`。
- 删除 `worker/app/adapters/planned.py`（仅用于占位 `DouyinAdapter`/`ConfigurableSelectorAdapter`，已确认无其他引用）。
- `account_calibrator.py` 新增 `verify_douyin_identity`：尝试 `douyin.com/aweme/v1/web/query/user/`，未登录或接口不可用（抖音 Web 接口依赖客户端 JS 生成的 `msToken`/`X-Bogus` 签名）时降级为 Cookie 存在性校验，记录降级原因，不阻塞校准。
- 两侧 `platforms.py` 中抖音 `adapter_status` 由 `planned` 改为 `calibration_required`。

### 3. 小红书反检测合规限速与风险词表

在既有"停止并通知"框架内，按平台收紧参数并扩充风险提示词：

- `worker/app/safety.py` 新增 `PLATFORM_WINDOW_MAX_ACTIONS`（小红书 = 1，其余沿用默认 2）与 `PLATFORM_DAILY_MAX_TASKS`（小红书 = 5，其余沿用默认 8），`ensure_account_can_run(account_id, platform=None)` 按平台读取阈值。
- 新增 `PLATFORM_RISK_TEXTS`：
  - 小红书："设备环境异常"、"当前账号存在异常行为"、"访问频率过高"、"访问太频繁，请稍后重试"、"笔记不存在或已被删除"、"账号已被限制"。
  - 微博："请求频繁，稍后再试"、"系统检测到异常访问"、"该微博已被删除"。
  - 抖音："请进行安全验证后继续操作"、"当前设备环境异常"、"视频不存在或已被作者删除"。
- `detect_page_risk(page, account_id, platform=None)` 命中 `RISK_TEXTS + PLATFORM_RISK_TEXTS[platform]` 时，与原逻辑一致地调用 `set_account_status(account_id, "cooling", "page_risk_signal")` 并停止当前任务。
- `task_runner.py`（`execute_shadow_run`、`execute_real_task`、daily-limit 检查）与 `account_calibrator.py` 的调用点均传入 `platform`，使上述平台特定参数生效。

### 4. 规则计划建议 API 与前端

- `core/app/api/lotteries.py` 新增 `GET /lotteries/{lottery_id}/action-plan/suggest`：可传 `rule_text`（缺省取活动已存的 `rule_text`），按活动平台调用 `parse_lottery_rule` 返回建议的动作计划（含 `confidence`、`ambiguity_patterns`、`unsupported_actions`）。仅需 `viewer` 权限，不修改数据。
- `frontend/src/pages/Lotteries.jsx` 的 `RulePlanEditor`：
  - 新增"建议动作计划"按钮，调用上述接口并用返回的 `required_actions` 直接更新复选框状态。
  - 新增建议元信息展示：置信度百分比、"规则文本含模糊表述，请人工核实"提示、`unsupported_actions` 对应的"需人工处理：@好友（需人工艾特）/收藏（需人工收藏）"提示。
  - 操作员仍需点击"确认动作计划"才会持久化（`PUT /action-plan`），建议结果不会自动保存。
- `frontend/src/uiContext.jsx` 中/英文词典新增 `lotteries.suggestRule`、`suggesting`、`suggestionConfidence`、`suggestionAmbiguous`、`suggestionUnsupportedPrefix`、`unsupportedActions.{mention_friends,favorited}`。
- `frontend/src/index.css` 新增 `.rule-suggestion` 样式。

## 变更文件

- `core/app/services/lottery_rules.py`（新增）、`core/app/services/bilibili_discovery.py`
- `core/app/utils/lottery_targets.py`、`canonicalizer.py`
- `core/app/adapter_config.py`、`platforms.py`、`api/lotteries.py`
- `core/tests/test_lottery_rules.py`（新增）、`test_lottery_targets.py`、`test_canonicalizer.py`
- `worker/app/adapters/douyin.py`（新增）、`adapters/registry.py`、`adapters/planned.py`（删除）
- `worker/app/adapter_config.py`、`platforms.py`、`safety.py`、`account_calibrator.py`、`task_runner.py`
- `frontend/src/pages/Lotteries.jsx`、`uiContext.jsx`、`index.css`
- `dashboard/dist/*`（前端重新构建产物）

## 验证证据

- `python3 -m unittest tests.test_lottery_targets tests.test_canonicalizer tests.test_bilibili_discovery tests.test_bilibili_qr tests.test_lottery_rules`：54 项全部通过（含 10 项新增规则解析用例与 Bilibili 规范化回归用例）。
- 全部改动 Python 文件 `py_compile` 通过。
- `npm install && npm run build`：前端构建成功，产物已同步到 `dashboard/dist`。
- `npm run dev` 临时启动开发服务器，确认 `Lotteries.jsx` 正常转译且页面可访问（未联调后端 API，环境无可运行的数据库/Redis）。

## 已知限制

- 抖音身份校验接口可能要求签名头（`msToken`/`X-Bogus`），不可用时降级为 Cookie 存在性校验，与小红书一致。
- 抖音、微博、小红书的默认探针选择器基于平台通用控件特征，真实选择器仍必须通过探针证据人工复核后保存。
- "建议动作计划"为辅助提示，不替代人工确认；`unsupported_actions` 命中的活动仍需人工完成 @ 好友 / 收藏等操作并在执行记录中体现。
- Worker 端适配器与安全逻辑仍无自动化测试（既有缺口）。

## 下一步

1. 用真实抖音账号导入 Cookie 并完成校准，验证身份接口实际可用性与降级路径。
2. 录入真实抖音抽奖链接，跑通探针 → 选择器复核 → Shadow-run 链路。
3. 收集更多平台真实活动文案，持续校准各平台的抽奖/动作/歧义正则。
4. 评估为微博、抖音也引入平台特定限速参数（当前仅小红书收紧）。

## 对应 Git 提交

- `ca7f702 Add Douyin lottery module and platform-aware rule parsing`
- 时间线见 [[DPMS_活动时间线]] 2026-06-11 条目。
