# DPMS_外部调研与v0.3.5规划_20260620

## 0. 本轮结论

当前 DPMS 不应继续堆叠新平台或大版本概念。主线应收敛为：

```text
v0.3.4：运行时可信度硬化与状态对齐
v0.3.5：Bilibili controlled validation node
v0.4.0：单平台小规模稳定实测版
```

本轮审查确认：主分支已经具备 transactional outbox、Redis pending recovery、versioned SQL migration runner、default-closed API auth、secret posture guard、readiness API、Bilibili selector-driven platform、smoke harness 等关键能力。当前更紧迫的问题不是继续扩张，而是把文档、版本账本、部署变量与下一节点验收口径对齐。

## 1. 外部调研摘要

### 1.1 GitHub / 开源项目

| 项目 | 观察 | 对 DPMS 的可用结论 |
| --- | --- | --- |
| browser-use/browser-use | 通用 AI browser agent，强调真实浏览器、持久化工具、恢复循环、CLI 与云端扩展。 | DPMS 不应直接变成通用 agent；应吸收其“持久化浏览器 + recovery loop + allowed domains / operator boundary”思路。 |
| Skyvern-AI/skyvern | 面向 RPA 的 browser workflow runtime，包含任务、工作流、UI、SDK、数据提取 schema、人工调试能力。 | DPMS 应保持“任务对象 + evidence + UI 可观测 + 人工确认”，而不是让 LLM 自主决定副作用动作。 |
| browserbase/stagehand | Browser agent SDK，核心价值是把传统 Playwright 与 AI 辅助操作结合。 | DPMS 可以在未来引入“selector probe 辅助建议”，但 real-run 仍必须依赖固定 selector config 和 gate。 |
| lavague-ai/LaVague | Large Action Model Web Agent，强调 action model、成本追踪、telemetry。 | DPMS 若未来引入 AI，必须先加成本计量、动作日志和敏感信息边界。 |
| shanmiteko/LotteryAutoScript | 轻量抽奖自动化脚本方向。 | 适合作为平台动作规则参考，不适合作为 DPMS 的架构底座；DPMS 已经走向账号资产、任务门禁、证据链、治理运行时。 |

### 1.2 Redis Streams / 队列可靠性

Redis Streams consumer group 的关键不是“能消费”，而是 pending list 的恢复、幂等、终态回收与补偿。DPMS 当前已经向正确方向推进：

- pending recovery 不再只补发 `task_id` stub，而应从 DB 重建完整任务上下文。
- dispatch 写 DB 与写 Redis 之间应由 transactional outbox 衔接。
- stranded lock 需要 reconciler 释放。

后续 v0.3.5 要验证的不是代码是否存在，而是在真实部署中是否能稳定恢复：worker kill、Redis 短暂不可用、重复 dispatch、任务失败截图、outbox retry。

### 1.3 Playwright / browser runtime

DPMS 采用 `launch_persistent_context` 的方向正确。下一阶段重点不是“更像真人”，而是：

- profile 目录生命周期管理；
- account calibration 的 evidence 标准；
- browser TTL / memory / heartbeat 指标；
- shadow-run 截图与 selector probe 结果的人工复核；
- 失败后明确停止，而不是盲目重试。

### 1.4 Reddit / X / 知乎 / Hackathon 类社区信号

公开社区对 browser automation 的共识大致收敛在三点：

1. 通用 AI agent 在动态网页和长任务中仍容易不稳定。
2. 生产系统需要人工确认、动作白名单、日志与回放，而不是完全自动点击。
3. 可靠性工程比“更聪明的 agent”更重要：任务幂等、状态机、失败恢复、证据链、权限边界。

因此 DPMS v0.3.5 的路线不应是“AI 自动理解全平台页面”，而应是“Bilibili 单平台证据驱动验证”。

## 2. 当前仓库审查结论

### 2.1 已完成

- Core API 已有 default-closed auth，`/api/health` 之外的 API 默认鉴权。
- Startup 已接入 versioned migration runner。
- Runtime 已接入 outbox dispatcher、recovery daemon、notification dispatcher、scheduler。
- Metrics/readiness API 已能输出平台 readiness、blockers、production checks、strategy advice。
- Bilibili 已进入 selector-driven 平台路径，而非仅是普通 planned 平台。
- 主分支已有 smoke harness 和 review fixes 的合并记录。

### 2.2 本轮发现并修复

| 问题 | 影响 | 本轮处理 |
| --- | --- | --- |
| README 仍停留在旧 `0.3.2` / V4 叙述 | 外部协作者无法判断当前真实阶段 | 更新 README 的版本状态、能力边界、安全边界和关键文档 |
| 缺少 `VERSION.md` | 产品版本、架构阶段、runtime stage 混在一起 | 新增 `VERSION.md` 作为版本账本 |
| `.env.example` 缺少 `DEPLOYMENT_MODE` | `config.py` 已支持该字段，但部署模板未暴露 | 新增 `DEPLOYMENT_MODE=dev` |
| 缺少本轮外部调研记录 | 后续调研容易重复 | 新增本文件作为调研账本与下一节点规划 |

## 3. v0.3.5 后续版本规划

### 3.1 v0.3.5：Bilibili controlled validation node

目标：

```text
单平台、低并发、证据驱动、人工确认、可回滚
```

验收项：

1. 一键 smoke 路径：账号导入/QR 登录 -> calibration -> target import -> action plan -> dry-run -> shadow-run -> evidence。
2. Worker kill 恢复测试：任务进入 running 后杀 worker，recovery 后从 DB 重建完整 payload。
3. Redis 异常测试：dispatch 后 Redis 短暂不可用，outbox 后台补投递。
4. Selector calibration 手册：把 Bilibili selector config 的采集、验证、保存、回滚写成固定 SOP。
5. Real-run 只允许 1 个测试账号、1 个低价值目标、手动确认、失败自动熔断。
6. Evidence review 页面/接口必须能看到 shadow screenshot、probe summary、gate blockers、policy decision。

### 3.2 v0.3.6：operator evidence console

目标：把当前分散的 evidence、probe、task_runs、policy decisions 汇总成一个“能拍板”的控制台视图。

范围：

- Evidence timeline by lottery。
- Account readiness and last calibration summary。
- Real-run gate explanation。
- One-click rollback to safe mode: `REAL_RUN_ENABLED=false` + circuit breaker open。

### 3.3 v0.4.0：single-platform small-scale validation

目标：只验证 Bilibili，暂不扩平台。

范围：

- 5-10 个账号级别的稳定性验证。
- 每日任务上限、账号冷却、风险事件阈值。
- 任务复盘：成功率、失败原因、截图证据、恢复次数。
- 形成“可运行但非大规模”的单平台 beta。

## 4. 禁止事项

v0.3.5 之前禁止：

- 新增平台优先级高于 Bilibili 验证。
- 默认打开 real-run。
- 绕过人工确认或证据门禁。
- 在没有 migration 的情况下继续 runtime schema 漂移。
- 让 AI agent 直接决定副作用动作。

## 5. 下一轮执行清单

```text
1. 增加 Bilibili validation checklist 页面或文档。
2. 补 worker kill / outbox retry / recovery rebuild 的纯逻辑与集成测试。
3. 固化 selector calibration SOP。
4. 打通 readiness -> evidence -> gate -> action plan 的操作路径。
5. v0.3.5 合并后再评估是否进入 v0.4.0。
```
