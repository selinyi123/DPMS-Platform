# DPMS Bilibili API Real-run 实施记录

## 背景

本次节点回应 `shanmiteko/LotteryAutoScript` 的接入评估：该项目在 Bilibili 动态抽奖自动化上提供了成熟的接口形状、执行顺序和业务码处理经验，但其许可证为 GPL-3.0，且整体架构是单体 Node.js 脚本，不适合直接并入 DPMS。

DPMS 采取的方式是“行为级学习 + Python 原生重写 + 门禁接入”：

- 不 vendoring 外部仓库源码。
- 不复制 GPL 源码实现。
- 复用 DPMS 已有的账号、事件、策略、门禁、Outbox、通知和风控体系。
- 仅接入 Bilibili 动态/opus 目标的受控真实动作执行链。

## 变更范围

### Core

- `core/app/adapter_config.py`
  - 新增 `API_REAL_ADAPTER_PLATFORMS = ("bilibili",)`。
  - 新增 `platform_has_api_real_adapter()`、`platform_has_runtime_real_adapter()`、`platform_real_adapter_kind()`。

- `core/app/platforms.py`
  - 平台真实动作能力现在支持两种来源：原生 API 引擎与完整 selector 配置。

- `core/app/services/real_run_readiness.py`
  - Bilibili API 引擎不再要求页面 selector probe。
  - Bilibili real-run 目标必须是动态目标：`https://t.bilibili.com/{id}` 或 `https://www.bilibili.com/opus/{id}`。
  - 视频 URL 仍可作为 Bilibili 目标被普通校验识别，但不会被视为 API real-run 可执行目标。
  - 新增 blocker：`bilibili_dynamic_target_required`。

- `core/app/api/metrics.py`
  - readiness 输出新增 `adapter_kind`。
  - Bilibili readiness 显示为 `api` 通道，不再误报 `real_adapter_not_enabled` 或 `adapter_probe_incomplete`。

- `core/app/api/lotteries.py`
  - 适配器列表、配置状态、dispatch 预检查和策略队列均使用统一的 runtime real adapter 判断。
  - Strategy queue 对 Bilibili API real-run 使用动态目标语义，避免把视频目标推荐为真实执行。

### Worker

- `worker/app/bilibili/runtime.py`
  - 新增 Bilibili 运行时纯函数：
    - 动态 ID 提取。
    - DPMS 阶段到 API action 的映射。
    - 动态详情解析。
    - 执行动作前置字段校验。
    - CAPTCHA / LIMIT / RISK / AUTH 结果到账号安全状态的映射。

- `worker/app/task_runner.py`
  - Bilibili `real_run` 优先走 `BilibiliApiClient + BilibiliApiExecutor`。
  - 其他平台仍走原有 Playwright selector real action。
  - Bilibili real-run 执行前校验：
    1. action plan 已确认且存在 required actions。
    2. target 为可解析动态 ID。
    3. 账号 Cookie 可转换为标准 Cookie header。
    4. `nav.isLogin=true`。
    5. 动态详情可解析出执行所需字段。
  - 执行结果写入 `task_phases` 和事件流。
  - 命中 `AUTH` 时账号进入 `login_required`。
  - 命中 `CAPTCHA`、`LIMIT`、`RISK` 时账号进入 `cooling`。

- `worker/app/utils/cookies.py`
  - 新增 `credential_to_cookie_header()`，支持将 JSON Cookie 列表或原始 Cookie 文本转换为 API 客户端可用的 Cookie header。

## 安全边界

本节点不实现以下能力：

- 不绕过验证码。
- 不做 OCR / 打码平台接入。
- 不隐藏自动化行为。
- 不规避平台风控。
- 不降低 real-run 全局开关、治理策略、熔断器、账号校准、shadow-run 证据、人工确认等既有门槛。

触发验证码、限流、风控或登录失效时，系统行为是停止、记录、通知、冷却账号，而不是继续对抗。

## 使用说明

Bilibili API real-run 的最低条件：

1. 通过二维码或 Cookie 导入一个 Bilibili 账号。
2. 账号校准成功并处于 `ready`。
3. 录入 Bilibili 动态/opus 抽奖链接：
   - `https://t.bilibili.com/{dynamic_id}`
   - `https://t.bilibili.com/opus/{dynamic_id}`
   - `https://www.bilibili.com/opus/{dynamic_id}`
4. 规则解析后人工确认 `action_plan.required_actions`。
5. 对同一目标完成 shadow-run 证据。
6. 全局 real-run 开关开启。
7. Governance `real_run_gate` 允许。
8. 管理员二次确认派发。

视频链接、短链和个人主页不作为本节点的 API real-run 执行目标。

## 验证结果

```powershell
$env:PYTHONPATH='worker'; .\.venv312\Scripts\python.exe -m unittest worker.tests.test_bilibili_runtime worker.tests.test_bilibili_engine worker.tests.test_bilibili_api_real_run worker.tests.test_bilibili_dry_run_smoke worker.tests.test_bilibili_shadow_run_smoke
```

结果：24 项通过。

```powershell
$env:PYTHONPATH='core'; .\.venv312\Scripts\python.exe -m unittest core.tests.test_platforms core.tests.test_real_run_gate core.tests.test_adapter_probe_apply
```

结果：24 项通过。

## 后续事项

- 在真实低风险 Bilibili 小号上执行一次只读 self-test：`worker/tools/bilibili_api_selftest.py`。
- 录入一个真实动态抽奖目标，完成 dry-run、shadow-run 和 real-run gate 检查。
- 若需要支持 Bilibili 视频抽奖，应新增“视频到动态/评论目标解析”能力，不能复用当前动态 API real-run 路径直接执行。
