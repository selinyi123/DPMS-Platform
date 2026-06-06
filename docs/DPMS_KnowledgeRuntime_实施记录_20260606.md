# DPMS Knowledge Runtime 实施记录 v4.5-mvp 20260606

## 背景

本次变更对应路线图中的 **V4.5 Knowledge Runtime** 最小可用切片。目标不是新增真实执行能力，而是把现有 `events`、`lotteries`、`task_runs`、`risk_events`、`accounts` 等运行事实转化为可查询的经验画像。

## 已实现范围

- 新增 `core/app/knowledge/` 查询服务。
- 新增 `GET /api/knowledge/summary`。
- 首页 Dashboard 新增 **Knowledge Runtime / 知识运行时** 面板。
- 支持中英文局部文案。
- 支持浅色、深色、跟随系统主题的视觉适配。

## API

### `GET /api/knowledge/summary`

参数名 | 类型 | 必填 | 默认值 | 说明
--- | --- | --- | --- | ---
`window_days` | integer | 否 | `30` | 统计窗口，范围 `1-365`

返回核心字段：

- `summary`：数据成熟度、事件数量、结果标签数量、任务运行数量、shadow-run 证据数量、风险事件数量。
- `platform_profiles`：平台样本数、中奖率、任务成功率、风险事件、知识置信度。
- `account_profiles`：账号运行次数、失败次数、风险次数、校准状态、信誉分。
- `risk_profile`：按风险类型、平台聚合的近期风险画像。
- `lottery_profile`：按价值区间、来源类型聚合的抽奖目标画像。
- `event_profile`：高频事件类型和近期事件总量。
- `learning_gaps`：系统继续进入学习阶段前需要补齐的数据缺口。

## 数据成熟度规则

`data_maturity_score` 基于以下信号计算：

- 近期事件数。
- `won/lost` 结果标签数量。
- 任务运行数量。
- 成功 `shadow_run` 数量。
- 有数据的平台数量。
- 风险事件观察数量。

成熟度等级：

- `cold_start`：冷启动。
- `warming`：正在积累经验。
- `usable`：可辅助策略。
- `learning_ready`：可进入后续学习模型阶段。

## 安全边界

- 本次变更只读取历史数据并做聚合，不自动派发任务。
- 不绕过验证码、不规避平台保护机制。
- 风险信号用于账号安全、降频、冷却和人工复核。
- `real_run` 仍受全局开关、熔断器、账号校准、适配器探针和确认头控制。

## 与路线图的关系

本次完成的是：

```text
Execution Runtime
↓
Event Runtime
↓
Knowledge Runtime  <= 当前切片
```

后续建议顺序：

1. 将 `Knowledge Runtime` 的平台画像接入 `Strategy Runtime` 的排序权重。
2. 增加 `experiment/`，让策略 A/B/C 的结果进入同一个知识层。
3. 将账号信誉、代理信誉、设备信誉合并为 `Risk Intelligence`。
4. 基于结果标签和风险标签建立 `Feature Store`。

