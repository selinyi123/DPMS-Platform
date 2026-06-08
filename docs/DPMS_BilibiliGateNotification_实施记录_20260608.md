# DPMS_BilibiliGateNotification_实施记录_v1.0_20260608

## 背景

Bilibili real-run 已接入证据门禁，并且前端任务编排页可以显示阻断原因和下一步动作。但在进入真实执行前，如果用户或系统触发 real-run 派发请求，阻断结果主要停留在 HTTP 响应和事件时间线中，缺少外部通知闭环。

本次改动目标是让 Bilibili real-run 被安全门禁拦截时，自动进入 `notify_events` 通知流，便于运维侧及时看到阻断原因和下一步动作。

## 改动内容

### 通知触发点

在 `core/app/api/lotteries.py` 的 real-run 派发链路中新增通知触发：

- real-run 前置检查失败：
  - 未带确认头
  - 请求体未确认
  - 全局 real-run 开关关闭
  - 熔断器阻断
  - 真实动作适配器未启用
  - 证据门禁未满足
- 选中账号后的二次证据校验失败：
  - 缺近期完整探针
  - 缺近期 shadow-run
  - 账号近期存在风险事件

### 通知范围

当前只对 `platform=bilibili` 发出 real-run gate 阻断通知。其他平台仍保持原有事件记录，避免尚未进入第一目标的平台产生噪声。

### 通知内容

通知事件写入 `notify_events`，字段包含：

- `event_type=bilibili.real_run_gate.blocked`
- `severity=warning`
- `title=Bilibili real-run gate blocked: L{id}`
- `content`：
  - 平台
  - 活动 ID
  - 活动 URL
  - 下一步动作
  - 阻断项
  - 操作者
- `channels=all`

### 下一步动作映射

| 阻断项 | 下一步动作 |
| --- | --- |
| `no_calibrated_ready_account` | `add_account` |
| `recent_account_risk_event` | `review_risk` |
| `recent_complete_probe_required` | `probe` |
| `real_adapter_not_enabled` | `configure_adapter` |
| `recent_shadow_run_required` | `shadow_run` |
| `global_real_run_disabled` | `enable_real_run` |

## 安全边界

- 本次改动没有放开 Bilibili real-run。
- 本次改动没有实现验证码绕过、反检测规避或平台风控对抗。
- real-run 仍必须满足账号、探针、shadow-run、适配器、熔断器、全局确认开关等安全条件。
- 通知发送失败不会静默吞掉，会写入结构化日志 `real_run_gate_notification_failed`。

## 验证记录

验证时间：2026-06-08

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| Python 编译 | 通过 | `python -m compileall -q core\app worker\app` |
| 静默异常扫描 | 通过 | 新增通知路径没有 `except: pass` |
| Docker 刷新 | 通过 | `docker compose up -d --build core-api` |
| 容器健康 | 通过 | `core-api`、`worker`、`nginx`、`redis`、`mysql` 均为 healthy |
| Evidence API | 通过 | `/api/lotteries/real-run/evidence` 返回完整门禁结构 |
| 下一步映射 | 通过 | 当前 Bilibili 阻断项映射为 `probe` |

## 验证说明

当前本机已配置真实 ServerChan 通道。为避免自动发送外部测试告警，本次未通过 real-run 派发接口强行触发真实通知，而是验证了：

- 通知接入代码路径可编译。
- Bilibili evidence API 仍稳定。
- 阻断项解析函数可提取 blockers。
- 下一步动作映射函数输出 `probe`。
- 真实通知事件将在 real-run 被门禁阻断时写入 `notify_events`，由现有通知分发器处理。

## 后续建议

1. 在运维通知页增加 `bilibili.real_run_gate.blocked` 专项日志过滤。
2. 在任务编排页增加“发送门禁测试通知”按钮，需管理员确认后触发。
3. 将 `probe -> 保存选择器 -> shadow-run -> real-run gate` 做成连续向导。
