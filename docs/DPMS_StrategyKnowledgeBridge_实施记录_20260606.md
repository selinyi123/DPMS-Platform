# DPMS Strategy Knowledge Bridge 实施记录 20260606

## 背景

上一阶段已经实现 `Knowledge Runtime`，可以从事件、任务、账号、风险和抽奖结果中提炼平台经验与账号画像。本次变更把这些经验接入 `Strategy Runtime` 的策略队列，使调度建议不再只依赖当前状态，而是开始使用历史经验。

## 已实现范围

- `/api/lotteries/strategy/queue` 新增经验感知字段。
- 策略评分接入平台知识置信度、历史中奖率、推荐账号信誉、估算期望值。
- 自动选号逻辑优先选择信誉分更高、风险更低、负载更轻的校准账号。
- 前端策略队列显示期望值、估算中奖率、推荐账号、账号信誉和平台知识置信度。
- 新增中英文原因码映射。

## 策略队列新增字段

参数名 | 类型 | 必填 | 默认值 | 说明
--- | --- | --- | --- | ---
`expected_value` | number | 是 | `0` | 基于目标价值、估算中奖概率和信任分计算的期望值
`estimated_win_probability` | number | 是 | `0.05` | 历史中奖率经置信度收缩后的估算中奖概率
`trust_score` | number | 是 | `0.35` | 基于推荐账号信誉、近期平台风险和知识置信度计算的信任分
`platform_knowledge` | object | 是 | `{}` | 平台经验画像，包含样本数、中奖率、任务成功率、风险事件和置信度
`recommended_account` | object/null | 否 | `null` | 推荐账号及其信誉、风险、运行历史

## 评分逻辑

策略分由以下因子叠加：

- 原始目标价值 `value_score`。
- 当前建议模式系数：`real_run` > `shadow_run` > `dry_run` > `blocked`。
- `dry_run` 与 `shadow_run` 成功验证奖励。
- 平台知识置信度奖励。
- 推荐账号信誉奖励。
- 期望值奖励。
- 估算中奖概率奖励。
- 近期平台风险与失败运行惩罚。

## 估算中奖概率

历史中奖率不会直接使用，而是用基础概率进行收缩：

```text
estimated_win_probability
= baseline * (1 - confidence_weight)
+ historical_win_rate * confidence_weight
```

其中：

- `baseline = 0.05`
- `confidence_weight <= 0.75`

这样可以避免少量样本导致系统对 `win_rate = 1.0` 过度自信。

## 推荐账号逻辑

推荐账号必须满足：

- `status = ready`
- 存在加密凭据
- 最新账号校准为 `succeeded`

排序优先级：

1. `reputation_score` 高。
2. `daily_task_count` 低。
3. 近期风险事件少。
4. 失败运行少。

## 安全边界

- 本次变更不会自动执行真实任务。
- `real_run` 仍受全局开关、熔断器、适配器探针、管理员权限和确认头控制。
- 验证码、风控、审核和封禁信号只用于降频、冷却、复核和风险预警。
- 未实现或放松任何规避验证码、绕过平台保护机制的能力。

## 验证记录

- `py_compile core/app/api/lotteries.py` 通过。
- `npm run build` 通过。
- `docker compose up -d --build core-api nginx` 通过。
- `GET /api/health` 返回 `ok`。
- 临时验证目标触发策略队列返回：
  - `expected_value`
  - `estimated_win_probability`
  - `trust_score`
  - `recommended_account`
  - `platform_knowledge`
  - `reason_codes`

