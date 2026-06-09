---
aliases:
  - DPMS Platform
  - DPMS 项目主页
tags:
  - DPMS
  - 项目管理
  - 自动化
status: active
updated: 2026-06-09
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

- 当前运行版本：`v3.0.2-local`
- 当前优先平台：Bilibili
- 本机入口：`http://localhost/`
- GitHub：`https://github.com/selinyi123/DPMS-Platform`
- 最近功能提交：`89e7aa0 Reject invalid Bilibili lottery targets`
- Obsidian 知识库里程碑：`90f6236 Add Obsidian project knowledge base`
- 当前生产策略：Real-run 默认关闭，证据不完整时禁止真实执行

## 快速导航

- [[DPMS_活动时间线]]
- [[DPMS_记录规范]]
- [[DPMS_能力演化路线图_v4-v9_20260605]]
- [[DPMS_最终搭建审阅与漏洞清单_20260602]]
- [[DPMS_EventStore_V4_实施记录_20260605]]
- [[DPMS_KnowledgeRuntime_实施记录_20260606]]
- [[DPMS_StrategyQueue_实施记录_20260606]]
- [[DPMS_BilibiliTargetValidation_实施记录_20260609]]
- [[DPMS_BilibiliOfficialQrLogin_实施记录_20260609]]
- [[DPMS_ObsidianKnowledgeBase_实施记录_20260609]]

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

1. 使用 Bilibili 官方 App 扫码，验证新账号完整登录与校准闭环。
2. 录入真实 Bilibili 视频或动态抽奖链接。
3. 使用已校准账号完成四阶段低风险探针。
4. 根据真实页面证据生成并复核选择器配置。
5. 完成 Shadow-run，验证页面、登录态与风险门禁。
6. 仅在证据完整、无近期风险时人工确认 Real-run。
