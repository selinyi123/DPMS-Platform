# DPMS_BilibiliReadinessWizard_实施记录_v1.0_20260608

## 背景

Bilibili 是当前第一目标平台。系统已经具备账号、探针、选择器、shadow-run、real-run 门禁和通知阻断链路，但这些状态分散在任务编排、运维通知、适配器配置和 evidence API 中，用户需要跨区域判断下一步。

本次改动目标是在运维通知页增加 **Bilibili 真实执行准备度** 向导，把真实执行前置条件集中展示，并提供载入探针草稿、保存运行时选择器、刷新状态等操作入口。

## 改动内容

### 数据接入

运维页新增读取：

- `/api/lotteries/real-run/evidence`

并与现有数据合并：

- `/api/lotteries/adapters/config`
- `/api/lotteries/probes`
- `/api/metrics/runtime/settings`

### 向导步骤

| 步骤 | 数据来源 | 当前用途 |
| --- | --- | --- |
| 安全账号 | `real-run/evidence.safe_accounts` | 判断是否存在已校准且有有效凭据的 Bilibili 账号 |
| 完整探针 | `real-run/evidence.probe_ready` 与探针候选 | 判断 24 小时内四阶段探针是否完成 |
| 运行时选择器 | `adapters/config` 与 `selector_ready` | 判断 Bilibili 真实动作选择器是否配置 |
| Shadow-run 证据 | `real-run/evidence.shadow_ready` | 判断真实执行前是否有近期 shadow-run |
| 全局开关 | `metrics/runtime/settings.real_run_enabled` | 判断 real-run 总开关是否开启 |

### 用户操作

新增按钮：

- **载入 Bilibili 探针草稿**：将最新 Bilibili 探针候选填入选择器 JSON 文本框。
- **保存到运行时**：复用现有运行时选择器保存逻辑。
- **刷新**：重新读取通知、探针、适配器、运行时和 real-run evidence 状态。

### UI

新增样式：

- `.bilibili-readiness-grid`
- `.bilibili-readiness-step`

布局保持 DPMS 控制台风格：信息密度高、卡片半径 8px、状态 badge 清晰、无营销式装饰。

## 验证记录

验证时间：2026-06-08

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| Product Design 上下文 | 通过 | 已读取技能说明并运行 user-context preflight；无保存上下文 |
| 前端构建 | 通过 | `npm run build` |
| Docker 刷新 | 通过 | `docker compose up -d --build nginx` |
| 容器健康 | 通过 | `core-api`、`worker`、`nginx`、`redis`、`mysql` 均为 healthy |
| 浏览器 UI | 通过 | 运维通知页显示 Bilibili 真实执行准备度向导 |
| Evidence API | 通过 | `/api/lotteries/real-run/evidence` 返回 200 |
| 控制台 | 通过 | 0 error |
| 草稿载入 | 通过 | 点击后选择器 JSON 载入 Bilibili 探针草稿 |

## 当前状态

当前 Bilibili readiness 显示：

- 安全账号：就绪，`1 safe`
- 完整探针：需处理，当前探针候选 `2/4`
- 运行时选择器：需处理，`missing`
- Shadow-run 证据：需处理，`required`
- 全局开关：需处理，`disabled`
- 下一步动作：`probe`

## 安全边界

- 本次未保存 Bilibili 运行时选择器。
- 本次未开启 real-run 总开关。
- 本次未触发真实执行。
- 本次只把 Bilibili 前置条件集中展示，并让探针草稿载入更容易。

## 后续建议

1. 让探针页标出缺失的阶段，帮助用户补齐 `2/4 -> 4/4`。
2. 为 Bilibili readiness 增加“跑完整探针”一键入口。
3. 在选择器保存后自动刷新 readiness，并引导下一步 shadow-run。
