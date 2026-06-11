# DPMS

DPMS 是一个多平台抽奖自动化与账号资产管理运行时，目标流程是：

```text
发现活动 -> 解析目标 -> 评估价值/风险 -> 选择账号 -> 排程执行
-> probe / dry-run / shadow-run -> 安全门禁 -> real-run
-> 证据保存 -> 通知 -> 复盘 -> 策略建议
```

## 当前能力

- 多平台账号池：支持二维码登录会话和原始 Cookie 粘贴导入。
- 工作流：支持目标上传、活动池、任务派发、dry-run、real-run 门禁。
- 账号安全：支持管理员令牌、危险操作确认、熔断器、账号状态机、代理隔离入口。
- 通知：支持 ServerChan、飞书、Webhook、Telegram 的运行时密钥配置。
- 前端：支持中文/英文、浅色/深色/跟随系统主题、操作反馈和事件时间线。
- V4 Event Store：记录账号、任务、通知、风险、Worker、探针和登录事件。

## 本地启动

1. 复制环境文件：

```powershell
Copy-Item .env.example .env
```

2. 编辑 `.env`，至少设置数据库、Redis、`ADMIN_TOKEN` 和通知密钥。

3. 启动服务：

```powershell
docker compose up -d --build
```

4. 打开控制台：

```text
http://localhost
```

## 前端开发

```powershell
cd frontend
npm install
npm run build
```

构建产物输出到 `dashboard/dist`，供 Nginx 容器服务。前端 API 路径默认是 `/api`，如需部署到不同网关路径，可在构建前设置 `VITE_API_BASE`；请求超时默认 `15000ms`，可通过 `VITE_API_TIMEOUT_MS` 调整。

## 重要安全说明

- 不要提交 `.env`、`browser-profiles/`、`logs/`、`backups/`、`releases/`。
- `browser-profiles/` 可能包含登录态、Cookie、二维码登录会话和账号浏览器数据。
- “反爬与账号安全”在本项目中仅指合规限速、风险识别、账号隔离和触发验证码/审核时停止并通知，不用于绕过验证码或规避平台保护机制。

## 关键文档

- `docs/DPMS_总设计方案_v1_20260611.md`（后续开发总设计方案，分阶段计划与验收标准）
- `docs/DPMS_能力演化路线图_v4-v9_20260605.md`
- `docs/DPMS_EventStore_V4_实施记录_20260605.md`
- `docs/DPMS_最终搭建审阅与漏洞清单_20260602.md`

## 测试

无需数据库即可运行纯逻辑单元测试：

```powershell
cd core; python -m unittest discover -s tests
cd ../worker; python -m unittest discover -s tests
```

## Obsidian 活动记录

项目活动记录同步到当前 Obsidian Vault 的 `DPMS-Platform` 目录。

同步仓库文档、导航和历史恢复资料：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sync_obsidian.ps1
```

仅执行 SHA-256 和 Wiki 链接完整性校验：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sync_obsidian.ps1 -VerifyOnly
```

每个关键里程碑需要同时更新 `docs/` 实施记录、Obsidian 活动时间线和 GitHub 提交。
