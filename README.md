# DPMS

DPMS 是一个可脱离 Codex、独立常驻运行的多平台自动抽奖系统。主线目标是完成“自动发现 → 规则判定 → 幂等平台动作 → 状态回读 → 结果留证”的真实闭环；账号治理、门禁和审计用于保障执行，而不能替代执行。

目标流程：

```text
发现活动 -> 解析目标 -> 评估价值/风险 -> 选择账号 -> 排程执行
-> probe / dry-run / shadow-run -> 安全门禁 -> real-run
-> 证据保存 -> 通知 -> 复盘 -> 策略建议
```

## 当前版本状态

```text
Product Version: 0.3.15
Architecture Stage: S16 / Bilibili API Real-run Integration
Runtime Stage: Bilibili API Real-run Path + Adaptive Account Risk Cooldown + Shadow-run Closed Loop + Migration-Gated Reliability Baseline + Managed Browser Context Pool + Static Preflight Gate + Compose Smoke Gate + Controlled Worker Lifecycle Harness + Controlled Browser Lifecycle Harness + Runtime Readiness Contract
Real-run Status: Gated / Bilibili API + Weibo OAuth Adapters Wired
Production Readiness: Not Ready
Platform Boundary: Bilibili and Weibo have gated automatic paths; Xiaohongshu and Douyin are manual-assisted Shadow only
```

详见 `VERSION.md`。

## 当前能力边界

- 多平台账号池：支持二维码登录会话和 Cookie 导入，账号状态通过校准流程进入可用状态。
- 活动目标管理：支持目标上传、活动池、规则解析、action plan 审核和 canonical URL 去重。
- 执行分层：支持 dry-run、shadow-run、real-run 门禁；Bilibili 动态/opus 与 Weibo 状态目标分别接入 API/OAuth real-run 通道，real-run 默认关闭；小红书、抖音保持人工辅助 Shadow。
- 队列与恢复：Bilibili、Weibo、小红书、抖音分别使用独立的标准 Redis Stream/consumer group，并各有版本化 Repair-only 通道 `lottery_repair_tasks:v1:<platform>` / `repair-workers:v1:<platform>`。标准与 Repair 通道拥有独立读取、Outbox 和 pending recovery，但在每个 Worker 进程内同平台共享执行锁；跨平台队列互不争抢。旧 Worker 不认识 Repair stream，新 Worker 也会拒绝标准通道中的 Repair 信封。历史 `lottery_tasks/workers` 只允许标准任务，由 Core 按不可变 Outbox 权威原子 fan-out，Worker 不直接执行旧队列。
- 旧队列 fan-out 的 Redis provenance marker 会覆盖恢复重投形成的 stream/task/message 绑定链；处理中和重试时保留。目标任务终态且当前绑定匹配后，Lua 先 `XACK` 当前消费组；只有该平台 lane 的所有消费组都已越过该消息且其 PEL 都不再持有该精确 ID，才在同一原子操作中 `XDEL` 目标消息并移除该消息的 marker binding，集合为空时才删除 marker key。legacy stream 本身继续保留作迁移 provenance；数据库 Outbox 历史不删除。
- 事务一致性：正常 dispatch 使用 DB transaction + outbox row + relay，降低 DB/Redis 双写不一致风险；四个平台拥有独立且按实例确定性错峰的 Outbox relay，单平台 DB backlog 或慢 Redis 写入不会卡住其他平台，非任务事件保留单独共享 lane。崩溃残留的 `sending` 行也由平台/共享 scope 互斥的 30 秒低频循环分别回收，每个 scope 每次最多 500 行；共享 Core 停机或单平台积压不再阻止兄弟平台回收，也不会让每条 relay lane 每 5 秒重复扫描。
- Redis 连续性安全：Compose 为 Redis 启用 AOF（`appendfsync everysec`）和 `redis-data` 持久卷；每条任务 lane 各有独立 continuity token/state，已投递 Outbox 保存由 Redis `run_id`、全局 token、精确 stream key 与 lane token 组成的 epoch。某条 lane 曾被观测为非空（或在 `XADD` 前已武装）后整体变为空时，只轮换该 lane epoch，并只回拨该 lane 中“已 sent、DB 仍 queued、投递 epoch 已过期”的行；兄弟平台及标准/Repair lane 不重放。选择性 `XDEL` 后 lane 仍非空、或有权限者精确恢复旧 token/state，仍无法自动识别，必须依赖 Redis ACL 与运维审计。Redis/PEL 全量丢失时，各平台有界 DB scanner 仅在精确权威绑定成立时自动恢复 `dry_run`/`shadow_run`；`real_run` 永不自动重放，而是进入 `reconciliation_required`。终态任务和非任务 stream 不重放。`/api/metrics/overview` 同时暴露各 lane `length`、聚合 `task_stream_length(_by_platform)` 与 `stale_running`。运行时 Core/Worker 不再拥有 `XGROUP CREATE`；Core 仅对自身 `notify_events` 与 discovery request stream 保留 `XGROUP DELCONSUMER`，Worker 仅对受治理 Worker stream 保留该命令，两者都只用于 Lua 原子复核后的零 pending 陈旧 consumer 元数据清理。Compose 的一次性随机 bootstrap identity 会在暴露 `health` 身份前，按只读 manifest 幂等预建并验证全部固定 group，然后通过 `ACL LOAD` 永久禁用自身。Redis 入口先修复既有 `/data` 卷权限，再以非 root `redis` 用户运行；容器同时启用只读根文件系统、`no-new-privileges` 和最小启动期 capability。
- 平台队列健康：保留 `workers_online` 和 legacy Outbox 指标以兼容旧监控，同时新增 `workers_online_by_platform`、`task_transport_by_platform`、`task_outbox_undelivered_by_platform` 和 `task_outbox_stale_by_platform`。每个平台的标准/Repair consumer 必须同时具有活跃 Redis 身份和同名新鲜 DB heartbeat；Worker 身份由主机名、PID 与进程启动 nonce 组成并限制在 DB 字段上限内，同主机多进程或 PID 复用后的新进程不能互相冒充。遗留 PEL 不能把已死亡 consumer 伪装为在线。Repair dispatch 的 Worker lane-health v2 区分最近一次 `XREADGROUP` 与循环容量等待进展：通常要求 45 秒内成功读取；当该精确 lane 已经成功读取并占满全部 32 个有界 in-flight 时，也可由每 10 秒更新、零失败且不含任务数据的 `capacity_wait` 证据证明循环仍存活。未满、进展过期、Redis 失败、监督退出或 Redis/DB consumer 身份不相交仍 fail-closed；Core 只把 v1 近期读取证据作为停机升级后的历史兼容观测，不据此授权新旧进程混跑。Outbox 按平台独立查询 `pending/sending`，超过 120 秒仍未投递即只阻断该平台 readiness；lane Redis/SQL、heartbeat 和平台 readiness 查询均有 5 秒 deadline，失败或超时返回局部 unavailable，不借用全局 Worker 或兄弟平台健康状态。
- Consumer group 治理：任务、Probe、Calibration、四个平台 discovery request、通知与登录 stream 的现役 group 都由有限 topology 声明；启动 preflight 只用有界 `XINFO GROUPS` 验证该进程拥有的固定 group 已由 bootstrap 建立，不会在运行时创建 group。指标暴露缺失、意外、陈旧 group、阻塞 `XDEL` 的 backlog 以及跨重启累积的陈旧 consumer 条目，Dashboard 每 5 秒轮询并对 retention alert 显示红色阻塞告警。一次顶层 metrics 请求对额外 `XINFO CONSUMERS` 共享最多 64 次、并发 8 的硬预算，等待并发槽也受同一 5 秒 deadline 约束；预算耗尽会返回 unavailable/retention alert，不再继续无界探测。运行时 Core/Worker 身份没有 `XGROUP DESTROY` 权限。确需退役已从 topology 移除且消费者已经停机的历史 group 时，只能使用 `scripts/retire_redis_consumer_group.py` 的短期 intent + 精确 allowlist + 独立 `group-admin` URL + append-only audit 流程；脚本默认 dry-run，现役 topology group 永远拒绝，静默窗口硬下限为 1 小时、运维建议使用 86400 秒，并在 group/consumer inventory 超界时 fail-closed。
- 运行故障收敛：`core-api` 只拥有共享控制面循环；四个 `core-<platform>-runner` 分别拥有本平台 discovery、recovery、Outbox、scheduler 与 manual-discovery lane。稳定服务名 `worker` 是 control Worker，只运行 login、task-outbox、维护，以及在 `LEGACY_CONTROL_STREAM_DRAIN_ENABLED=true` 时运行两条 legacy control fan-out；四个 `worker-<platform>` 只消费本平台 task、Probe 与 Calibration lane。每个进程用 `FIRST_COMPLETED` 监督自身关键循环，任一循环异常、取消或意外正常返回都会取消同进程兄弟循环并退出；单个平台模块预检失败只阻止该平台 runner/Worker，不阻止其他平台或控制面启动。
- 浏览器生命周期：支持 persistent browser context、TTL、idle eviction、capacity guard、context reaper 和内存归因。角色拆分后每个 Worker 在空闲时都不会启动 Playwright 或 Chromium；首次实际浏览器请求才按需初始化。`WORKER_MAX_BROWSERS` 与 `WORKER_MAX_PERSISTENT_CONTEXTS` 是单进程上限，不是五个 Worker 的全局上限；容量规划必须按 control 加四个平台的同时活跃进程求和。
- 运行前检查：包含 runtime preflight、container runtime smoke、controlled worker lifecycle smoke、controlled browser lifecycle smoke、runtime readiness contract。
- 证据与治理：记录 events、audit logs、policy decisions、policy transitions、risk events、evidence files 和 notification logs。

## 安全边界

- `REAL_RUN_ENABLED=false` 是默认安全姿态。
- `DEPLOYMENT_MODE=production` 时，系统会拒绝使用默认 `ADMIN_TOKEN`、默认 `UPDATE_SECRET` 或空 `ENCRYPTION_KEY` 启动。
- real-run 不是默认能力；必须通过账号校准、API/selector adapter readiness、evidence gate、策略门禁、熔断器和二次确认。
- shadow-run 当前属于“认证态观察型运行”：可能打开真实目标页面并读取页面状态，但不执行点击/互动动作。
- 本项目的账号安全目标是合规限速、风险识别、账号隔离，以及在需要人工处理时停止并通知。

## 本地启动

1. 复制环境文件：

```powershell
Copy-Item .env.example .env
Copy-Item .env.mysql-admin.example .env.mysql-admin
Copy-Item .env.redis-admin.example .env.redis-admin
```

2. 编辑 `.env`，至少设置数据库、Redis、`ADMIN_TOKEN`、`UPDATE_SECRET`、`ENCRYPTION_KEY` 和通知密钥。开发环境保持：

```text
DEPLOYMENT_MODE=dev
REAL_RUN_ENABLED=false
LEGACY_TASK_STREAM_DRAIN_ENABLED=true
LEGACY_CONTROL_STREAM_DRAIN_ENABLED=true
REDIS_MAX_CONNECTIONS=64
REDIS_SOCKET_TIMEOUT_SECONDS=15
REDIS_CONNECT_TIMEOUT_SECONDS=5
REDIS_CORE_PASSWORD=dpms-core-local-only-change-me-2026
REDIS_WORKER_PASSWORD=dpms-worker-local-only-change-me-2026
REDIS_HEALTH_PASSWORD=dpms-health-local-only-change-me-2026
```

Production also requires the eight scoped `REDIS_CORE_<PLATFORM>_PASSWORD`
and `REDIS_WORKER_<PLATFORM>_PASSWORD` values and four
`ENCRYPTION_KEY_<PLATFORM>` values. Keep
`DPMS_MYSQL_PLATFORM_DATABASE_MODE=shared` only for local/rolling compatibility
until the database-routing gate in `docs/DPMS_PLATFORM_SECURITY_RELEASE.md`
is cleared; production remains blocked because provisioning isolated schemas
alone does not route Core API data to platform Workers.

以上共享 Redis 控制面运行时密码仅供本地开发；平台 Core/Worker 还需分别设置
八个 scoped Redis 运行时密码；`group-admin` 密码只放在
`.env.redis-admin`，MySQL root 密码只放在 `.env.mysql-admin`。Core、平台 Core
runner 和 Worker 都使用显式环境白名单，不会继承 root、migration、health、
group-admin 或其他 Redis 角色的秘密。生产模式会拒绝内置值、空值、短密码和角色复用。
密码通过独立环境变量传入，不要拼接到 `REDIS_URL`。

3. 启动服务：

```powershell
$composeArgs = @(
  '--env-file', '.env',
  '--env-file', '.env.mysql-admin',
  '--env-file', '.env.redis-admin'
)

# 运行服务不再自动执行 DDL。新卷和既有卷都显式、幂等地准备角色并迁移。
docker compose @composeArgs up -d --build --wait mysql redis
docker compose @composeArgs exec -T mysql /usr/local/bin/dpms-mysql-provision-roles
docker compose @composeArgs --profile migration run --rm --build core-migrate
# Keep shared mode for this release. The four platform migration commands are
# deferred until the API database router and continuity checks are delivered.

docker compose @composeArgs up -d --build --wait core-api `
  core-bilibili-runner core-weibo-runner `
  core-xiaohongshu-runner core-douyin-runner

# 上一条命令必须成功并确认四个 runner 均 healthy 后，才启动 Worker。
docker compose @composeArgs up -d --build --wait worker `
  worker-bilibili worker-weibo worker-xiaohongshu worker-douyin

docker compose @composeArgs up -d --build nginx
```

### 生产停机升级与回滚

当前版本同时改变 MySQL Schema、Redis ACL、固定 consumer-group topology、任务/Repair
信封和控制队列协议，**不支持新旧 Core/Worker 混跑**。发布必须使用停机切换：

本版本还移除了 GET/HEAD 的 `?admin_token=` 查询参数认证，这是有意的安全破坏性变更。
停机前必须把 EventSource、图片/证据直链和运维脚本升级为
`Authorization: Bearer <token>`；新前端已使用 header-authenticated fetch 读取
SSE/blob。发布时应关闭或强制刷新所有旧长驻页面，旧客户端升级前收到 `401` 属预期，
不得为兼容而恢复会进入 URL、代理日志、浏览器历史或 Referer 的 query token。

1. 保持两个 legacy drain 开关为 `true`，暂停新派发并完成仍在运行的 Repair/real-run
   对账；记录 Redis PEL/lag、Outbox backlog、数据库备份和 Redis AOF/卷快照。
2. 使用旧版本的部署清单停止全部旧 Core、全部旧 Worker 和旧 Nginx。不得保留一个旧
   Worker，也不得先启动任何新应用进程。
3. 使用新清单先启动 MySQL 与 Redis；对既有 MySQL 卷显式执行
   `dpms-mysql-provision-roles`。Redis 入口会用一次性 bootstrap 身份建立并验证固定
   group，随后禁用该身份。两步都成功后才继续。
4. 运行一次性 `core-migrate`，确认完整 migration ledger、生产 Schema verifier、
   MySQL runtime-role 拒绝 DDL 的契约和 Redis ACL/topology 验证全部通过。
   当前 Worker 镜像要求数据库账本与随镜像发布的 `0001`—`0028` 迁移文件集合及
   checksum 完全一致；`0024` 持久化 Profile 清理 intent，`0025` 建立跨进程
   persistent-context lease，`0026` 为小红书只读目标追踪建立独立来源、候选审核和
   来源命中投影，`0027` 将来源命中证据改为限制删除，`0028` 为最小权限运行时校验器
   提供只读触发器元数据；候选只有显式接受后才会创建或复用
   `lotteries`。缺失、漂移或额外
   版本都会阻止 Worker 启动。
   首次启动新 storage-init 拓扑时，会一次性递归把各平台既有 Profile/Evidence
   调整为 Worker `1000`、共享只读组 `2000`，并在各自根目录写入
   `.dpms-storage-permissions-v1` 标记；这会改变 Linux bind 目录下既有文件的
   UID/GID，历史文件较多时应先在副本测量并预留停机时间。后续启动只校验和修复固定
   根目录，不再周期性遍历全部历史文件；删除标记等同于显式请求重新执行权限迁移。
5. 先启动 `core-api`，再启动四个 `core-<platform>-runner`；逐个确认健康后，启动
   control `worker` 与四个 `worker-<platform>`，最后启动 Nginx。一个平台 runner 或
   Worker 失败只暂停该平台，不应以兼容 monolith 替代。

回滚同样必须停机：先停止全部新 Nginx/Core/Worker，保留数据库、AOF、Stream、PEL 和
审计记录，再恢复经过验证、彼此兼容的旧应用、Schema、Redis ACL 与 topology 组合。
不得在新进程仍运行时恢复旧 ACL/协议，也不得通过 `XDEL`、`XGROUP DESTROY` 或盲目执行
down migration 来“清空现场”。若备份无法证明与旧应用兼容，应维持停机并人工对账，
不能用新旧混跑缩短故障窗口。

4. 打开控制台：

```text
http://localhost:8080
```

Compose 默认仅在本机回环地址监听，可通过 `.env` 中的 `DPMS_HTTP_PORT`
修改宿主机端口。需要从其他主机访问时，应显式调整 `docker-compose.yml`
的监听地址，并同时配置防火墙、反向代理和访问控制。

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
$env:PYTHONPATH="$PWD;$PWD\core"; python -m unittest discover -s core/tests
$env:PYTHONPATH="$PWD;$PWD\worker"; python -m unittest discover -s worker/tests
cd frontend; node --test; cd ..
python scripts/platform_contract_parity.py --strict-target-kinds
```

Core 与 Worker 的队列拓扑唯一来源是 `shared/task_streams.py`；两端的
`app/task_streams.py` 只保留兼容 re-export。Compose 镜像会复制 `shared/`，修改该
目录后需要重新构建 `core-api` 与 `worker` 镜像，而不能只依赖 `app/` 的 bind mount。

## 重要安全说明

- 不要提交 `.env`、`browser-profiles/`、`logs/`、`backups/`、`releases/`。
- `browser-profiles/` 可能包含登录态、Cookie、二维码登录会话和账号浏览器数据。
- 不要把生产密钥写入 `.env.example`、文档、截图或 Issue。
- 热更新只允许受管 symlink 布局；当前 compose 开发布局下不会删除 `/app/app` bind mount。

## 关键文档

- `docs/DPMS_平台模块独立架构_20260723.md`：四个平台业务隔离、共享基础设施边界与验收条件。
- `VERSION.md`：当前产品版本、架构阶段、运行阶段、real-run 状态。
- `docs/DPMS_v0.3.12_Controlled_Browser_Lifecycle_Smoke_20260621.md`：受控浏览器生命周期 smoke 说明。
- `docs/DPMS_v0.3.11_Controlled_Worker_Lifecycle_Smoke_20260621.md`：受控 Worker 生命周期 smoke 说明。
- `docs/DPMS_v0.3.10_Container_Runtime_Smoke_20260621.md`：容器运行合同检查说明。
- `docs/DPMS_v0.3.9_Runtime_Preflight_20260621.md`：runtime preflight 说明。
- `docs/version-runtime-note.md`：运行时版本号统一的剩余限制。
- `docs/DPMS_运行时可信度硬化_实施记录_20260614.md`
- `docs/DPMS_BilibiliApiRealRun_实施记录_20260624.md`
- `docs/DPMS_BilibiliRiskAwareDiscovery_实施记录_20260624.md`
- `docs/DPMS_BilibiliActionLedger_实施记录_20260625.md`
- `docs/DPMS_BilibiliAdaptiveRiskCooldown_实施记录_20260625.md`
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

## Probe / Account Calibration 队列迁移

Adapter Probe 与 Account Calibration 不再共用单一实时队列。四个平台分别使用：

- `adapter_probe_requests:<platform>` / `adapter-probers:<platform>`
- `account_calibration_requests:<platform>` / `account-calibrators:<platform>`

新校准请求把账号状态或凭据变更、`account_calibrations` 行和
`outbox_events` 行放在同一数据库事务中。Outbox 按精确 stream key 独立 relay；
Redis lane 丢失时只重放该平台仍为 `queued` 且投递 epoch 已过期的请求。Worker
按平台独立预取、pending refresh、stale recovery 和 DB orphan scan，因此一个平台
占满自己的最多 32 个本地 inflight 不会遮住兄弟平台的下一条请求。
平台 Probe/Calibration 的终态或无关消息使用 all-groups-confirmed Lua 原子执行
`XACK`/`XDEL`：只有所有实时消费组都越过该精确消息且 PEL 不再持有它时才删除；
legacy fan-out 消息继续只 `XACK`，保留为升级期 provenance。

Worker 常驻 Redis 连接预算约为 20（8 task reader、4 probe reader、4 calibration
reader、2 legacy control fan-out、login 和 pubsub），恢复/刷新/ACK 与执行写入还会
产生突发连接。Core 与 Worker 因此都要求 `REDIS_MAX_CONNECTIONS>=64`；显式配置
更小值会在启动时 fail-fast。停机切换前应先把旧环境中的 32 提升到至少 64。

控制队列也属于本版本不可混跑的协议切换，不能用旧的“先上 Worker、再上 Core”
滚动步骤。按“生产停机升级与回滚”执行：

1. 停机前保持 `LEGACY_CONTROL_STREAM_DRAIN_ENABLED=true`，暂停新请求并记录
   legacy/per-platform PEL、lag、Outbox 与 heartbeat 快照。
2. 停止全部旧 Core 和旧 Worker 后，启动新 Redis bootstrap、应用完整迁移并验证
   topology；旧 `adapter-probers` / `account-calibrators` group 只保留作 provenance
   与回滚审计，不允许旧 Worker 与新进程同时消费。
3. 依次启动新 Core、control Worker 和四个平台 Worker。观察
   `/api/metrics/overview` 中 Probe 与 Account Calibration 的 per-platform
   length、pending、lag、同名 Worker heartbeat 与 Outbox backlog；同时确认独立
   legacy fan-out group
   `adapter-probers:legacy-fanout` /
   `account-calibrators:legacy-fanout` 的 Pending 和 lag 均为 0，legacy stream 的
   `pending/sending` Outbox 也为 0。
4. 只有上述 Redis 与数据库观测全部可用且持续归零后，才把
   `LEGACY_CONTROL_STREAM_DRAIN_ENABLED` 设为 `false`。关闭开关只停止兼容
   fan-out/relay，不删除旧 stream、旧 group 或历史 marker；物理清理需另走受控
   运维流程。

## Redis Consumer Group 显式退役

先从 `shared/redis_consumer_groups.py` 的现役 topology 移除目标 group，部署该版本并
停止其全部 consumer；脚本会在检查 intent 或 allowlist 的静默声明之前拒绝任何仍在
现役 topology 中的 group。随后分别准备短期 intent 与精确 allowlist，例如：

```json
{
  "version": 1,
  "intent_id": "00000000-0000-4000-8000-000000000001",
  "stream": "lottery_tasks:bilibili",
  "group": "retired-workers:v0:bilibili",
  "actor": "operator@example.invalid",
  "ticket": "OPS-2042",
  "reason": "Retire the drained historical consumer group.",
  "created_at": "2026-07-24T00:00:00+00:00",
  "expires_at": "2026-07-24T01:00:00+00:00",
  "inactive_for_seconds": 86400,
  "break_glass_oversized_inventory": false
}
```

```json
{
  "version": 1,
  "allowed": [
    {
      "intent_id": "00000000-0000-4000-8000-000000000001",
      "stream": "lottery_tasks:bilibili",
      "group": "retired-workers:v0:bilibili",
      "actor": "operator@example.invalid",
      "ticket": "OPS-2042",
      "reason": "Retire the drained historical consumer group.",
      "created_at": "2026-07-24T00:00:00+00:00",
      "expires_at": "2026-07-24T01:00:00+00:00",
      "inactive_for_seconds": 86400,
      "break_glass_oversized_inventory": false
    }
  ]
}
```

先执行默认 dry-run；确认 `pending=0`、`lag=0`、没有活跃 consumer 且 inventory
未超界后，再显式执行并写 append-only JSONL 审计：

```powershell
python scripts/retire_redis_consumer_group.py --intent-file .\intent.json --allowlist-file .\allowlist.json
python scripts/retire_redis_consumer_group.py --intent-file .\intent.json --allowlist-file .\allowlist.json --audit-log .\redis-group-retirement.jsonl --execute
```

`REDIS_GROUP_ADMIN_URL` 只能通过独立的短期管理员环境注入；不要写进上述 JSON、仓库、
命令历史或运行时 Core/Worker 配置。脚本的硬下限为 3600 秒，推荐
`inactive_for_seconds=86400`；group 超过 32 个或目标 group 报告超过 256 个 consumer
时拒绝继续。脚本不会执行 `XGROUP DELCONSUMER`，也不会自动清理带 pending 的旧身份。

Compose 的 `group-admin` 密码不得加入根 `.env`；应用容器虽然已改用显式环境白名单，
仍应从配置源上保持管理员秘密分离。
复制 `.env.redis-admin.example` 为被 Git 忽略的 `.env.redis-admin`，替换默认值，并仅
通过三个 `--env-file` 参数（`.env`、`.env.mysql-admin`、`.env.redis-admin`）做
Compose 插值。`REDIS_GROUP_ADMIN_URL` 只在执行退役命令的受控进程内由该密码临时构造，
用后立即清除；Compose 不会把密码或 URL 注入 Core/Worker。

Redis 队列身份除已有命令外，必须允许 `INFO`、`EVAL`、`GET`、`SET`、`DEL`、
`EXPIRE`、`XLEN`、`XADD`、`XACK`、`XDEL`、`XREADGROUP`、`XCLAIM`、
`XPENDING`、`XINFO GROUPS`、`XINFO CONSUMERS`，并把 key scope 限制到上述八条
平台 stream、两个 legacy stream，以及
`dpms:task-stream:continuity:v1`、
`dpms:task-stream:lane-continuity:v1:*`、
`dpms:task-stream:lane-state:v1:*` 和 legacy fan-out marker。继续显式禁止
`FLUSHDB`、`FLUSHALL`、`XGROUP DESTROY` 及对 continuity key 的非协议覆写。
