# DPMS Bilibili 安全门禁实施记录 2026-06-08

## 背景

本节点用于推进 Bilibili 抽奖执行链从“可配置执行”升级为“证据驱动执行”。目标不是绕过平台风控，而是在自动化前增加可审计、可回滚、低频和遇风险即停止的门禁，避免未校准选择器、缺失影子验证或账号近期风险导致真实任务误执行。

## 本次改动

- Bilibili 内置适配器默认不再声明 `REAL_ACTIONS=True`，必须由运行时选择器配置完成后才允许真实动作。
- Bilibili 评论阶段配置必须同时包含 `input` 和 `submit` 选择器，避免仅识别到评论框就误判为可执行。
- 关注、点赞、转发阶段支持 `click`、`selectors`、`buttons` 配置格式；评论阶段支持 `done`/`success` 验证选择器。
- 任务断点续跑从“重复上一个已完成阶段”改为“从下一个阶段继续”，避免重复点赞、重复关注、重复转发。
- 账号安全节流从 5 分钟 5 次收紧为 10 分钟 2 次，单账号每日任务上限从 30 次收紧为 8 次。
- 页面风险检测改为明确验证码、频率、账号异常、登录页 URL 等信号，减少普通页面文案误触发。
- Core API、Worker、探针摘要统一 Bilibili 选择器完整性判断，避免前端显示可执行但 Worker 拒绝的假就绪。
- real-run 派发新增证据门禁：同一目标必须有 24 小时内完整适配器探针、24 小时内成功 shadow-run、实际账号 24 小时内无风险事件。

## 验证记录

- `python -m compileall -q core\app worker\app` 通过。
- `docker compose up -d --build core-api worker` 通过，Core API 与 Worker 均进入 `healthy`。
- Core 容器断言通过：完整 Bilibili 配置返回可执行，不完整评论配置返回不可执行。
- Worker 容器断言通过：`BilibiliAdapter(selector_config=完整配置).REAL_ACTIONS=True`，空配置与不完整评论配置均为 `False`。
- 临时开启 real-run 与临时完整选择器后，对测试目标派发 `real_run` 返回 `409 Conflict`，未进入任务队列。
- 测试后已恢复：`real_run_enabled=false`，Bilibili 运行时选择器配置为空。

## 当前状态

- Bilibili 当前具备账号 Cookie/二维码登录、dry-run、shadow-run、探针与安全门禁能力。
- Bilibili real-run 仍处于受控阻断状态，原因是当前环境没有 24 小时内完整探针与同目标 shadow-run 证据。
- 下一步应在用户提供低风险 Bilibili 活动目标后，先执行探针与 shadow-run，确认 4 个阶段选择器完整，再考虑是否开启受控 real-run。

## 风险边界

- 本项目不实现验证码绕过、反检测规避或平台风控对抗。
- 账号安全策略以“少动作、可观察、遇风险停止、证据先行”为原则。
- “无风险”无法由任何自动化系统承诺；当前实现目标是将风险降到可控、可审计、可回滚范围内。
