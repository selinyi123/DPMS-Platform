# DPMS Bilibili Real-Run 安全任务治理（2026-07-14）

## 目标与不可妥协条件

下一次真实 Bilibili 运行只有在以下条件同时成立时才允许派发：

1. 原始规则完整保存且可解析；不能用简化摘要覆盖来源规则。
2. 每个动作的目标、精确内容和媒体要求均可表达，并由操作者审核同一份不可变 payload。
3. 目标账号、目标动态、执行路径、适配器配置、Action Plan 与 Probe/Shadow 证据版本一致。
4. 每次外部动作前重新检查全局开关、熔断器、账号租约、目标身份、风险状态和任务所有权。
5. 外部结果不确定时不得自动重放；必须保留锁、隔离账号/平台并进入显式对账。
6. 证据文件、事件和数据库引用可验证、可追踪，存储能力不足时 fail closed。

当前结论：以上条件尚未全部实现，真实运行继续禁止。

## 分块职责与当前状态

| 工作流 | 主要目录/模块 | 已完成的安全底线 | 未完成的验收项 | 状态 |
| --- | --- | --- | --- | --- |
| 规则与审核 | `core/app/services/lottery_rules.py`、`core/app/api/lotteries.py`、`frontend/src/pages/Lotteries.jsx` | ASUS 规则能识别四个基础动作及话题、@、媒体、翻译等不支持要求；复杂原文不能被编辑器降级覆盖 | Action Plan v2；动作级精确内容、媒体、话题、@对象、payload hash/version；不可伪造的完整正文来源证明；最终内容预览和审核绑定 | 授权阻塞 |
| 资格与账号治理 | `worker/app/safety.py`、`worker/app/task_runner.py`、账号 API | real-run 前检查风险和账号状态；未知结果会冷却账号；24 小时有效风险不会再被最近 50 条短冷却事件挤出 | 统一 `account_operation_lease`；Probe/Shadow/登录/校准/real-run 互斥与 fencing；限额检查和 claim 原子化 | 授权阻塞 |
| Probe/Shadow | `worker/app/adapter_probe.py`、`worker/app/task_runner.py`、`core/app/services/real_run_readiness.py` | 通用浏览器可见性证据不会冒充真实成功；Bilibili API 与 selector 路径缺证据时硬阻断；Probe 截图在私有共享证据卷落地前关闭 | `execution_path_id/version`、config/plan/target/account hash 绑定；Bilibili API 只读预检；selector 目标级 readback | 授权阻塞 |
| 平台执行 | `worker/app/bilibili/**`、`worker/app/adapters/selector_flow.py` | 精确动态 ID、转发包装动态、逐点击门禁、目标 URL 复核、未知结果隔离；未来平台未显式注册执行路径时 Worker 硬阻断 | 执行器只接受已审核动作 payload；评论/转发精确内容；远端状态 readback | 授权阻塞 |
| 幂等与恢复 | `core/app/services/outbox.py`、`core/app/services/recovery_daemon.py`、`worker/app/task_runner.py` | 派发去重、claim fencing、动作 ledger 精确续跑、重试计入远端尝试上限、真实运行租约失效不自动重放、锁与熔断保留 | durable pre-action intent、generation token、对账记录/API/UI、可证明的安全续跑 | 授权阻塞 |
| 证据与可观测性 | `worker/app/evidence_storage.py`、`worker/app/task_runner.py`、`core/app/services/real_run_readiness.py` | 安全 openat、O_EXCL、inode 绑定、文件/目录 fsync、取消清理、事务化登记、批量查询与预算告警 | Core/Worker 共享私有证据卷；预算耗尽独立状态；长期统一两套目录原语 | 拓扑/协议阻塞 |
| 发现与规则摄取 | `core/app/services/discovery.py` | 每轮/source/关键词/候选均有硬上限；失败尝试同样扣预算；规则刷新不会沿用旧审核结果；过期更新使用真实 affected-row 语义 | 关键词摘要不能作为全文；通过 dynamic ID 拉取正文并原子维护不可伪造的 provenance；多 Core 副本共享租约/预算 | 架构授权阻塞 |
| 生产 Schema | `core/migrations/**`、`core/app/migrations_runner.py`、`core/app/main.py` | 生产运行时 DDL 被阻止，迁移失败会 fail closed | 完整版本化迁移、migration-first 启动、fresh production DB smoke | 授权阻塞 |

## 强制门禁矩阵

| 门禁 | 权威证据 | 当前结果 |
| --- | --- | --- |
| 原始规则存在且未被替换 | 数据库 `rule_text` 与来源更新流程 | 已实现 |
| 当前规则无需人工处理 | 当前解析结果，而非仅保存的旧 plan | 已实现；复杂规则会阻断 |
| 精确动作内容已审核 | reviewed payload hash/version | 未实现 |
| 目标身份一致 | canonical target + 页面/API 返回的动态 ID | 基础实现；selector 仍缺目标级 readback |
| 对应执行路径 Probe | path/config/target/account/plan 绑定证据 | 未实现，当前硬阻断 |
| 对应执行路径 Shadow | 同上，并含截图/观察/内容 readback | 部分实现，路径绑定未完成 |
| 账号操作互斥 | 统一租约和 generation fencing | 未实现 |
| 动作可恢复幂等 | pre-action intent + remote outcome + reconciliation | 未实现；当前仅故障后停止 |
| 安全证据存储 | Worker 启动自检 + Core 可验证共享目录 | 代码已实现；当前 Windows bind mount 不满足 |

补充状态：active policy 行存在但内容为空、坏 JSON、坏 UTF-8 或结构不合法时会 fail closed，不再静默回退内置默认策略。数据库命令已经提交后，非事务事件写入失败也不再向调用方伪装成“命令失败”并诱发重复提交；关键事件仍走重试、dead-letter 和高可见日志。

## 当前部署阻塞

`docker-compose.yml` 把 Windows 主机的 `./browser-profiles` 映射到 `/profiles`。实测容器内 `/profiles` 为 `0777`，Worker 启动自检会返回：

```text
code=evidence_directory_mode_insecure
directory=/profiles/task-failures
component=/profiles
operation=mode_check
```

不得通过放宽目录权限检查来恢复启动。需要经授权把浏览器资料与证据存储拆分，并让 Core/Worker 共享受控的 Linux named volume 或等价私有卷。

## 需要明确授权的变更包

1. **协议与持久化格式**：Action Plan v2、审核 payload hash/version、路径和证据绑定字段。
2. **Schema**：完整生产迁移、账号操作租约、外部动作 intent、reconciliation 与证据索引。
3. **核心执行语义**：统一账号操作 fencing、动作对账和安全续跑。
4. **部署拓扑**：拆分 browser profile 与 evidence volume，并重启本地服务验证。

这些授权不自动包含真实平台动作、依赖变更或 Git 操作。

## 验证基线

以下是本轮大范围修改前、使用现有本地测试镜像、源码只读挂载和 `--network none` 得到的历史快照，不能代表当前完整 diff 已通过同等回归：

- Core：478/478
- Worker：166/166
- 前端纯 JavaScript：4/4

当前 diff 在 2026-07-14 完成的离线验证：

- Python AST：188 个文件通过。
- Core 规则与目标纯逻辑：46/46；包含 ASUS 完整 UTF-8 原文的四动作、四类不可表达要求回归。
- Core 发现安全与规则刷新隔离测试：25/25。
- Core policy/risk 定向隔离测试：20/20。
- Core Action Plan 当前规则精确匹配：3/3；Action Plan 来源保护/API 事务：13/13。
- ASUS 保存计划不可覆盖语义门禁：1/1。
- Worker consume gate 与导航安全：30/30。
- Probe 启动、拒绝结算与权威绑定：3/3；Bilibili 恢复账本绑定：3/3；Bilibili executor 行为隔离场景：2/2。
- 前端纯 JavaScript 门禁：9/9。
- Lottery API 提交后事件、重复键与 repair 硬阻断：8/8；所有 endpoint 的提交后事件均复用 fail-safe helper；`git diff --check` 通过。
- 当前 diff 的 75 个文本文件均通过 UTF-8 strict、U+FFFD 与 C1 控制字符检查；PowerShell 默认显示乱码已确认是读取编码问题，不是文件损坏。
- 之前对实际 Windows bind mount 的验证仍按预期 fail closed。

当前两个 Python runtime 缺少完整项目测试所需的 `fastapi`、`databases`、`httpx`、`pytest` 或 Playwright 依赖组合；Docker daemon 未启动；前端 `node_modules` 不存在。因此最新完整 Core/Worker suite、真实 MySQL/Redis 并发 fencing、Linux openat 证据路径和 Vite/JSX build 尚未复跑。没有安装依赖、启动/重启服务、运行真实 Bilibili 动作、执行数据库迁移或进行 Git 写操作。

## 已知剩余维护风险

1. Bilibili 关键词搜索可能只返回截断摘要；跨来源 canonical 去重又未原子维护 provenance，且手工创建的 `source_type` 不可作为可信证明。当前 Bilibili Core/Worker 总闸仍会阻止真实动作，但解除总闸前必须通过完整正文摄取、不可伪造的 attestation 和去重升级规则解决。
2. 前端已经要求 `execution_evidence_bound === true` 的正向结果，但 Core 尚无该版本化协议位，因此当前会硬阻断；需要在获准的证据绑定协议中正式提供并校验该证明。
3. 多 Worker 在同一主机直接以 hostname 作为实例 ID 时会冲突；需要把唯一实例标识和租约/fencing 一并设计，不能只改显示字符串。
4. 账户风险查询为保证正确性会读取账号 24 小时窗口内全部事件；异常事件洪峰下应改成按冷却语义的 SQL 筛选，不能恢复固定“最近 N 条”截断。
5. 发现调用预算是单进程、单轮边界；多 Core 副本仍需要共享租约/预算。

## 后续实施顺序

1. 先确认四类授权并冻结 Action Plan v2 与证据绑定契约。
2. 并行开发：规则/审核 UI、Probe/Shadow、幂等/账号租约、迁移/证据卷。
3. 在全新本地数据库上验证 migration-first 启动。
4. 运行 Dry Run 和 Shadow Run，验证 payload hash、执行路径和证据 readback 全链一致。
5. 完成逐项验收审计后，真实平台测试仍需单独明确授权。
