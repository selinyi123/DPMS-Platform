---
aliases:
  - DPMS Platform
  - DPMS 项目主页
tags:
  - DPMS
  - 项目管理
  - 自动化
status: active
updated: 2026-06-24
---

# DPMS-Platform

## 项目定位

DPMS 是多平台抽奖自动化与账号资产管理运行时，目标工作流为：

```text
发现活动
-> 解析规则
-> 评估价值与风险
-> 选择账号
-> 排程
-> probe / dry-run / shadow-run
-> 安全门禁
-> real-run
-> 证据保存
-> 通知
-> 复盘与策略建议
```

安全边界：反爬与账号安全仅指合规限速、账号隔离、风险识别以及触发验证码或审核时停止并通知，不包含验证码绕过、自动化隐藏或平台保护规避。

## 当前状态

- 当前运行版本：`v0.3.14-local`（V5.5–V9 认知阶梯 + V10 Scheduling / V11 Capacity / V12 Orchestration / V13 Throughput 运营规模化主线 + Bilibili API real-run 受控执行链）
- 当前优先平台：Bilibili
- 平台状态：微博、小红书、抖音均已接入选择器驱动执行链路，处于 `calibration_required` 阶段
- 本机入口：`http://localhost/`
- GitHub：`https://github.com/selinyi123/DPMS-Platform`（开发分支 `claude/code-review-ul0pqt`）
- 最近功能提交：运营规模化主线 V10–V13（Scheduling / Capacity / Orchestration / Throughput，四个只读 advisory 运行时；编排时间→资源→广度→可持续），详见 [[DPMS_V10-V13_OperationalScaling_实施记录_20260614]]
- 最近 Bilibili 执行节点：Bilibili 动态/opus 目标已接入 API real-run 通道，仍受账号校准、shadow-run、Governance gate、熔断器、全局开关和管理员确认约束，详见 [[DPMS_BilibiliApiRealRun_实施记录_20260624]]
- 最近 Bilibili 发现节点：自动发现已从抽奖合集动态扩散到 UP 源池，本地验证新增 80 个 Bilibili UP 追踪源、70 个候选目标，并完成 ID 72 shadow-run 成功验证，详见 [[DPMS_BilibiliDiscoveryExpansion_实施记录_20260624]]
- 最近 Bilibili 安全修复：修复规则识别建议与已保存执行计划不同步导致 real-run 漏动作的问题；新增 `lottery_action_plan_stale` 门禁，详见 [[DPMS_BilibiliRulePlanSafety_实施记录_20260624]]
- 最近 Bilibili 补做能力：新增 missing-action repair，只补做真实执行中缺失的动作，避免完整重跑造成重复交互，详见 [[DPMS_BilibiliMissingActionRepair_实施记录_20260624]]
- 最近 Bilibili 风险感知更新：关键词发现开始真正接入 Bilibili 搜索，real-run 门禁新增账号近期风险冷却解释，前端显示全局 real-run 开关、账号池和冷却截止时间，详见 [[DPMS_BilibiliRiskAwareDiscovery_实施记录_20260624]]
- 最近硬化：运行时可信度硬化（P0→P1→P2→生产基线：默认鉴权、派发原子化+outbox、Governance 唯一 real-run 权威、密文上下文绑定、关键事件死信、production 密钥校验+安全头+前端 auth guard+版本化迁移），详见 [[DPMS_运行时可信度硬化_实施记录_20260614]]
- 后续开发总设计方案：[[DPMS_总设计方案_v1_20260611]]；后续版本规划：[[DPMS_V8-V9_后续版本规划_20260612]]、[[DPMS_V10-V13_运营规模化_后续版本规划_20260614]]
- 测试基线：core **341 项** + worker 21 项单元测试通过
- Obsidian 知识库里程碑：`90f6236 Add Obsidian project knowledge base`
- Bilibili 官方二维码登录里程碑：`761051d Add official Bilibili QR login`
- Bilibili 动态发现与规则计划里程碑：`04e4c1d Add Bilibili discovery rule plans`
- 当前生产策略：Real-run 默认关闭，证据不完整时禁止真实执行；V7 起该门禁同时以版本化 Policy Object（`real_run_gate` v1）形式可查询、可回放；P1-4 起 Governance policy 为 real-run 唯一权威，每次真实执行绑定授权它的 `decision_id`

## 快速导航

- [[DPMS_活动时间线]]
- [[DPMS_记录规范]]
- [[DPMS_总设计方案_v1_20260611]]
- [[DPMS_能力演化路线图_v4-v9_20260605]]
- [[DPMS_最终搭建审阅与漏洞清单_20260602]]
- [[DPMS_EventStore_V4_实施记录_20260605]]
- [[DPMS_KnowledgeRuntime_实施记录_20260606]]
- [[DPMS_StrategyQueue_实施记录_20260606]]
- [[DPMS_BilibiliTargetValidation_实施记录_20260609]]
- [[DPMS_BilibiliOfficialQrLogin_实施记录_20260609]]
- [[DPMS_BilibiliDiscoveryRulePlan_实施记录_20260609]]
- [[DPMS_BilibiliApiRealRun_实施记录_20260624]]
- [[DPMS_BilibiliDiscoveryExpansion_实施记录_20260624]]
- [[DPMS_BilibiliRulePlanSafety_实施记录_20260624]]
- [[DPMS_BilibiliMissingActionRepair_实施记录_20260624]]
- [[DPMS_BilibiliRiskAwareDiscovery_实施记录_20260624]]
- [[DPMS_ObsidianKnowledgeBase_实施记录_20260609]]
- [[DPMS_BilibiliGateReviewCleanup_实施记录_20260611]]
- [[DPMS_WeiboXiaohongshuLotteryModule_实施记录_20260611]]
- [[DPMS_AllPlatformLotteryRules_实施记录_20260611]]
- [[DPMS_DeployReadinessGeneralization_实施记录_20260611]]
- [[DPMS_StrategyEngineExtraction_实施记录_20260611]]
- [[DPMS_DecisionExplainability_实施记录_20260611]]
- [[DPMS_V5.5-V7_RuntimeExpansion_实施记录_20260612]]
- [[DPMS_V8-V9_后续版本规划_20260612]]
- [[DPMS_V8_TransitionRuntime_实施记录_20260613]]
- [[DPMS_V9_SemanticRuntime_实施记录_20260613]]
- [[DPMS_V10-V13_运营规模化_后续版本规划_20260614]]
- [[DPMS_V10-V13_OperationalScaling_实施记录_20260614]]

## 文档分区

| 目录 | 内容 |
| --- | --- |
| `00_导航` | 项目主页、活动时间线、记录规范 |
| `01_项目基线` | README、能力路线图、历史恢复资料 |
| `02_架构演进` | Event、Knowledge、Strategy Runtime |
| `03_工作流与运行时` | Shadow-run、回滚与执行工作流 |
| `04_Bilibili里程碑` | Bilibili 安全门禁、探针、向导和目标校验 |
| `05_运维安全与前端` | 通知、主题、运行态运维能力 |
| `06_审查与风险` | 工程审查、漏洞清单 |
| `07_文档与治理` | Obsidian 迁移及后续文档治理记录 |
| `99_附件` | 非 Markdown 历史片段 |

## 当前下一步

后续开发以 [[DPMS_总设计方案_v1_20260611]]、[[DPMS_V8-V9_后续版本规划_20260612]] 与 [[DPMS_V10-V13_运营规模化_后续版本规划_20260614]] 为准。阶段路线（S1–S8 认知阶梯，S9–S12 运营规模化主线）：

- S1（已完成）：Strategy Runtime 模块化 + 工程质量基线（策略引擎抽取、core/worker 测试基线、活动/账号分层）。
- S2（已完成）：决策可解释接口 + 前端 Strategy/Knowledge 页面。
- S3（已完成 / V5.5）：Experiment Runtime 最小闭环（仅 dry/shadow，自动停止 + guardrails）。
- S4（已完成 / V6）：Risk Intelligence（账号信誉聚合 + 24h 风险预测，`tighten_action` 只能收紧）。
- S5 前半（已完成 / V6.5）：Learning Runtime（Feature Store + 透明概率模型，advisory-only）。
- S6（已完成 / V7）：Governance Runtime（real-run 门禁制度化为可版本化、可回放的 Policy Object）。
- S7（已完成 / V8）：Transition Runtime（策略迁移图 / 制度血统，发布与激活分离，宽松化变更强制留痕）。
- S8（已完成 / V9）：Semantic Runtime（Intent → Institution → Policy → Transition → Execution 语义执行链，纯只读聚合解释层，不新增数据表；已泛化至 lottery + account + task 三类主体）。
- S9（已完成 / V10）：Scheduling Runtime（合规限速/日上限/最小间隔/时间窗下的只读排程计划；`respects_rate_limit` 保证不越限）。
- S10（已完成 / V11）：Capacity Runtime（供给侧建模 + 可持续上限 + 一账号一代理绑定建议；`isolation_violations` 检测共享隔离风险）。
- S11（已完成 / V12）：Orchestration Runtime（跨平台批量波次 + 强制 dry→shadow→real 安全爬坡；real 波次仅含门禁已记录放行项；草案惰性入 `campaign_plans`）。
- S12（已完成 / V13）：Throughput Runtime（实际 vs 可持续上限 + 单向背压：风险上升只建议降速，scale_up 永不破上限）。

运营侧人工关卡（保持不变）：

1. 使用平台官方 App 扫码，验证新账号完整登录与校准闭环。
2. 录入真实视频/动态/微博/笔记抽奖链接（首页/个人主页不可作为执行目标）。
3. 核对自动解析的规则动作计划；歧义规则必须人工确认。
4. 使用已校准账号完成规则所需阶段的低风险探针。
5. 根据真实页面证据生成并复核选择器配置。
6. 完成 Shadow-run，验证页面、登录态与风险门禁。
7. 仅在证据完整、无近期风险时人工确认 Real-run。
