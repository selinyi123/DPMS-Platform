---
tags:
  - DPMS
  - Bilibili
  - 自动发现
  - ShadowRun
updated: 2026-06-24
---

# DPMS Bilibili 自动发现扩散实施记录

## 摘要

- 代码提交：`e872913 Expand Bilibili discovery sources`
- 权威技术记录：`docs/DPMS_BilibiliDiscoveryExpansion_实施记录_20260624.md`
- 本地部署状态：`core-api` / `mysql` / `nginx` / `redis` / `worker` 均 healthy
- 自动发现结果：从抽奖合集动态扩散出 `80` 个 Bilibili UP 源，二次扫描新增 `70` 个候选目标
- 当前 Bilibili 源池：`81` 个 `up` 源 + `1` 个 `url_list` 源
- 影子运行验证：Lottery `72`，Task `a5d91fe9-e09b-4b3b-92dc-a9282574c307`，状态 `succeeded`
- 真实执行门禁：除 `global_real_run_enabled=false` 外，其余关键门禁均满足

## 已实现能力

1. 从 Bilibili 合集动态中识别 `【UP 名称】、【UID】`。
2. 自动加入 `tracked_sources`，形成可持续扫描的 UP 源池。
3. 对真实 UP 动态生成候选抽奖目标。
4. 自动解析规则动作计划。
5. 支持旧目标重复扫描时刷新 `title`、`rule_text`、`action_plan`。
6. 修复 UTF-8 被 Latin-1/CP1252 误解码导致的规则文本乱码。
7. 通过已校准 Bilibili 账号完成 shadow-run 验证。

## 安全边界

- 未开启真实账号动作。
- 未绕过验证码。
- 未接入打码平台。
- 未规避平台风控。
- 未在用户确认具体目标和动作前执行真实关注、点赞、评论或转发。

## 当前阻塞

- Bilibili real-run 当前唯一阻塞：全局真实执行开关关闭。
- 生产级多账号运行仍需要补齐代理出口。
- Weibo / Douyin / Xiaohongshu 仍缺少已校准安全账号和真实动作适配器证据。

## 验证

- `core.tests.test_bilibili_collection_expansion`
- `core.tests.test_bilibili_discovery`
- `core.tests.test_lottery_rules`
- `python -m unittest discover core\tests`
- `py_compile`
- `git diff --check`

结果：core `341` 项单元测试通过。
