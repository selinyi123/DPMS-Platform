# DPMS 微博与小红书抽奖模块实施记录

## 背景与问题

Bilibili 已具备完整的抽奖执行链路（目标校验 → 探针 → 选择器校准 → Shadow-run → 安全门禁 → Real-run），但微博和小红书仍停留在 `planned.py` 中的通用占位适配器：

- 没有平台目标 URL 校验，首页、个人主页等不可执行页面可以进入探针和执行流程。
- 没有平台规范化器，同一条微博/笔记的不同 URL 形态（桌面、移动、短链）会被当作不同活动重复入库。
- 选择器完整性判定只要求"四个阶段非空"，字符串占位即可解锁真实动作，远弱于 Bilibili 的结构化要求。
- 账号校准只检查 Cookie 名存在，不验证登录态真实有效。
- 登录态失效跳转、真实执行门禁阻塞通知均只覆盖 Bilibili。

## 目标与安全边界

将微博、小红书提升为与 Bilibili 同等的"选择器驱动 + 门禁保护"平台。安全边界与既有立场一致：

- 真实动作默认关闭（`action_adapter: False`），必须先用真实页面探针证据校准选择器。
- 评论阶段必须配置 input + submit 结构化选择器组，点击阶段必须有显式 click 选择器。
- 登录跳转、风控提示词命中时停止并通知，不绕过验证码或平台保护机制。
- 抖音保持 `planned` 状态不变，不在本次范围。

## 实现内容

### Worker 适配器重构

- 新增 `worker/app/adapters/selector_flow.py`：将 BilibiliAdapter 的选择器驱动执行逻辑（done 状态校验、结构化评论输入/提交、转发确认、探针选择器合并、REAL_ACTIONS 完整性判定）提炼为共享基类 `SelectorFlowAdapter`。
- `bilibili.py` 精简为仅声明 `PLATFORM` 和默认探针选择器的子类，行为不变。
- 新增 `weibo.py`、`xiaohongshu.py`：按各自页面控件特征（微博：关注/赞/评论框/转发；小红书：关注/点赞/contenteditable 评论/发送/分享）声明默认探针选择器。
- `planned.py` 仅保留通用占位基类与 DouyinAdapter；`registry.py` 改为从专属模块注册微博/小红书适配器。

### 目标校验与规范化（core）

- `lottery_targets.py`：新增 `validate_weibo_target`（接受 `weibo.com/{uid}/{mblogid}`、`weibo.com/detail/{mid}`、`m.weibo.cn/status|detail/{id}`、`t.cn` 短链；拒绝首页、`/u/`、`/n/`、`/hot/` 等）与 `validate_xiaohongshu_target`（接受 `explore/{note_id}`、`discovery/item/{note_id}`（24 位十六进制）、`xhslink.com` 短链；拒绝首页与用户主页）。
- `canonicalizer.py`：新增 `WeiboCanonicalizer`（桌面/移动/detail 三种形态归一到 `canonical://weibo/status/{id}`，t.cn 短链先解析重定向）、`XiaohongshuCanonicalizer`（explore 与 discovery/item 归一到 `canonical://xiaohongshu/note/{note_id}`，统一小写）；新增 `canonicalize_platform_url` 统一分发入口，`lotteries.py` 与 `discovery.py` 的重复分发逻辑改为复用该入口；模块级 `import httpx` 按项目惯例改为函数内延迟导入。

### 选择器完整性与门禁

- 两侧 `adapter_config.py` 新增 `STRUCTURED_SELECTOR_PLATFORMS = ("bilibili", "weibo", "xiaohongshu")`：三个平台统一要求 followed/liked/reposted 有 click 选择器、commented 为含 input+submit 的结构化组；其余平台维持原宽松判定。
- `lotteries.py` 的 `phase_configured` 改用该集合判定。
- `emit_real_run_gate_notification` 从 Bilibili 专属扩展到所有结构化平台，事件类型改为 `{platform}.real_run_gate.blocked`，标题使用平台 label。

### 账号安全

- `account_calibrator.py`：微博校准通过 `m.weibo.cn/api/config` 验证 `login`/`uid`，未登录即失败；小红书尝试 `web/v2/user/me` 接口，`guest` 状态判定为未认证，接口形态不可用时降级为 Cookie 存在性校验（不阻塞校准，但记录降级原因）。
- `safety.py` 的 `LOGIN_URL_MARKERS` 新增 `passport.weibo.com` 与 `xiaohongshu.com/login`，登录跳转即置为 `login_required` 并停止。
- 两侧 `platforms.py` 中微博/小红书 `adapter_status` 由 `planned` 改为 `calibration_required`。

## 变更文件

- `worker/app/adapters/selector_flow.py`（新增）、`bilibili.py`、`weibo.py`（新增）、`xiaohongshu.py`（新增）、`planned.py`、`registry.py`
- `worker/app/adapter_config.py`、`adapter_probe.py`、`safety.py`、`account_calibrator.py`、`platforms.py`
- `core/app/utils/lottery_targets.py`、`canonicalizer.py`
- `core/app/adapter_config.py`、`platforms.py`、`api/lotteries.py`、`services/discovery.py`
- `core/tests/test_lottery_targets.py`（扩展）、`test_canonicalizer.py`（新增）

## 验证证据

- `python3 -m unittest tests.test_lottery_targets tests.test_canonicalizer tests.test_bilibili_discovery tests.test_bilibili_qr`：27 项全部通过（含 15 项新增微博/小红书用例与 Bilibili 规范化回归用例）。
- 全部改动文件 `py_compile` 通过。
- `task_runner` 对适配器的调用面（`_follow/_like/_comment/_repost`、`REAL_ACTIONS`、`PLATFORM`）与重构后基类完全兼容。

## 已知限制

- 自动发现仅 Bilibili 支持 UP 动态源；微博/小红书目前通过 url_list 源或批量导入录入目标，无 API 自动发现。
- 小红书身份校验接口可能要求签名头，不可用时降级为 Cookie 存在性校验（结果中带 `note` 标记降级原因）。
- 默认探针选择器基于平台通用控件特征，真实选择器仍必须通过探针证据人工复核后保存。
- 运维页的"Bilibili 真实执行准备度"向导未泛化到新平台；门禁证据表、规则计划编辑、策略队列等核心链路为平台通用，已自动覆盖。
- Worker 端适配器与安全逻辑仍无自动化测试（既有缺口）。

## 下一步

1. 用真实微博/小红书账号导入 Cookie 并完成校准，验证身份接口实际可用性。
2. 录入真实微博/小红书抽奖链接，跑通探针 → 选择器复核 → Shadow-run 链路。
3. 评估将 Deploy 页就绪向导泛化为多平台。
4. 评估微博/小红书的合规自动发现源（仅限已登录账号可见的公开内容）。

## 对应 Git 提交

- `6a0a929 Add Weibo and Xiaohongshu lottery adapters`
- 时间线见 [[DPMS_活动时间线]] 2026-06-11 条目。
