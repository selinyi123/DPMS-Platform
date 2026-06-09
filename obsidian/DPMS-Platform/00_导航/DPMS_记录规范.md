---
tags:
  - DPMS
  - 文档治理
  - Obsidian
updated: 2026-06-09
---

# DPMS 记录规范

## 必须记录的活动

- 架构版本或数据库结构变化。
- 新平台适配器、登录方式或真实动作能力。
- Probe、Dry-run、Shadow-run、Real-run 工作流变化。
- 账号安全、风险门禁、熔断器和回滚变化。
- 通知通道、部署配置和生产环境变化。
- 前端主要页面、主题或关键操作流程变化。
- P0/P1 缺陷修复和安全审查结果。

## 单次里程碑记录结构

1. 背景与问题。
2. 目标与安全边界。
3. 实现内容。
4. 变更文件。
5. 验证证据。
6. 已知限制。
7. 下一步。
8. 对应 Git 提交。

## 文件命名

```text
DPMS_[能力或模块]_实施记录_v[版本]_[YYYYMMDD].md
```

已有历史文件保持原名，不为了格式统一而改名。

## 同步流程

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/sync_obsidian.ps1
```

仅验证 Vault 是否与仓库一致：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/sync_obsidian.ps1 -VerifyOnly
```

默认 Vault：

```text
D:\TOOL\OBSIDIAN\Home\prompt仓库
```

使用其他 Vault：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/sync_obsidian.ps1 -VaultPath "D:\MyVault"
```

## 完成门禁

关键里程碑只有同时满足以下条件才视为记录完成：

- 仓库 `docs/` 存在实施记录。
- [[DPMS_活动时间线]] 已添加入口。
- 同步脚本执行成功。
- `-VerifyOnly` 返回所有文件一致。
- Git 提交已推送到远程仓库。
