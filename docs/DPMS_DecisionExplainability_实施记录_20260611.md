# DPMS_DecisionExplainability_实施记录_v1.0_20260611

## 背景

`docs/DPMS_总设计方案_v1_20260611.md` 将 S2 定为「决策可解释 + 前端 Strategy/Knowledge 页面」：在 S1 抽取出的纯策略引擎（`priority_tier`/`account_tier`/评分与概率估计）基础上，补齐单目标的「为什么」解释接口，并把 S1 已有但仅存在于 API 字段中的分层信息，前移到可操作的运营页面。

## 改动内容

### 1. 纯逻辑：评分构成拆解

`core/app/strategy/engine.py` 新增一组 `*_breakdown` 函数（与既有 `strategy_score`/`estimate_win_probability`/`estimate_trust_score`/`strategy_knowledge_confidence` 数值完全一致，但额外返回各分量）：`strategy_score_breakdown`、`estimate_win_probability_breakdown`、`estimate_trust_score_breakdown`、`strategy_knowledge_confidence_breakdown`。**行为保持**：原函数数值与新函数 `total`/`blended`/`trust_score`/`confidence` 字段一一对应。

### 2. API：单目标解释端点

`core/app/api/lotteries.py`：

- 新增 `STRATEGY_TARGET_METRICS_SQL` 共享 SQL 片段（`safe_accounts`/`recent_platform_risk`/`active_runs`/`dry_success`/`shadow_success`/`failed_runs`/`latest_probe_result` 子查询，按 `l.platform`/`l.id` 关联），供 `strategy_queue` 与新解释端点共用，避免逻辑漂移。
- 新增 `compute_strategy_item(item, *, selector_config, real_run_enabled, platform_knowledge, account_recommendations, include_breakdown=False)`：单目标的完整评分/门禁逻辑，`GET /strategy/queue` 与新端点共用同一份计算。
- 新增 `GET /{lottery_id}/strategy/explain`：返回 `compute_strategy_item(..., include_breakdown=True)`，附加 `explain` 块（`mode`/`score`/`win_probability`/`trust_score`/`knowledge_confidence` 的逐项构成）。
- 删除 `strategy_queue` 中已被 `compute_strategy_item` 取代的重复内联逻辑与未使用导入（`estimate_trust_score`/`estimate_win_probability`/`strategy_score`）。

### 3. 前端：Strategy / Knowledge 控制台

- `frontend/src/pages/Strategy.jsx`：策略队列按 `priority_tier`（S/A/B/hold）分组展示，每行可打开解释抽屉，抽屉内渲染评分构成、胜率混合、信任度构成、知识置信度构成与门禁阻塞原因；底部固定「仅建议，real-run 仍需人工确认」安全提示。
- `frontend/src/pages/Knowledge.jsx`：数据成熟度卡片、平台/账号画像表（含 `ConfidenceBar` 置信度条）、学习缺口清单，数据来自 `/knowledge/summary`。
- `frontend/src/App.jsx` 注册 `strategy`/`knowledge` 两个页面；`frontend/src/uiContext.jsx` 新增对应中英文 i18n 命名空间与导航文案；`frontend/src/index.css` 补充 drawer/panel/kv-row/confidence-bar/gap-list 样式。

## 验证记录

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| core 单元测试 | 通过 | 新增 breakdown 函数与既有数值函数结果一致，纳入 `test_strategy_engine.py` |
| `npm run build` | 通过 | Strategy/Knowledge 页面编译通过 |
| 行为保持检查 | 通过 | `strategy/queue` 既有字段未变化，新增 `priority_tier` 已在 S1 落地，本次仅新增解释端点与页面 |

## 安全边界

- 解释端点与页面均为只读展示，不触发任何派发；`recommended_mode` 仍受 real-run 总开关、断路器与证据门禁约束，前端固定展示安全提示。

## 下一步

对应 `docs/DPMS_总设计方案_v1_20260611.md` S3-S5+，详见 [[DPMS_V5.5-V7_RuntimeExpansion_实施记录_20260612]] 与 [[DPMS_V8-V9_后续版本规划_20260612]]。

## 对应 Git 提交

- `f67ec40 Add strategy explain endpoint with shared scoring helper`
- `d11228d Add Strategy and Knowledge console pages (S2 / V5)`
