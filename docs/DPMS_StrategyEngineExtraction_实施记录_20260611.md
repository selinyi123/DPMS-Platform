# DPMS_StrategyEngineExtraction_实施记录_v1.0_20260611

## 背景

`docs/DPMS_总设计方案_v1_20260611.md` 将后续开发的第一阶段（S1）定为「Strategy Runtime 模块化 + 工程质量基线」。

此前的决策层（V5 Strategy Runtime）以建议级形式存在：`GET /api/lotteries/strategy/queue` 已经在做期望值评分、执行模式选择和账号信誉排序，但相关纯逻辑（模式选择、评分、期望值/信任度估计）全部内联在 `core/app/api/lotteries.py` 中。由于该文件在模块顶部 `from app.db import database`，这些纯函数无法在没有数据库的环境下被导入和单测。

同样，知识评分函数（`account_reputation`、`confidence_score`、`build_data_maturity`、`build_learning_gaps` 等）内联在 `core/app/knowledge/service.py`，而该文件同样依赖 `app.db`，导致这部分评分数学无法独立测试。

此外，Worker 侧长期没有任何自动化测试（历次实施记录反复标注的工程缺口）。

本次目标：把决策与评分的纯逻辑抽取为无 DB 依赖的独立模块，补齐 core 与 worker 的单元测试基线，并引入路线图 §6.3 的账号分层与活动优先级分层。**不改变任何既有 API 字段语义，仅新增字段。**

## 改动内容

### 1. 新增纯策略引擎 `core/app/strategy/`

- `core/app/strategy/engine.py`（无 DB / Redis / FastAPI 依赖）：
  - 抽取自 `lotteries.py`：`choose_strategy_mode`、`strategy_score`、`estimate_win_probability`、`estimate_trust_score`、`strategy_knowledge_confidence`、`empty_platform_knowledge`、`first_or_none`，以及内部 `as_int` / `clamp`。
  - 新增 `priority_tier(strategy_score) -> "S"|"A"|"B"|"hold"`：活动优先级分层。
  - 新增 `account_tier(reputation_score) -> "S"|"A"|"B"|"watch"`：账号分层（对齐路线图 §6.3）。
  - 模块 docstring 写明安全边界：引擎只建议模式与排序，不越过 real-run 门禁；`real_run` 仅在上游门禁全部满足时才会被建议。
- `core/app/strategy/__init__.py`：统一导出上述函数。

### 2. 新增纯知识评分 `core/app/knowledge/scoring.py`

- 抽取自 `service.py`：`build_data_maturity`、`build_learning_gaps`、`gap`、`account_reputation`、`confidence_score`、`ratio`、`as_int`、`as_float`、`clamp`。
- 逻辑逐字保留，未改变数值公式。

### 3. 行为保持式重构

- `core/app/knowledge/service.py`：改为 `from app.knowledge.scoring import (...)` 并通过 `__all__` 重新导出，删除内联的评分函数定义。`from app.knowledge.service import account_reputation`（在 `lotteries.py` 中使用）仍然有效。
- `core/app/api/lotteries.py`：改为 `from app.strategy.engine import (...)`，删除内联的 7 个策略函数与已无引用的 `clamp` 定义；保留 `as_int` / `as_float` / `safe_ratio`（文件内其余约 30 处仍在使用）。

### 4. 新增字段（仅新增，不改语义）

- `strategy/queue` 每条新增 `priority_tier`（`blocked` 目标固定为 `hold`）。
- `load_strategy_account_recommendations` 的账号建议新增 `account_tier`。

### 5. 测试基线

- `core/tests/test_strategy_engine.py`（新增）：模式选择优先级（active_run → no_account → breaker → dry → shadow → real）、real-run 开关关闭时绝不返回 real_run、评分单调性与下界、风险扣分封顶、期望概率混合与置信度封顶、信任度边界、分层阈值与单调性等。
- `core/tests/test_knowledge_scoring.py`（新增）：账号信誉边界与排序、置信度封顶、数据成熟度分级与上界、学习缺口 P0 触发与上限 8 项、数值工具函数。
- `worker/tests/__init__.py` + `worker/tests/test_adapter_config.py`（新增，worker 首个测试集）：`selector_values` / `click_selectors` / `decode_b64` / `load_selector_config`（含 Base64 优先、错误 JSON 降级）/ `has_complete_selectors`（四个结构化平台完整性、评论需 input+submit、缺阶段判负）等。

## 验证记录

验证时间：2026-06-11

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| core 纯模块独立导入 | 通过 | 无数据库环境下成功 `import app.strategy.engine` 与 `app.knowledge.scoring` |
| core 单元测试 | 通过 | `python -m unittest discover -s tests`：94 项全部通过（原 44 + 新增 50） |
| worker 单元测试 | 通过 | `python -m unittest discover -s tests`：21 项全部通过（worker 首个测试集） |
| `py_compile` | 通过 | 全部新增/改动 Python 文件编译通过 |
| 行为保持检查 | 通过 | `from app.knowledge.service import account_reputation` 仍可用；既有 API 字段未删改 |
| 字段新增检查 | 通过 | `strategy/queue` 新增 `priority_tier`，账号建议新增 `account_tier` |

## 安全边界

- 本次未触发任何真实执行，未开启 real-run 总开关，未保存任何平台运行时选择器。
- 策略引擎 docstring 明确：引擎只建议模式与排序，real-run 仍由 API 与 worker 的门禁强制执行。
- 测试 `test_real_run_disabled_never_yields_real_run` 固化了关键安全属性：全局开关关闭时绝不产生 real_run 建议。

## 已知限制

- 带数据库的查询路径（`strategy_queue`、`load_strategy_*`、`build_*_profiles`）仍需在部署环境联调；本次只能在无 DB 环境验证纯逻辑。
- `priority_tier` / `account_tier` 暂未在前端展示（计划在总方案 S2 阶段加入 Strategy/Knowledge 页面）。

## 下一步（对应总方案 S2）

1. 新增 `GET /api/lotteries/{id}/strategy/explain` 单目标评分解释接口。
2. 新增前端 `Strategy.jsx`（按 `priority_tier` 分组）与 `Knowledge.jsx`（数据成熟度与学习缺口）。
3. 在解释抽屉中展示评分构成，保持“仅建议、人工确认 real-run”。

## 对应 Git 提交

- `1a56aec Extract Strategy Runtime engine and add test baseline`
- 时间线见 [[DPMS_活动时间线]] 2026-06-11 条目。
