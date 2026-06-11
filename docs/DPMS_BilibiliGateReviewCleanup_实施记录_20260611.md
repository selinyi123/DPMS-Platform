# DPMS Bilibili 安全门禁批次代码审查清理实施记录

## 背景与问题

对 Bilibili Real-run 安全门禁加固批次（`3174a97~1..HEAD`，含官方二维码登录、动态发现规则计划、目标校验、real-run 证据门禁等共 16 个提交）进行代码审查：新增的 12 项单元测试全部通过，所有改动文件编译通过，未发现安全合规层面的阻塞问题。审查发现 4 项代码质量层面的小问题：

1. `frontend/src/pages/Lotteries.jsx` 中 `realActionReady` 函数定义后从未被调用。
2. `core/app/api/lotteries.py` 重复定义了 `selector_values`，且 `phase_configured` 中重复内联了 `core/app/adapter_config.py` 已提供的 `click_selectors` 拆装逻辑——该文件已从 `app.adapter_config` 导入 `selector_config_complete`，但未复用其余 selector 工具函数。
3. `core/app/api/notify.py` 的 `looks_like_placeholder_secret` 未识别 `GENERIC_WEBHOOK_URL` 的示例值 `https://example.com/dpms/webhook`：当"使用最小模板"在某些已配置渠道组合下恰好返回该示例值，且用户未编辑直接保存时，会被当作真实 Webhook 密钥静默写入并标记该通道为"已配置"。
4. `frontend/src/uiContext.jsx` 中 zh/en 两份 `bilibiliSteps` 翻译块末尾各有一处 `},    },` 格式问题。

## 目标与安全边界

仅修复上述代码质量问题，不改变任何安全门禁、风控阈值、real-run 判定逻辑或选择器完整性判定结果；不引入新依赖或架构调整；延续 README 中"反爬与账号安全仅用于合规限速、风险识别、账号隔离和触发验证码/审核时停止并通知"的边界。

## 实现内容

- 删除 `Lotteries.jsx` 中未使用的 `realActionReady`。
- `core/app/api/lotteries.py` 改为从 `app.adapter_config` 导入 `click_selectors`、`selector_values`，删除本地重复定义的 `selector_values`，`phase_configured` 的非评论阶段分支改用 `click_selectors`（行为与原内联逻辑一致）。
- `looks_like_placeholder_secret` 增加 `example.com` 关键字判定，避免示例 Webhook URL 被当作真实密钥保存。
- 修复 `uiContext.jsx` 中 zh/en `bilibiliSteps` 块末尾的 `},    },` 格式问题，拆分为两行独立的闭合括号。

## 变更文件

- `core/app/api/lotteries.py`
- `core/app/api/notify.py`
- `frontend/src/pages/Lotteries.jsx`
- `frontend/src/uiContext.jsx`
- `scripts/sync_obsidian.ps1`（新增本记录的同步映射）

## 验证证据

- `python3 -m py_compile app/api/lotteries.py app/api/notify.py app/adapter_config.py`：通过。
- `python3 -m unittest tests.test_bilibili_discovery tests.test_bilibili_qr tests.test_lottery_targets`：12 项全部通过。
- 全仓库搜索确认 `realActionReady`、`core/app/api/lotteries.py` 中旧版本地 `selector_values` 定义均无残留引用。

## 已知限制

- `worker/app/adapter_config.py` 与 `core/app/adapter_config.py` 中 selector 完整性逻辑仍然重复；两者分属独立部署的服务（worker / core），合并需引入共享包并同步调整两侧 Dockerfile 与依赖，本次未处理。
- Worker 端（`task_runner.py` 的阶段执行与断点续跑、`safety.py` 的风险检测）仍缺少自动化测试，属于既有缺口，本次未新增覆盖。

## 下一步

- 评估引入共享 Python 包（例如 `shared/adapter_config`）以消除 worker/core 间的 selector 逻辑重复。
- 为 worker 端阶段执行、断点续跑与风险检测补充单元测试。
- 按 [[DPMS_项目主页]] "当前下一步" 推进 Bilibili 真实账号扫码校准与目标录入。

## 对应 Git 提交

- 本记录对应提交：`Clean up Bilibili gate review follow-ups`（代码清理 + 本文档 + 同步脚本映射）。
- 时间线记录见 [[DPMS_活动时间线]] 2026-06-11 条目。
