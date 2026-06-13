# DPMS_V8_TransitionRuntime_实施记录_v1.0_20260613

## 背景

`docs/DPMS_V8-V9_后续版本规划_20260612.md` 将 S7 / V8 定义为 **Transition Runtime（策略迁移图）**：在 V7 Policy Object 的基础上，把"策略版本如何演化"本身变成一条可审计、可回放、永不静默放宽的记录链 —— 每一次版本变更都要回答"为什么改、改了什么、影响谁、如何回退、是收紧还是放宽"。

本次实施落地 V8 的全部范围：纯逻辑迁移引擎、`policy_transitions` 审计表、发布/激活两步式管理端点、迁移记录只读 API，以及 `TransitionGraph.jsx` 前端页面。延续 S1-S7 确立的"纯逻辑模块 + 单元测试 + 薄 API 层 + `ensure_*_schema()` 幂等迁移 + 前端页面"模式，**不修改任何既有 API 字段语义，仅新增模块与端点**。

## 改动内容

### 1. 纯逻辑模块 `core/app/transition/engine.py`（无 DB 依赖）

- `TRANSITION_REASON_CODES = (experiment_result, risk_tightening, manual_review, incident_response, scheduled_review)`。
- `POLICY_SUBJECTS = {"real_run_gate": "lottery"}`：策略键到受影响主体类型的映射，供 `impact_scope` 使用。
- `classify_transition(diff)`：把一次策略 diff 分类为 `tightening`/`loosening`/`neutral`。**放宽优先于收紧**——只要 `removed_gates` 非空，或任意 gate 的 `required` 由 `True` 变为 `False`，整个变更即被判定为 `loosening`，即便同一次变更中也新增了必需 gate。
- `validate_transition(*, from_policy, to_policy, reason_code, rollback_condition, loosening_justification=None)`：校验版本号必须严格 `+1`（不允许跳号/回填）；`reason_code` 必须在 `TRANSITION_REASON_CODES` 中；`rollback_condition` 不能为空；若分类为 `loosening`，则 `loosening_justification` 必填，且 `reason_code` 不能是 `scheduled_review`（"定期复核"不能用来掩盖一次主动放宽）。
- `impact_scope(policy_key, diff)`：基于 `POLICY_SUBJECTS` 与受影响 gate code，返回 `{subject_type, affected_gates, description}`；未知 `policy_key` 返回空影响面而非报错（lineage/diff 本身仍然有意义）。
- `rollback_plan(transition_record)`：草拟回到 `from_version` 的逆向迁移；草案本身仍需 `validate_transition` 校验后才可发布，回退从不自动生效。

### 2. `core/app/governance/policy.py` 的最小扩展

`diff_policies(old, new)` 新增两个键，供 `classify_transition` 区分"更严"与"更松"而无需重新读取完整策略对象：

- `added_gates_required`：新增 gate -> 新版本中的 `required` 布尔值。
- `changed_required_to`：`required` 发生变化的 gate -> 新版本中的 `required` 布尔值。

既有字段（`added_gates`/`removed_gates`/`changed_required`/`from_version`/`to_version`）不变，`core/tests/test_governance_policy.py` 15 项测试全部仍通过。

### 3. 数据表 `policy_transitions`（`ensure_governance_schema()`，幂等）

```sql
CREATE TABLE IF NOT EXISTS policy_transitions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  policy_key VARCHAR(64) NOT NULL,
  from_version INT NOT NULL,
  to_version INT NOT NULL,
  reason_code VARCHAR(32) NOT NULL,
  classification VARCHAR(16) NOT NULL,
  trigger_event VARCHAR(128) NULL,
  experiment_id CHAR(36) NULL,
  diff JSON NOT NULL,
  impact_scope JSON NULL,
  rollback_condition TEXT NOT NULL,
  loosening_justification TEXT NULL,
  requires_extra_review TINYINT NOT NULL DEFAULT 0,
  activated_at TIMESTAMP NULL,
  created_by VARCHAR(128) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_policy_transition_key (policy_key, to_version)
) ENGINE=InnoDB
```

同时为 `policy_versions` 补了一条幂等种子迁移（`ON DUPLICATE KEY UPDATE policy_key = policy_key`），把 `DEFAULT_REAL_RUN_POLICY`（version 1）写为 `active=1` 的种子行，使第一次发布有真实的 `from_version=1` 可供 diff，而不必特判"无历史"分支。

### 4. 治理 API 新增两个管理端点（`core/app/api/governance.py`）

- **`POST /api/governance/policies`**（`publish_policy_version`，`admin` + 二次确认）：校验 `definition`（`validate_policy`）与 `policy_key` 一致，加载当前 active 策略作为 `from_policy`，调用 `validate_transition`；版本已存在则 409。通过后：
  1. 以 `active=0` 写入 `policy_versions`（**发布 ≠ 生效**，评估行为不变）；
  2. 写入一条 `policy_transitions` 记录（`diff`、`classification`、`impact_scope`、`requires_extra_review = classification == "loosening"`）；
  3. 记录 `audit_event`（`governance.policy_publish`，放宽变更 `risk_level=high`）与事件溯源 `PolicyVersionPublished`。
  返回 `from_version`/`to_version`/`classification`/`diff`/`impact_scope`/`requires_extra_review`。

- **`POST /api/governance/policies/{version}/activate`**（`activate_policy_version`，`admin` + 二次确认）：要求目标版本存在且**已有对应的 `policy_transitions` 记录**（否则 409，提示先 `POST /policies` 发布）；已是 active 则返回 `already_active`；否则将旧 active 置 0、新版本置 1，并把该迁移记录的 `activated_at` 置为 `NOW()`，记录 `audit_event`（`governance.policy_activate`，`risk_level=high`）与 `PolicyVersionActivated` 事件。

新增请求体 `PolicyPublishRequest`、`PolicyActivateRequest`（`core/app/models/schemas.py`）。

### 5. 只读迁移 API `core/app/api/transitions.py`（前缀 `/api/transitions`）

- `GET /?policy_key=real_run_gate`：按 `to_version ASC` 返回该 `policy_key` 的全部迁移记录（`diff`/`impact_scope` 反序列化为对象，`requires_extra_review` 转为布尔）。
- `GET /{policy_key}/lineage`：返回 `{policy_key, active_version, chain}`，`active_version` 取自 `policy_versions`（`active=1`），`chain` 即上面的迁移记录列表 —— 前端据此画出 `v1 -> v2 -> v3 -> ...` 的完整链路，且每一跳都带 diff 摘要。

### 6. 前端 `frontend/src/pages/TransitionGraph.jsx`

- 时间线视图：`v{from} -> v{to}` 链路，每条迁移边显示 `classification`（颜色区分：tightening=绿、loosening=红、neutral=灰）、`reason_code`、`created_at`；`requires_extra_review` 的迁移额外标红"需额外复核"。当前 active 版本节点高亮。点击迁移边展开 `diff`（新增/移除 gate 及 `required` 变化）与 `impact_scope` 描述。
- 管理表单：基于当前 active 策略一键生成下一版本 JSON 草稿（"基于当前生效策略生成"按钮，版本号自动 +1）；填写 `reason_code`（下拉框，限定 `TRANSITION_REASON_CODES`）、`rollback_condition`、可选 `loosening_justification`、`note`；提交 -> `POST /governance/policies`（二次确认）-> 展示返回的 `diff`/`classification`/`impact_scope`/`requires_extra_review`。
- 每个"已发布未激活"的版本节点单独展示"激活"按钮 -> `POST /governance/policies/{version}/activate`（二次确认）——发布与激活在 UI 上是两个独立、都需二次确认的动作。
- 顶部安全提示横幅："发布不等于生效；任何放宽门禁的变更都必须填写书面理由，并会被标记为需要额外复核"。
- `frontend/src/App.jsx` 注册导航项 `transitions`；`frontend/src/uiContext.jsx` 新增 `nav.transitions` 与完整 `transitions` 中英文命名空间；`frontend/src/index.css` 新增 `.transition-chain`/`.transition-node`/`.transition-edge-group`/`.transition-edge` 布局样式。

## 设计取舍：每次版本递增对应一条迁移记录（而非两条）

`docs/DPMS_V8-V9_后续版本规划_20260612.md` 的建议数据结构暗示"发布"与"激活"可能分别写入迁移信息。本次实现选择**每次版本号 `+1` 只在发布时写入一条 `policy_transitions` 行**，激活动作仅在该既有行上补写 `activated_at = NOW()`，不新增第二行。

理由：若发布与激活各写一行，`v(N) -> v(N+1)` 这条边在 lineage 中会出现两条记录，重建"当前 active 版本是怎么从 v1 走过来的"链路时需要额外去重/合并逻辑，且两行的 `diff`/`classification` 完全相同，徒增歧义。单行 + `activated_at` 可空字段已经能完整表达"已发布但未激活"（`activated_at IS NULL`）与"已激活"（`activated_at` 非空）两种状态，`GET /transitions/{policy_key}/lineage` 按 `to_version ASC` 即可还原完整链路，且每一跳的 diff/分类唯一不重复。验收标准（发布不影响线上行为、激活后旧决策仍用旧版本重放、放宽变更必须留痕并标记复核、lineage 可完整还原）均满足。

## 验证记录

验证时间：2026-06-13

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 新增纯模块独立导入 | 通过 | `app.transition.engine` 无 DB/FastAPI 依赖 |
| core 单元测试 | 通过 | `python -m unittest discover -s tests`：**183 项全部通过**（原 164 + 新增 19） |
| `ast.parse` 语法检查 | 通过 | `main.py`/`governance.py`/`transitions.py` 均通过 |
| `app.main` 完整导入 | 通过 | 设置 `ENCRYPTION_KEY` 后 `import app.main` 成功，共 93 条路由，含新增 `/api/transitions/`、`/api/transitions/{policy_key}/lineage`、`/api/governance/policies`（GET+POST）、`/api/governance/policies/{version}/activate` |
| 前端构建 | 通过 | `npm run build` 成功，126 modules，输出至 `dashboard/dist` |
| 行为保持检查 | 通过 | 未修改任何既有 API 字段语义；新增模块均为附加路由、表与页面 |
| 安全边界检查 | 通过 | 发布/激活均要求 `admin` + 二次确认；放宽变更强制要求 `loosening_justification` 且不可用 `scheduled_review`；发布不改变 `evaluate_policy` 使用的 active 版本 |

## 安全边界

- **发布 ≠ 生效**：`POST /policies` 写入的新版本 `active=0`，`_load_active_policy()` 与既有 `GET /real-run/{lottery_id}` 评估路径完全不受影响，直到显式 `POST /policies/{version}/activate`。
- **放宽必须留痕**：`classify_transition` 把"移除 gate"或"`required: True -> False`"判定为 `loosening`（即便同一变更中也收紧了别的 gate）；`validate_transition` 强制要求非空 `loosening_justification`，且禁止用 `scheduled_review` 作为放宽的理由代码；`policy_transitions.requires_extra_review=1` 的记录在前端以醒目红色标记。
- **版本链不可跳跃**：`to_version` 必须等于 `from_version + 1`，杜绝跳号或回填历史。
- **激活前必须先发布**：`POST /policies/{version}/activate` 要求该版本已有 `policy_transitions` 记录，否则 409，杜绝绕过迁移审计直接切换 active 版本。
- **审计与事件溯源**：发布与激活均写 `audit_event`（`risk_level=high`/`medium`）与 `record_event`（`PolicyVersionPublished`/`PolicyVersionActivated`），与既有治理/实验/学习等运行时一致。
- 与 V5.5-V7 完全相同的总原则：迁移记录与 Transition Graph 是**审计与建议层**，从不直接触发 real-run、断路器或账号操作。

## 已知限制

- 当前 `POLICY_SUBJECTS` 仅登记 `real_run_gate -> lottery`；新增其他 `policy_key` 时需要同步扩展该映射，否则 `impact_scope` 会返回空影响面（不报错，但前端不会展示受影响主体）。
- 前端"基于当前生效策略生成"按钮只做版本号 `+1` 的浅拷贝草稿，gate 列表的增删改仍需管理员手工编辑 JSON；这是有意为之 —— 任何策略变更都应由人工显式审阅，而非自动生成。
- 带数据库的端到端联调（发布 -> 激活 -> 重放历史决策仍用旧版本）依赖部署环境的 MySQL；本次在无 DB 环境完成纯逻辑测试、路由注册与前端构建验证，端到端联调留待本机部署环境执行。

## 下一步

S8 / V9 Semantic Runtime（语义执行链）的展开规划见 [[DPMS_V8-V9_后续版本规划_20260612]]（`docs/DPMS_V8-V9_后续版本规划_20260612.md`），将把 `Intent -> Institution -> Policy -> Transition -> Execution` 五层在一次查询内串起来，其中 `Transition` 节点直接复用本次的 `GET /api/transitions/{policy_key}/lineage`。

## 对应 Git 提交

（本次提交）
