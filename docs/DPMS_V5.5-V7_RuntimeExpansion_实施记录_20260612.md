# DPMS_V5.5-V7_RuntimeExpansion_实施记录_v1.0_20260612

## 背景

`docs/DPMS_总设计方案_v1_20260611.md` 在 S1（Strategy Runtime 模块化）与 S2（决策可解释 + Strategy/Knowledge 前端）之后，将 S3-S5+ 定为：

- **S3 / V5.5 Experiment Runtime**：dry-run / shadow-run 范围内的策略 A/B 对比闭环。
- **S4 / V6 Risk Intelligence**：账号信誉聚合 + 24 小时风险预测，预测只能收紧派发。
- **S5+ / V6.5-V9 Learning / Governance / Transition / Semantic**：Feature Store → 概率模型（仅建议）→ Policy Object 制度化 → 策略迁移图 → 语义执行链，每步均要求“模型只建议、版本可记录、特征可解释、决策可回放”。

本次实施一次性推进 4 个里程碑版本：**V5.5（S3）、V6（S4）、V6.5（S5 前半：Feature Store + 概率模型）、V7（S6：Policy Object 制度化）**，延续 S1/S2 确立的“纯逻辑模块 + 单元测试 + 薄 API 层 + schema 迁移”模式，**不修改任何既有 API 字段语义，仅新增模块与端点**。

## 改动内容

### 1. S3 / V5.5 — Experiment Runtime（实验运行时）

新增 `core/app/experiment/`：

- `engine.py`（无 DB 依赖）：
  - `SAFE_EXPERIMENT_MODES = ("dry_run", "shadow_run")`；`real_run` 实验必须 `allow_real_run=True` 且由管理员显式开启，分发时仍受标准 real-run 门禁约束。
  - `validate_experiment_spec()`：校验名称/平台/模式/分支（≥2 个、key 唯一、权重为正）。
  - `assign_branch()`：基于 `unit_key` 的 SHA256 哈希，按权重累积分布做稳定分桶（同一单元永不重新分桶）。
  - `branch_stats()` / `compare_branches()`：将原始计数转换为参与率/中奖率/失败率/风险率，样本量 < `DEFAULT_MIN_SAMPLES`(8) 时返回 `insufficient_data`，从不强行宣布获胜分支。
  - `should_stop_branch()`：命中验证码/封禁等硬信号立即停止；或风险事件率在 ≥`MIN_RUNS_FOR_RISK_RATE`(5) 次运行后超过 `DEFAULT_RISK_RATE_THRESHOLD`(0.2) 时停止。
  - `experiment_guardrails()`：把安全约束（仅 dry/shadow、评论内容来自受控模板池、验证码/限流自动停止分支、结果仅建议）转成人类可读提示，供前端展示。

新增 `core/app/api/experiments.py`（前缀 `/api/experiments`）：

- `GET /` 列表（含每个实验的分支）；`POST /` 创建（`operator` 起，`allow_real_run=true` 需 `admin`）；`GET /{id}` 详情（分支统计、`compare_branches` 结果、`should_stop_branch` 信号、`experiment_guardrails`）；`POST /{id}/assign` 将一个抽奖单元稳定分配到分支；`POST /{id}/stop`、`POST /{id}/branches/{key}/stop`（均写 `audit_event` + `record_event`）。
- 新增数据表（`ensure_experiment_schema`）：`experiments`、`experiment_branches`、`experiment_assignments`。

新增 `core/app/models/schemas.py` 请求体：`ExperimentBranchCreate`、`ExperimentCreate`、`ExperimentAssignRequest`、`ExperimentStopRequest`、`ExperimentBranchStopRequest`。

测试：`core/tests/test_experiment_engine.py`，26 项，覆盖校验规则、哈希分桶的稳定性与权重分布、统计比率、领先分支判定的样本门限、自动停止的硬/软信号、guardrails 文案。

### 2. S4 / V6 — Risk Intelligence（风险情报）

新增 `core/app/risk/`：

- `intelligence.py`（无 DB 依赖）：
  - `RECOMMENDED_ACTIONS = (allow, dry_run_only, cooldown, relogin, manual_review, block)`，按保守程度排序。
  - `tighten_action(baseline, recommended)`：返回两者中更保守的一个 —— 风险情报**只能收紧**，永不放宽。
  - `forecast_account_risk()`：综合信誉分、24h/7d 风险事件数、连续失败、账号状态、最近一次校准结果、当日任务量，输出 0-100 的 `risk_score`、0-1 的 `forecast_24h`（24 小时风险概率，封顶 0.95）、`recommended_action`（取自安全集合）、`reasons`、`forecast_band`（low/moderate/elevated/high）。
  - `_recommend_action()`：按“封禁→冻结→硬信号/高频风险→需要重新登录→近期风险事件→校准失败→连续失败或低信誉→allow”的严格优先级映射，保证推荐总是最严格适用的一条规则。

新增 `core/app/api/risk_intel.py`（前缀 `/api/risk`，只读）：

- `GET /intelligence`：按 `risk_score`/`forecast_24h` 倒序列出全部账号的预测与推荐动作，附 `bands` 汇总；`GET /intelligence/{account_id}`：单账号详情。
- 聚合 SQL 复用 `accounts`、`task_runs`、`risk_events`、`account_calibrations`；硬风险信号通过 `HARD_RISK_REGEX = "captcha|ban|banned|verification|frozen|security"` 与 `LOWER(event_type) REGEXP :hard_regex` 匹配（而非要求事件类型与关键词完全相等）。

测试：`core/tests/test_risk_intelligence.py`，15 项，覆盖 `tighten_action` 的单调性、各风险因子对 `risk_score`/`forecast_24h` 的贡献与封顶、`forecast_band` 边界、`_recommend_action` 的优先级顺序。

### 3. S5（前半）/ V6.5 — Learning Runtime（学习运行时）

新增 `core/app/learning/`：

- `feature_store.py`（无 DB 依赖）：`FEATURE_SET_VERSION = "fs-1.0"`；`extract_features()` 把目标价值分、平台胜率、知识置信度、账号信誉、dry/shadow 验证次数、近期平台风险，归一化为 `[0,1]` 的 6 维特征向量（`value`、`win_rate`、`knowledge_confidence`、`account_reputation`、`validation_depth`、`recent_risk`），并保留 `raw` 原始输入用于审计；`FEATURE_DESCRIPTIONS` 提供每个特征的中文/英文可解释说明。
- `model.py`：`MODEL_VERSION = "lm-1.0"`；手工设定的线性-逻辑回归基线（`BIAS=-2.2` + 6 个具名权重），`predict_success_probability()` 输出概率、logit、各特征贡献度与 `confidence_band`（high/moderate/low/very_low）；`explain_prediction()` 在此基础上按贡献度绝对值排序得到 `top_drivers`。模型未使用任何隐藏训练数据，权重写死且有文档说明，确保完全可解释、可复现。

新增 `core/app/api/learning.py`（前缀 `/api/learning`，只读）：

- `GET /model`：模型卡片（版本、特征集版本、偏置、权重、特征说明、`advisory_only: true`）。
- `GET /predictions`：对 `pending`/`claimed` 状态目标按 `value_score` 取前 N 个，复用 `STRATEGY_TARGET_METRICS_SQL`、`load_strategy_platform_knowledge`、`load_strategy_account_recommendations`（均来自 `app.api.lotteries`），抽取特征并输出概率、置信带与 `top_drivers`。

测试：`core/tests/test_learning.py`，14 项，覆盖特征归一化边界与饱和、`validation_depth` 的 dry/shadow 权重差异、`predict_success_probability` 的 sigmoid 边界与 `confidence_band` 阈值、`explain_prediction` 的排序正确性、模型版本/特征集版本的稳定性。

### 4. S5（后半）/ S6 / V7 — Governance Runtime（治理运行时）

新增 `core/app/governance/`：

- `policy.py`（无 DB 依赖）：
  - `DEFAULT_REAL_RUN_POLICY`（`policy_key="real_run_gate"`, `version=1`）：把既有 real-run 门禁的 9 个证据项（`global_real_run_enabled`、`circuit_breaker_closed`、`valid_lottery_target`、`action_plan_reviewed`、`calibrated_account_available`、`real_adapter_enabled`、`recent_complete_probe`、`recent_shadow_run`、`no_recent_account_risk`）表达为带 `remediation` 的有序 gate 列表 —— 这是对**现状的制度化**，不是新增限制。
  - `validate_policy()`：校验 `policy_key`/`version`/gate 唯一性/至少一个 gate。
  - `evaluate_policy()`：**fail-closed** —— 任一必需 gate 的输入缺失或为假即判定 `block`；只有全部满足才 `allow`；`next_action` 取第一个失败 gate 的 `remediation`。
  - `build_decision_record()` / `replay_decision()`：构建可持久化的决策记录，并用记录中的 `inputs` 重新跑一遍同版本策略，验证 `recomputed_outcome == recorded_outcome`（决策可回放）。
  - `diff_policies()`：结构化对比两个策略版本的新增/移除 gate 与 `required` 变化，供未来策略版本评审使用。

新增 `core/app/api/governance.py`（前缀 `/api/governance`）：

- `GET /policy`：当前生效策略（DB 中 `active=1` 的最新版本，否则回退到 `DEFAULT_REAL_RUN_POLICY`）。
- `GET /policies`：策略版本列表（无记录时回退展示内置默认版本）。
- `GET /real-run/{lottery_id}`：把现有 `real_run_gate_status()` + `circuit_breaker_allows()` + `is_real_run_enabled()` 的结果映射为 gate 输入，跑 `evaluate_policy()` 并默认写入 `policy_decisions`（`record=false` 可跳过）。**只读评估，从不派发、从不放宽门禁。**
- `GET /decisions`：决策日志（可按 `subject_id` 过滤）。
- `POST /decisions/{decision_id}/replay`：取出历史决策的 `inputs`，用当前生效策略重放并对比结果。
- 新增数据表（`ensure_governance_schema`）：`policy_versions`、`policy_decisions`。

测试：`core/tests/test_governance_policy.py`，15 项，覆盖策略校验规则、fail-closed（输入缺失即 block）、单个 gate 失败即 block、`next_action` 取首个失败 gate 的 remediation、allow/block 决策的回放一致性、策略 diff。

### 5. 集成与路由注册

`core/app/main.py`：

- 导入新增四个 API 模块：`experiments, risk_intel, learning, governance`。
- `ensure_runtime_schema()` 内调用新增的 `ensure_experiment_schema()` 与 `ensure_governance_schema()`（均为 `CREATE TABLE IF NOT EXISTS`，幂等迁移）。
- 注册路由：`/api/experiments`、`/api/risk`（risk-intel）、`/api/learning`、`/api/governance`。

## 验证记录

验证时间：2026-06-12

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 新增纯模块独立导入 | 通过 | `app.experiment.engine` / `app.risk.intelligence` / `app.learning.feature_store` / `app.learning.model` / `app.governance.policy` 均无 DB/FastAPI 依赖 |
| core 单元测试 | 通过 | `python -m unittest discover -s tests`：**164 项全部通过**（原 94 + 新增 26 + 15 + 14 + 15） |
| `py_compile` / `ast.parse` | 通过 | 全部新增/改动 Python 文件语法检查通过 |
| `app.main` 完整导入 | 通过 | 安装依赖后 `import app.main` 成功，新增 14 条路由全部注册（`/api/experiments/*`、`/api/risk/intelligence*`、`/api/learning/*`、`/api/governance/*`） |
| 行为保持检查 | 通过 | 未修改任何既有 API 字段语义；新增模块均为附加路由与表 |
| 安全边界检查 | 通过 | 4 个新模块均为只读评估/建议/统计；无一处绕过或放宽 real-run 门禁、断路器或证据要求 |

## 安全边界

- **Experiment Runtime**：实验默认仅允许 `dry_run`/`shadow_run`；`real_run` 实验需管理员显式 `allow_real_run` 且分发时仍走完整 real-run 门禁；命中验证码/封禁/限流信号自动停止对应分支并记录事件；评论类分支的内容来自受控模板池（由 `experiment_guardrails` 在前端可见地声明，而非隐藏约束）。
- **Risk Intelligence**：推荐动作严格取自 `RECOMMENDED_ACTIONS` 安全集合；`tighten_action` 保证风险预测只能让派发更保守；接口全部只读，不修改账号状态或门禁。
- **Learning Runtime**：模型权重为手工设定、写入文档，无隐藏训练数据；每条预测都带 `model_version`/`feature_version` 与可解释的 `top_drivers`；输出明确标记 `advisory: true`，从不替代 real-run 证据门禁、断路器或管理员确认。
- **Governance Runtime**：策略评估 fail-closed —— 必需 gate 缺失或为假即 `block`；`DEFAULT_REAL_RUN_POLICY` 是对现有门禁证据的制度化表达而非新约束；决策记录可回放（同版本同输入必得到同结果）；评估接口从不派发、从不放宽门禁。

## 已知限制

- 4 个新模块的数据访问路径（`experiments`/`risk_intel`/`learning`/`governance` 的真实查询）依赖部署环境的 MySQL/Redis，本次只能在无 DB 环境验证纯逻辑与路由注册；带数据库的端到端联调留待本机部署环境执行。
- `policy_versions` 表当前无内置初始化数据；`GET /policy`、`GET /policies` 在表为空时回退到代码内 `DEFAULT_REAL_RUN_POLICY`（version 1），后续若需要发布 version 2+ 需要管理端写入流程（暂未提供写接口，遵循“治理变更需显式、可审计”的原则，先以只读评估落地）。
- Experiment Runtime 暂未提供前端创建实验的表单；前端控制台（见下）聚焦于监控、对比与安全停止。

## 下一步

对应 `docs/DPMS_能力演化路线图_v4-v9_20260605.md` 第 11-12 节（V8 Transition Runtime、V9 Semantic Runtime）的展开规划见 [[DPMS_V8-V9_后续版本规划_20260612]]（`docs/DPMS_V8-V9_后续版本规划_20260612.md`）。

前端：新增 `Experiments.jsx` / `RiskIntelligence.jsx` / `Learning.jsx` / `Governance.jsx` 四个控制台页面，延续 S2 的 `panel`/`data-table`/`drawer` 布局与中英文 i18n。

## 对应 Git 提交

- `02a8d99 Add Experiment, Risk Intelligence, Learning and Governance runtimes (V5.5-V7)`
