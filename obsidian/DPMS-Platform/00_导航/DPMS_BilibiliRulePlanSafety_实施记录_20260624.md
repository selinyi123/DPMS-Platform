---
tags:
  - DPMS
  - Bilibili
  - RealRun
  - 安全门禁
updated: 2026-06-24
---

# DPMS Bilibili 规则计划安全修复实施记录

## 摘要

- 权威技术记录：`docs/DPMS_BilibiliRulePlanSafety_实施记录_20260624.md`
- 事故现象：L72 真实执行只完成 `关注 / 转发`，未完成 `点赞 / 评论`。
- 根因：real-run 使用了已保存旧 `action_plan`，前端识别建议未强提示“未保存”，门禁未比较规则文本与保存计划。
- Worker 修复：只有 `real_run` 消耗账号动作窗口；`dry_run` 与 `shadow_run` 不再触发账号冷却计数。
- 门禁修复：新增 `lottery_action_plan_stale`，保存计划缺少规则识别动作时阻断真实执行。
- 前端修复：规则计划展示“已保存执行计划 / 当前草稿 / 规则识别建议”，并提示草稿未保存与保存计划缺失动作。
- 本地数据：L72 规则原文与四动作计划已恢复；账号 14 因补做尝试触发 `action_window`，当前不应继续真实执行。

## 当前结论

本次不是执行器缺失点赞或评论能力，而是规则计划同步与门禁新鲜度缺口。系统已修复“旧计划悄悄进入 real-run”的问题，但仍缺少 partial repair / missing-action-only 补做工作流。

## 后续要求

1. 新增 `repair_run` 或 `missing_action_run`。
2. 用 `task_phases` 对比 `action_plan.required_actions`，只执行缺失动作。
3. 前端显示“部分完成 / 可补做 / 不可重跑”状态。
4. 对已有真实动作的抽奖禁止简单完整重跑，避免重复转发或重复交互。
