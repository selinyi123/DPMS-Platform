# DPMS_V10-V13_OperationalScaling_实施记录_v1.0_20260614

## 背景

V4–V9 九个里程碑（认知阶梯：记忆→经验→决策→实验→预测→学习→制度→演化→自我解释）已全部落地。本次按 [[DPMS_V10-V13_运营规模化_后续版本规划_20260614]] 推进**运营规模化主线**的四个重量级大版本 **V10–V13**，把执行从「单目标、单账号、手工排程」升级为「跨平台、批量账号、可持续吞吐」的**有序编排**。

延续 S1–S8 的「纯逻辑模块 + 单元测试 + 薄只读 API 层 + （必要时）幂等 schema + 前端页面」模式。**核心立场不变**：一切新增层均为**只读、advisory、零新增决策权**——计划/建议都是草案，真实派发仍只能经由既有 real-run 门禁 + 管理员授权 + 熔断器；限速一律是合规限速，引擎只会让执行**更保守**；账号隔离、强制安全爬坡、背压单向降速作为本主线四条新增安全不变量，各有直接回归测试。

## 改动总览

| 版本 | 纯模块 | 只读 API（前缀） | 新增表 | 新增测试 |
| --- | --- | --- | --- | --- |
| V10 Scheduling | `core/app/scheduling/engine.py` | `/api/scheduling`（plan/limits） | 无 | 12 |
| V11 Capacity | `core/app/capacity/engine.py` | `/api/capacity`（overview/bindings） | 无 | 8 |
| V12 Orchestration | `core/app/orchestration/engine.py` | `/api/orchestration`（campaign/draft/drafts） | `campaign_plans` | 11 |
| V13 Throughput | `core/app/throughput/engine.py` | `/api/throughput`（overview/backpressure） | 无 | 11 |

测试基线：core **242 项**（原 200 + 新增 42）全部通过；`app.main` 导入正常，路由 94 → **103**；`npm run build` 通过（127 → **131 modules**）。

## V10：Scheduling Runtime（调度运行时 / S9）

### 纯模块 `core/app/scheduling/engine.py`

- `PLATFORM_RATE_LIMITS`/`DEFAULT_RATE_LIMIT`/`rate_limit_for(platform)`：每平台保守合规限速 `{max_daily_per_account, min_spacing_min, max_per_window}`；未知平台回退到最保守默认。
- `plan_schedule(*, candidates, accounts, window_minutes, limits=None)`：时间以「相对 now 的分钟偏移」表达（保持纯函数）。按平台分组；候选 `(value_score desc, lottery_id asc)`；逐候选挑「最早可用且仍有配额」的账号，分配 `scheduled_at_offset_min`，随后该账号 `next_free += spacing`、`quota_left -= 1`；超窗/超配额/超窗口上限的候选进入 `unscheduled` 带 `reason`。
- `respects_rate_limit(plan, limits)`：对**已生成的计划**独立复核「同账号相邻时隙间隔 ≥ spacing、全部落在窗内」——作为 V10 安全不变量回归（测试断言任意生成计划恒过）。
- `detect_overload(...)`：需求 > 窗内可持续供给的平台清单。

### API `core/app/api/scheduling.py`

- `GET /plan?window_minutes=&platform=`：加载 `status='ready'` 账号（`daily_task_count`/`last_active_at`/`risk_score`）与 `pending|claimed` 未过期候选；由 `last_active_at` 与平台 spacing 计算 `ready_in_min`、由 `max_daily - daily_task_count` 计算 `remaining_quota`；调用 `plan_schedule` 后把分钟偏移转回 ISO 时间戳；附 `overloaded_platforms`。
- `GET /limits`：透明返回合规限速表。
- 时间戳/冷却解析全部 fail-soft（缺失或不可解析 → `ready_in_min=0`，由门禁与配额继续兜底）。`mode="gated"` 明示真实模式由门禁决定。

### 安全边界

排程只会在超限时**留空**，永不安排超过合规限速的任务；不派发、不写库。

## V11：Capacity Runtime（容量运行时 / S10）

### 纯模块 `core/app/capacity/engine.py`

- `compute_capacity(accounts, proxies, *, limits)`：每平台 `{safe_accounts, total_accounts, healthy_bound_proxies, sustainable_daily, max_daily_per_account}` + 全局 `proxy_pool{total, healthy, bound, free_healthy}`。`sustainable_daily = backing * max_daily`，其中 `backing = min(safe_accounts, healthy_bound_proxies)`——供给受「安全账号」与「健康代理」中更稀缺者封顶。健康代理 = `status active 且非冷却 且 health_score>=60`。
- `recommend_bindings(accounts, proxies)`：在**一账号一代理**约束下，为缺代理的 ready 账号建议**未绑定的**健康代理（按健康分降序贪心配对）；供给不足者进 `unmet`。从不建议共享。
- `isolation_violations(accounts)`：检测多账号共享 `proxy_id` 或 `fingerprint_id`，返回 `{kind, key, account_ids, detail}`——非空即隔离风险（应为空）。

### API `core/app/api/capacity.py`

- `GET /overview`：每平台供给 + 可持续上限，并以 `accounts.daily_task_count` 求和得 `current_daily_used` 与 `headroom`；全局代理池。代理冷却由 `cooldown_until > now` 判定。
- `GET /bindings`：绑定建议 + `isolation_violations`。

### 安全边界

账号隔离是不变量：引擎只检测/建议，**从不**制造共享绑定；隔离冲突前端醒目标红。

## V12：Orchestration Runtime（编排运行时 / S11）

### 纯模块 `core/app/orchestration/engine.py`

- `RAMP_STAGES = (dry_run, shadow_run, real_run)`、`target_stage(target)`：目标的**保守可达模式**——`gate_ready` 才 real_run，`shadow_eligible` 才 shadow_run，否则 dry_run（永不高于就绪度允许）。
- `build_campaign_plan(*, targets, capacity=None, wave_capacity=None)`：按模式（ramp 顺序，安全模式先行）再按平台分组，每平台按 `value_score` 降序、用容量（V11 `sustainable_daily`）或显式上限分块，逐块成波次；返回 `{waves:[{index, mode, ramp_step, platform_loads, items, size}], summary:{total, by_mode, wave_count, platforms, requires_review}}`。
- `validate_campaign(plan, *, gate_state)`：`real_run` 项必须 `gate_ready`、`shadow_run` 项必须 `shadow_eligible`，否则报 `real_run_without_gate` / `shadow_run_without_probe`——**镜像门禁，只会更严**，作为 V12 安全不变量回归。
- `campaign_risk_summary(plan)`：每平台负载、模式分布、real 项数与复核标记。

### API `core/app/api/orchestration.py` + 表 `campaign_plans`

- `GET /campaign?platform=`：候选 lotteries（pending/claimed 未过期）的就绪度**完全由已记录数据派生**——`shadow_eligible` 来自「存在 task_mode='shadow_run' 且 succeeded 的 task_run」，`gate_ready` 来自「policy_decisions 中该 lottery 存在 outcome='allow'」。即**campaign 从不重新评估门禁，只编排门禁已记录放行的目标**。返回 plan + `risk_summary` + `validation`（不写库）。
- `POST /campaign/draft`（admin）：**服务端重新生成**计划并存为 `campaign_plans`（`status='draft'`，含 `waves`/`requires_review`）——草案是**惰性**对象，永不自动激活/执行；客户端无法注入任意 plan。`GET /drafts` 为审计视图。
- 表 `campaign_plans` 经 `ensure_orchestration_schema()` 幂等创建，挂入 `ensure_runtime_schema()`。

### 安全边界

计划永远是 `draft`；强制爬坡不可跳级；real 波次仅含门禁已记录放行的目标；采纳仍走既有派发流程。

## V13：Throughput Runtime（吞吐运行时 / S12）

### 纯模块 `core/app/throughput/engine.py`

- `measure_throughput(runs, *, window_min)`：每平台 `{observed, succeeded, failed, success_rate, failure_rate, observed_per_hour}`。
- `sustainable_ceiling(capacity, risk_signals)`：`ceiling = sustainable_daily * (1 - discount)`，`discount = min(0.75, risk_rate + max(0, failure_rate-0.2))`——风险**只会收紧**上限，且永不低于额定的 1/4。
- `backpressure_recommendation(observed, ceiling, risk_trend)`：`action ∈ {scale_up, hold, throttle, pause}`。**风险上升（rising）时只输出 throttle/pause，绝不 scale_up**；`scale_up` 仅在低利用率（<0.5）且风险平稳且 risk_rate<0.25 时给出，且 `factor` 受 `ceiling/observed` 封顶，确保投影负载永不破上限——V13 安全不变量回归。
- `saturation_alerts(observed, ceiling, threshold=0.9)`：利用率达阈值或有负载却无容量的平台。

### API `core/app/api/throughput.py`

- `GET /overview?window_minutes=`：窗内 `task_runs`（经 accounts 取 platform）聚合 + 风险率（窗内 `risk_events`/观测量）+ 把 V11 日上限按窗缩放后的 `ceiling` + 利用率 + 饱和告警。
- `GET /backpressure?window_minutes=`：背压建议 + 饱和告警；`risk_trend` 由「近半窗 vs 前半窗」风险计数比较得出（rising/flat/falling）。

### 安全边界

背压**只朝降速方向**起作用，强化（非绕过）限速与熔断；纯只读，不改变任何执行。

## 前端

四个只读控制台页面（`frontend/src/pages/{Scheduling,Capacity,Orchestration,Throughput}.jsx`），在 `App.jsx` 注册、`uiContext.jsx` 增 `nav.*` 与中英完整命名空间，新增 `.alert-ok` 样式：

- **Scheduling**：排程概览指标 + 时隙表（计划时间/账号/偏移/门禁模式）+ 未排程原因 + 合规限速表 + 过载告警。
- **Capacity**：每平台供给与可持续上限/余量 + 代理池 + 绑定建议 + 隔离冲突（红条）。
- **Orchestration**：波次表（模式按 ramp 着色）+ 校验状态/错误 + 惰性「存为草案」（用 `postJSON`）+ 已存草案审计列表。
- **Throughput**：观测 vs 上限利用率 + 饱和告警 + 背压表（scale_up/hold/throttle/pause + 风险趋势）。

每页顶部均有 `alert-warn` 安全横条，明示「只读、不派发、真实执行仍走门禁」。

## 验证记录

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| core 单元测试 | 通过 | `python -m unittest discover -s tests`：**242 项**全部通过（原 200 + 新增 12+8+11+11） |
| `app.main` 导入 | 通过 | 设 `ENCRYPTION_KEY` 后导入成功，路由 **103** 条，含 9 条新端点 |
| 前端构建 | 通过 | `npm run build` 成功，**131 modules**，输出至 `dashboard/dist` |
| 行为保持 | 通过 | 未修改任何既有 API 字段语义；仅新增模块/只读端点/页面 + 一张审计草案表 |
| 安全边界 | 通过 | 四条新增不变量（排程不越限 / 隔离检测 / 爬坡不可跳级 / 背压单向）各有直接测试 |

## 安全边界总览（四条新增不变量）

1. **排程不越限**（V10）：`respects_rate_limit` 对任意生成计划恒为真；排程只 withhold，不放宽限速。
2. **账号隔离**（V11）：`isolation_violations` 检出共享、`recommend_bindings` 永不建议共享。
3. **爬坡不可跳级**（V12）：`validate_campaign` 拒绝任何未就绪即升档的目标；real 波次仅含门禁已记录放行项；草案惰性。
4. **背压单向**（V13）：风险上升只产出 throttle/pause；scale_up 受上限封顶，永不破 ceiling。

与 V4–V9 一致：本主线全部为**审计与建议层**，从不直接触发 real-run、断路器或账号操作；real-run 门禁、管理员授权、熔断器、证据记录、审计日志五道关卡保持不变。

## 下一步

V10–V13 完成运营规模化主线的「编排时间 → 编排资源 → 编排广度 → 守住可持续」闭环。后续可选深化（均不新增安全性质）：把 V10 排程结果与 V12 波次落为可对照的执行队列建议；为 V13 背压接入更细的熔断邻近度信号；将容量/吞吐指标下沉到 Dashboard 概览卡。规划基线以 [[DPMS_V10-V13_运营规模化_后续版本规划_20260614]] 与 [[DPMS_总设计方案_v1_20260611]] 为准。

## 对应 Git 提交

- `a37fa1b Add operational-scaling backend (V10-V13)`
- `a527d13 Add operational-scaling console pages (V10-V13)`
