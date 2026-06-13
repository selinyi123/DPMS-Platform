---
aliases:
  - DPMS Platform
  - DPMS 项目主页
tags:
  - DPMS
  - 项目管理
  - 自动化
status: active
updated: 2026-06-13
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

- 当前运行版本：`v8.0-local`（V5.5 Experiment / V6 Risk Intelligence / V6.5 Learning / V7 Governance / V8 Transition Runtime 已落地）
- 当前优先平台：Bilibili
- 平台状态：微博、小红书、抖音均已接入选择器驱动执行链路，处于 `calibration_required` 阶段
- 本机入口：`http://localhost/`
- GitHub：`https://github.com/selinyi123/DPMS-Platform`（开发分支 `claude/code-review-ul0pqt`）
- 最近功能提交：S7 Transition Runtime（策略迁移图，发布/激活分离，宽松化强制留痕），详见 [[DPMS_V8_TransitionRuntime_实施记录_20260613]]
- 后续开发总设计方案：[[DPMS_总设计方案_v1_20260611]]；后续版本规划：[[DPMS_V8-V9_后续版本规划_20260612]]
- 测试基线：core **183 项**（94 + 26 + 15 + 14 + 15 + 19）+ worker 21 项单元测试通过
- Obsidian 知识库里程碑：`90f6236 Add Obsidian project knowledge base`
- Bilibili 官方二维码登录里程碑：`761051d Add official Bilibili QR login`
- Bilibili 动态发现与规则计划里程碑：`04e4c1d Add Bilibili discovery rule plans`
- 当前生产策略：Real-run 默认关闭，证据不完整时禁止真实执行；V7 起该门禁同时以版本化 Policy Object（`real_run_gate` v1）形式可查询、可回放

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

后续开发以 [[DPMS_总设计方案_v1_20260611]] 与 [[DPMS_V8-V9_后续版本规划_20260612]] 为准。阶段路线：

- S1（已完成）：Strategy Runtime 模块化 + 工程质量基线（策略引擎抽取、core/worker 测试基线、活动/账号分层）。
- S2（已完成）：决策可解释接口 + 前端 Strategy/Knowledge 页面。
- S3（已完成 / V5.5）：Experiment Runtime 最小闭环（仅 dry/shadow，自动停止 + guardrails）。
- S4（已完成 / V6）：Risk Intelligence（账号信誉聚合 + 24h 风险预测，`tighten_action` 只能收紧）。
- S5 前半（已完成 / V6.5）：Learning Runtime（Feature Store + 透明概率模型，advisory-only）。
- S6（已完成 / V7）：Governance Runtime（real-run 门禁制度化为可版本化、可回放的 Policy Object）。
- S7（已完成 / V8）：Transition Runtime（策略迁移图 / 制度血统，发布与激活分离，宽松化变更强制留痕）。
- S8（下一步 / V9）：Semantic Runtime（Intent → Institution → Policy → Transition → Execution 语义执行链，纯聚合解释层）。

运营侧人工关卡（保持不变）：

1. 使用平台官方 App 扫码，验证新账号完整登录与校准闭环。
2. 录入真实视频/动态/微博/笔记抽奖链接（首页/个人主页不可作为执行目标）。
3. 核对自动解析的规则动作计划；歧义规则必须人工确认。
4. 使用已校准账号完成规则所需阶段的低风险探针。
5. 根据真实页面证据生成并复核选择器配置。
6. 完成 Shadow-run，验证页面、登录态与风险门禁。
7. 仅在证据完整、无近期风险时人工确认 Real-run。
