---
tags:
  - DPMS
  - Bilibili
  - RealRun
  - Repair
updated: 2026-06-24
---

# DPMS Bilibili 缺失动作补做实施记录

## 摘要

- 权威技术记录：`docs/DPMS_BilibiliMissingActionRepair_实施记录_20260624.md`
- 新增只读补做计划：`GET /api/lotteries/{lottery_id}/repair-plan`
- 新增补做派发：`POST /api/lotteries/{lottery_id}/repair-dispatch`
- 补做任务仍按 `real_run` 安全级别执行，但任务消息中的 `action_plan` 只包含缺失动作。
- 已完成动作从 `events` 中的 `TaskPhaseCompleted` 汇总，并且只统计 `task_mode = real_run`，避免 dry/shadow 污染真实动作历史。
- 前端活动池显示“已完成动作 / 缺失动作”，并在存在缺失动作时显示“补做缺失”按钮。

## L72 当前状态

- 完整规则计划：`followed / liked / commented / reposted`
- 已完成真实动作：`followed / reposted`
- 缺失动作：`liked / commented`
- 当前阻断：`global_real_run_disabled`

## 安全边界

本次只完成工程能力落地与只读验证，没有再次执行真实账号动作。补做派发仍受管理员确认、全局 real-run 开关、账号安全、熔断器、证据门禁和 Governance policy 约束。
