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

## 下一步

V9 是 `docs/DPMS_能力演化路线图_v4-v9_20260605.md` 规划范围内的最后一个里程碑。后续可选方向（均为既有运行时的深化，而非新增安全性质）：把语义链作为 Governance 页"评估真实运行门禁"面板的跳转目标；为 `consistency_checks` 增补更多跨层不变量；将 `subject_type` 泛化到 `account`/`task` 等主体。规划基线仍以 [[DPMS_总设计方案_v1_20260611]] 与 [[DPMS_V8-V9_后续版本规划_20260612]] 为准。

## 对应 Git 提交

- `__PENDING__ Add Semantic Runtime (V9 / stage S8): read-only Intent->Execution trace`
