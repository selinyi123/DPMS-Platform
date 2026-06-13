# DPMS_V9_SemanticRuntime_实施记录_v1.0_20260613

## 背景

`docs/DPMS_V8-V9_后续版本规划_20260612.md` 将 S8 / V9 定义为 **Semantic Runtime（语义执行链）**：把 `Intent -> Institution -> Policy -> Transition -> Execution` 五层在**一次查询内**串起来，让任意一个目标都能回答"它为什么被考虑、当时适用哪条策略、门禁怎么判、规则怎么演化来的、最终发生了什么"。

V9 的重点**不是新增决策能力**，而是新增一个**纯聚合的解释/审计视图**：把已经存在于 V5（策略评分）、V6.5（学习预测）、V7（治理决策）、V8（迁移血统）各表/各 API 中的数据，按同一个 subject 串成一条链并生成自然语言叙述。延续 S1-S7 确立的"纯逻辑模块 + 单元测试 + 薄 API 层 + 前端页面"模式，**不修改任何既有 API 字段语义,仅新增模块、只读端点与页面，且不新增任何数据表**。

## 改动内容

### 1. 纯逻辑模块 `core/app/semantic/trace.py`（无 DB 依赖）

- `build_semantic_trace(*, subject_type, subject_id, intent=None, institution=None, policy_decision=None, transition_lineage=None, execution=None) -> dict`：纯函数，输入均为已从各 API/表取出的 plain dict/list（或 `None`），输出统一的五段结构 + `narrative` + `consistency_checks`。任一输入缺失时对应段为空 dict/空 lineage，**绝不抛异常**（fail-soft）。
- `narrative_lines(trace, lang="en") -> list[str]`：把五段结构转成一组简短陈述句，中（`zh`）英（`en`）两套模板并排维护（`_TEMPLATES`），保证不漂移。缺失的层输出"暂无记录"句而非被丢弃——叙述永远覆盖全部五层。
- `consistency_checks(trace) -> list[str]`：轻量跨层一致性提示（**仅提示，不阻断**）：①门禁结果为 `block`/`deny` 但执行状态却落在 `{succeeded, won, completed, success}`；②决策时的 `policy_version` 小于当前 `active_version`（说明该决策发生在策略变更之前，应以历史版本回放）。一致的链路返回 `[]`。
- 常量 `BLOCKING_OUTCOMES`、`SUCCEEDED_STATUSES` 集中定义判定词表；`_normalize_transition` 把 `GET /transitions/{policy_key}/lineage` 的响应（含 `chain`）或裸 hop 列表统一规约为 `{policy_key, active_version, lineage}`，每跳只保留叙述/UI 需要的少数字段，trace 不再携带完整 diff。

### 2. 只读 API `core/app/api/semantic.py`（前缀 `/api/semantic`）

- `GET /trace/{subject_type}/{subject_id}`（当前仅支持 `subject_type="lottery"`，否则 400；`subject_id` 必须可转 int，否则 400；lottery 不存在 404）。内部按五层依次取数后调用 `build_semantic_trace`：
  1. **Intent**：复用 `app.api.lotteries.explain_lottery_strategy(lottery_id)`（V5/S2）取 `strategy_score`/`priority_tier`/`recommended_mode`/`value_score`；并尝试在 `app.api.learning.target_predictions(limit=100)`（V6.5）中匹配该 `lottery_id`，补 `top_drivers`/`probability`/`model_version`（目标不在 pending/claimed 预测集合中时留空，fail-soft）。
  2. **Institution**：复用治理层 `_load_active_policy(REAL_RUN_GATE_POLICY_KEY)`，取 `policy_key` + 当前 `active_version`。
  3. **Policy**：`policy_decisions` 中按 `subject_id` 取最新一条（`outcome`/`policy_version`/`reasons`/`decision_id`）；`next_action` 由 trace 层从 `reasons[0].remediation` 派生。
  4. **Transition**：复用 `app.api.transitions.policy_lineage(policy_key)`（V8），直接得到 lineage 链。
  5. **Execution**：该 lottery 的 `task_runs`（最近 20 条：`task_id`/`status`/`task_mode`/时间），`status` 取 lottery 自身状态，并附 `latest_run_status`。
- 全程**零写操作**；通过复用既有路由函数而非重复 SQL，避免与 V5/V6.5/V7/V8 的取数逻辑漂移。

### 3. 前端 `frontend/src/pages/SemanticTrace.jsx`

- 输入 `lottery_id` → `GET /semantic/trace/lottery/{id}` → 顶部展示 `narrative`（按当前界面语言取 `narrative[language]`，回退 `en`）与 `consistency_checks`（非空时以 `alert-warn` 横条逐条展示，空则提示"未发现跨层不一致"）。
- 五段卡片网格（`semantic-grid`/`semantic-card`，`auto-fit` 自适应）：Intent / Institution / Policy / Transition / Execution，各自缺数据时显示该层的"暂无记录"占位；Policy 的 `outcome` 用 `badge-ready`/`badge-danger` 着色，Transition 的最近一跳复用 `transitions.classifications.*` 文案与收紧/宽松/中性配色。
- `frontend/src/App.jsx` 注册页面 `semantic` 与导航项；`frontend/src/uiContext.jsx` 新增 `nav.semantic` 与完整 `semantic` 中英文命名空间；`frontend/src/index.css` 新增 `.alert-warn`/`.reason-list-block`/`.semantic-grid`/`.semantic-card` 样式。

## 设计取舍

- **不新增数据表**：V9 是纯聚合层，所有数据都已由 V5-V8 落库；新增表只会引入与既有表同步的负担。`build_semantic_trace` 输入全部来自既有 API/表，保证"语义层永远是只读旁路"。
- **`next_action` 由 `reasons` 派生而非新存字段**：`policy_decisions` 未持久化 `next_action`，trace 层 `_policy_next_action` 优先用显式字段、否则取首个未通过门禁的 `remediation`，与 V7 `evaluate_policy` 的 `next_action` 语义一致，不必改表。
- **叙述中英并排模板**：`narrative_lines(trace, lang)` 返回单语 list，`build_semantic_trace` 同时生成 `{"en": [...], "zh": [...]}` 存入 trace；前端按界面语言取用，API 一次返回两套，避免按语言重复请求。

## 验证记录

验证时间：2026-06-13

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 新增纯模块独立导入 | 通过 | `app.semantic.trace` 无 DB/FastAPI 依赖 |
| core 单元测试 | 通过 | `python -m unittest discover -s tests`：**193 项全部通过**（原 183 + 新增 10） |
| worker 单元测试 | 通过 | `worker` 21 项保持通过 |
| `app.main` 完整导入 | 通过 | 设置 `ENCRYPTION_KEY` 后 `import app.main` 成功，共 **94 条路由**，含新增 `/api/semantic/trace/{subject_type}/{subject_id}` |
| 前端构建 | 通过 | `npm run build` 成功，127 modules，输出至 `dashboard/dist` |
| 行为保持检查 | 通过 | 未修改任何既有 API 字段语义；未新增数据表；新增均为附加只读路由与页面 |
| 安全边界检查 | 通过 | 端点只读、零写；叙述/一致性提示仅供展示，不进入 `evaluate_policy`/real-run 门禁 |

## 安全边界

- **纯聚合、零新增决策权**：`/api/semantic/trace` 不写任何表，`narrative`/`consistency_checks` 仅用于展示，**绝不**作为 `evaluate_policy`/real-run 门禁/断路器的输入，杜绝"解释文本反向影响决策"的循环依赖。
- **fail-soft 仅用于展示**：任一来源缺失时该段为空、叙述输出"暂无记录"，但这只影响语义视图本身，**不**改变 V7/V8 的 fail-closed 门禁——语义层永远是只读旁路。
- **跨层一致性只提示不阻断**：`consistency_checks` 发现"门禁阻塞却执行成功""决策版本旧于当前生效版本"时给出文字提示供人工核查，不触发任何自动动作。
- 与 V5.5-V8 完全相同的总原则：语义链是**审计与建议层**，从不直接触发 real-run、断路器或账号操作；V9 把"只读、advisory、可解释、可回放"四个既有性质**可视化**，而非新增第五个性质。

## 已知限制

- 当前仅支持 `subject_type="lottery"`；扩展到其他主体类型需要为其分别接入 intent/policy/execution 取数（institution/transition 已与 `policy_key` 解耦，可直接复用）。
- Intent 的 `top_drivers`/`probability` 依赖学习预测集合（仅含 `pending`/`claimed` 目标）；目标不在该集合时这两项为空，叙述以策略评分/档位为主，属预期 fail-soft 行为。
- 带数据库的端到端联调（针对一个有真实门禁评估记录的 lottery 验证五段齐备）依赖部署环境的 MySQL；本次在无 DB 环境完成纯逻辑测试、路由注册与前端构建验证，端到端联调留待本机部署环境执行。

## 后续增强（2026-06-13）：Governance ↔ Semantic 交叉链接

规划文档（V9 前端章节）要求"语义链可作为 Governance 页'评估真实运行门禁'面板的'查看完整语义链'跳转目标，避免与 V7 页面重复造轮子"。本次补齐该集成，**纯前端改动，未触碰任何后端/接口**：

- **跨页导航下沉到 `uiContext`**：把原本散落在 `App.jsx` 内的 `page` 状态上移到全局 `UiProvider`，新增 `pageParams` 与 `navigate(nextPage, params)`。导航栏按钮走 `navigate(key)`（不带 params，等同于清空上一次的深链负载），任意页面均可通过 `navigate('semantic', { subjectId })` 携带参数深链跳转。`App.jsx` 改为从 context 读取 `page`/`navigate`，并对未知 page key 回退 `dashboard`。
- **Governance 页两处跳转点**：①"评估真实运行门禁"结果面板底部新增"查看完整语义链"按钮，携带刚评估的 `lotteryId` 跳转；②"近期决策"表中 `subject_type === 'lottery'` 的行新增同名按钮，携带该决策的 `subject_id` 跳转。
- **`SemanticTrace.jsx` 消费深链参数**：`useEffect` 监听 `pageParams.subjectId`，存在时自动回填输入框并触发查询（`lookup` 重构为可接收显式 id）；手动从导航栏进入时 params 为空，行为不变。
- i18n 新增 `governance.viewSemanticTrace`（中"查看完整语义链"/英"View full semantic trace"）。

安全边界不变：跳转只是把同一个只读 `GET /api/semantic/trace/lottery/{id}` 的入口前置到 Governance 操作流里，不新增任何写操作或决策权。`npm run build` 通过（127 modules），core 193 项测试不受影响（本次零后端改动）。

## 后续增强（2026-06-13）：补充第三条跨层一致性检查

回应「下一步」中"为 `consistency_checks` 增补更多跨层不变量"，`core/app/semantic/trace.py` 新增第三条检查（与既有两条并列，仍为**仅提示、不阻断**）：

- **执行层内部一致性**：`execution.latest_run_status`（最近一条 `task_runs` 的状态）落在 `SUCCEEDED_STATUSES` 中，但 `execution.status`（lottery 自身状态）尚未同步到该集合时，提示"最近一次任务运行已成功，但 lottery 记录状态仍为 X，请核对 lottery 记录是否已更新"。该检查只读取 trace 内已聚合的两个字段，不引入新的数据源。

新增 2 项单元测试（`test_stale_lottery_status_after_successful_run_is_flagged`、`test_lottery_status_matching_latest_run_has_no_stale_hint`），均通过；core 单元测试增至 **195 项全部通过**（原 193 + 新增 2）。`narrative_lines` 与既有两条检查均未改动，前端 `SemanticTrace.jsx` 无需改动（已通用渲染 `consistency_checks` 列表）。

## 后续增强（2026-06-13）：subject_type 泛化到 account

回应「下一步」中"将 `subject_type` 泛化到 `account`/`task` 等主体"，本次把语义链从"仅 lottery"扩展到 **lottery + account** 两类主体。后端复用 V6 风险情报与 `task_runs`，**不新增数据表、不新增决策权**：

- **纯模块 `trace.py` 保持主体无关**：`narrative_lines` 的意图句改为读取通用 (score, tier)——lottery 用 `strategy_score`/`priority_tier`（V5），account 用 `reputation_score`/`account_tier`（V6）；新增 `_intent_score_tier` 做字段回退。叙述的"首要驱动"子句改为**仅在存在驱动因子时拼接**，因此 account（无学习驱动）输出"目标 7 评分 78（A 档）。"而 lottery 仍输出带驱动的整句。lottery 既有输出逐字不变。
- **API `semantic.py` 按 `subject_type` 分流**：`SUPPORTED_SUBJECTS = {"lottery", "account"}`；institution/transition 两层主体无关（同一 `real_run_gate` 策略对象与其血统治理所有主体），仅 intent/execution 按主体取数。account 的 intent 复用 `account_risk_intelligence(account_id)`（信誉分 + `account_tier()` + 24h 风险等级 + 建议动作），execution 改按 `task_runs.account_id` 取该账号的运行记录。
- **修正潜在跨类型主键碰撞**：`_latest_decision` 原仅按 `subject_id` 取最新决策，泛化后 lottery #5 与 account #5 会互相串号；现改为同时按 `subject_type` 与 `subject_id` 过滤（对既有 lottery 行为等价，因当前只写过 `subject_type='lottery'`）。
- **前端 `SemanticTrace.jsx` 新增主体类型下拉**（抽奖目标 / 账号），查询走 `/semantic/trace/{type}/{id}`；account 的意图卡片展示信誉分/档位/风险等级/建议动作。Governance 既有深链不带 `subjectType`，默认回退 `lottery`，行为不变。i18n 两端新增 `semantic.subjectType*` 与 `semantic.intent.{reputationScore,accountTier,forecastBand,recommendedAction}`。

新增 2 项纯模块测试（account 意图用信誉/档位且不带驱动子句、lottery 仍带驱动子句），core 单元测试增至 **197 项全部通过**；`app.main` 导入正常（94 条路由不变，同一端点现支持两类主体）；`npm run build` 通过（127 modules）。安全边界不变：account 语义链同样只读、advisory，account 的 Policy 层在尚未产生 account 级门禁决策时按 fail-soft 显示"暂无决策"。

## 下一步

V9 是 `docs/DPMS_能力演化路线图_v4-v9_20260605.md` 规划范围内的最后一个里程碑。后续可选方向（均为既有运行时的深化，而非新增安全性质）：继续为 `consistency_checks` 增补跨层不变量；将 `subject_type` 进一步泛化到 `task` 等主体（account 已落地）。规划基线仍以 [[DPMS_总设计方案_v1_20260611]] 与 [[DPMS_V8-V9_后续版本规划_20260612]] 为准。

## 对应 Git 提交

- `f61aa98 Add Semantic Runtime (V9 / stage S8): read-only Intent->Execution trace`
- `5e3c90a Link Governance real-run panel and decisions to the Semantic Trace`
- `15de710 Add a third semantic consistency check: stale lottery status after a succeeded run`
- `__PENDING__` Generalize the semantic trace to account subjects alongside lotteries
