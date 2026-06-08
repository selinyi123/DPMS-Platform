# DPMS_NotificationSecretBundle_实施记录_v1.0_20260608

## 背景

DPMS 需要多维度通知能力承接账号风险、登录校准、任务结果、中奖提醒和生产异常告警。此前运维页只能逐通道填写通知密钥，用户在配置 ServerChan、飞书、Webhook、Telegram 时缺少统一的粘贴入口和明确反馈。

## 目标

- 支持在运维通知页直接粘贴 `.env` 样式的多行通知密钥。
- 支持一次保存多个通知通道的密钥。
- 保存后 API 不回显密钥原文。
- 避免将 `<token>`、`...`、`changeme` 等模板占位符误判为有效生产配置。
- 保留单通道保存和清空能力。

## 改动内容

### 后端

新增 `PUT /api/notify/secrets`：

- 请求体：`{"content": "SERVERCHAN_KEY=...\nFEISHU_WEBHOOK=..."}`
- 支持字段：
  - `SERVERCHAN_KEY`
  - `FEISHU_WEBHOOK`
  - `GENERIC_WEBHOOK_URL`
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
- 自动忽略不支持的 key。
- 自动忽略空值和模板占位符。
- 密钥通过现有 `cookie_vault` 加密后写入 `notification_secrets`。
- 记录审计事件 `notification_secret.bundle_save`。
- 写入事件流 `NotificationSecretBundleSaved`。

### 前端

运维通知页新增：

- **批量粘贴通知 .env** 文本框。
- **批量保存密钥** 按钮。
- **使用最小模板** 按钮。
- 保存成功后刷新通知健康状态、配置说明和日志列表。
- 保存失败时通过 Toast 和页面提示显示错误。

### 安全保护

以下值不会被保存为有效密钥：

- `...`
- `changeme`
- `change-me`
- `your-key`
- `your-secret`
- `<serverchan-send-key>`
- `<token>`
- `<bot-token>`
- `<chat-id>`

## 验证记录

验证时间：2026-06-08

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| Python 编译 | 通过 | `python -m compileall -q core\app worker\app` |
| 前端构建 | 通过 | `npm run build` |
| Docker 刷新 | 通过 | `docker compose up -d --build core-api nginx` |
| 批量 API 保存 | 通过 | 临时 Webhook + Telegram 配置可一次保存 |
| 占位符保护 | 通过 | `SERVERCHAN_KEY=<serverchan-send-key>` 返回 400 |
| 测试密钥清理 | 通过 | Webhook 与 Telegram 测试密钥已清空 |
| 浏览器 UI | 通过 | 运维通知页显示批量粘贴区和保存按钮 |
| 浏览器控制台 | 通过 | 0 error |
| 运维页 API | 通过 | `/api/notify/channels`、`/api/notify/status`、`/api/notify/config-guide`、`/api/notify/logs` 均返回 200 |

## 当前状态

- 当前本机环境已有 1 个通知通道可用：ServerChan。
- Feishu、Webhook、Telegram 仍未配置真实密钥。
- 生产告警具备最小可用通知通道，但多通道冗余仍需继续配置。

## 后续建议

1. 为通知测试增加发送后轮询，让用户能在页面上看到最终 `sent/failed` 结果。
2. 将账号风险、Bilibili real-run gate 阻断、中奖结果绑定到统一通知策略。
3. 增加通知通道优先级和失败降级策略，例如 ServerChan 失败后自动尝试 Webhook 或 Telegram。
