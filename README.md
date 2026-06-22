# DPMS

DPMS 是一个多平台抽奖活动管理、账号资产治理与证据化运行时平台。当前主线目标不是脚本化批量执行，而是建立一个可审计、可门禁、可恢复、可扩展的运营控制面。

目标流程：

```text
发现活动 -> 解析目标 -> 评估价值/风险 -> 选择账号 -> 排程执行
-> probe / dry-run / shadow-run -> 安全门禁 -> real-run
-> 证据保存 -> 通知 -> 复盘 -> 策略建议
```

## 当前版本状态

```text
Product Version: 0.3.13
Architecture Stage: S15 / Controlled Runtime Readiness Contract
Runtime Stage: Shadow-run Closed Loop + Migration-Gated Reliability Baseline + Managed Browser Context Pool + Static Preflight Gate + Compose Smoke Gate + Controlled Worker Lifecycle Harness + Controlled Browser Lifecycle Harness + Runtime Readiness Contract
Real-run Status: Gated / Calibration Required
Production Readiness: Not Ready
Primary Platform: Bilibili first, other platforms remain plugin/calibration tracks
```

详见 `VERSION.md`。

## 当前能力边界

- 多平台账号池：支持二维码登录会话和 Cookie 导入，账号状态通过校准流程进入可用状态。
- 活动目标管理：支持目标上传、活动池、规则解析、action plan 审核和 canonical URL 去重。
- 执行分层：支持 dry-run、shadow-run、real-run 门禁；real-run 默认关闭。
- 队列与恢复：使用 Redis Streams consumer group、pending recovery、worker heartbeat、lease、dead-letter 与 outbox 结构。
- 事务一致性：正常 dispatch 使用 DB transaction + outbox row + relay，降低 DB/Redis 双写不一致风险。
- 浏览器生命周期：支持 persistent browser context、TTL、idle eviction、capacity guard、context reaper 和内存归因。
- 运行前检查：包含 runtime preflight、container runtime smoke、controlled worker lifecycle smoke、controlled browser lifecycle smoke、runtime readiness contract。
- 证据与治理：记录 events、audit logs、policy decisions、policy transitions、risk events、evidence files 和 notification logs。

## 安全边界

- `REAL_RUN_ENABLED=false` 是默认安全姿态。
- `DEPLOYMENT_MODE=production` 时，系统会拒绝使用默认 `ADMIN_TOKEN`、默认 `UPDATE_SECRET` 或空 `ENCRYPTION_KEY` 启动。
- real-run 不是默认能力；必须通过账号校准、selector probe、evidence gate、策略门禁、熔断器和二次确认。
- shadow-run 当前属于“认证态观察型运行”：可能打开真实目标页面并读取页面状态，但不执行点击/互动动作。
- 本项目的账号安全目标是合规限速、风险识别、账号隔离，以及在需要人工处理时停止并通知。

## 本地启动

1. 复制环境文件：

```powershell
Copy-Item .env.example .env
```

2. 编辑 `.env`，至少设置数据库、Redis、`ADMIN_TOKEN`、`UPDATE_SECRET`、`ENCRYPTION_KEY` 和通知密钥。开发环境保持：

```text
DEPLOYMENT_MODE=dev
REAL_RUN_ENABLED=false
```

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

## 检查命令

无需启动容器的静态检查：

```powershell
python scripts/runtime_preflight.py
python scripts/runtime_readiness_contract.py
```

Compose 合同检查：

```powershell
python scripts/container_runtime_smoke.py
```

受控生命周期 smoke 默认只 dry-run：

```powershell
python scripts/controlled_worker_lifecycle_smoke.py
python scripts/controlled_browser_lifecycle_smoke.py
```

显式执行受控 smoke 时必须使用独立 project name，并确保 `.env` 已配置：

```powershell
python scripts/controlled_worker_lifecycle_smoke.py --execute --project-name dpms-worker-smoke
python scripts/controlled_browser_lifecycle_smoke.py --execute --project-name dpms-browser-smoke
```

## 测试

无需数据库即可运行纯逻辑单元测试：

```powershell
cd core; python -m unittest discover -s tests
cd ../worker; python -m unittest discover -s tests
```

## 重要安全说明

- 不要提交 `.env`、`browser-profiles/`、`logs/`、`backups/`、`releases/`。
- `browser-profiles/` 可能包含登录态、Cookie、二维码登录会话和账号浏览器数据。
- 不要把生产密钥写入 `.env.example`、文档、截图或 Issue。
- 热更新只允许受管 symlink 布局；当前 compose 开发布局下不会删除 `/app/app` bind mount。

## 关键文档

- `VERSION.md`：当前产品版本、架构阶段、运行阶段、real-run 状态。
- `docs/DPMS_v0.3.12_Controlled_Browser_Lifecycle_Smoke_20260621.md`：受控浏览器生命周期 smoke 说明。
- `docs/DPMS_v0.3.11_Controlled_Worker_Lifecycle_Smoke_20260621.md`：受控 Worker 生命周期 smoke 说明。
- `docs/DPMS_v0.3.10_Container_Runtime_Smoke_20260621.md`：容器运行合同检查说明。
- `docs/DPMS_v0.3.9_Runtime_Preflight_20260621.md`：runtime preflight 说明。
- `docs/version-runtime-note.md`：运行时版本号统一的剩余限制。
- `docs/DPMS_运行时可信度硬化_实施记录_20260614.md`
- `docs/DPMS_总设计方案_v1_20260611.md`

## 当前未合入分支说明

`claude/code-review-ul0pqt` 已作为 Draft PR #22 指向 `main`，但当前 `mergeable=false`。它包含一组历史安全硬化变更，不能直接强合，否则存在冲突或旧代码覆盖当前 main 的风险。应单独做冲突消解和逐项移植。

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
