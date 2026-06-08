# DPMS Bilibili Real Gate UI 实施记录 2026-06-08

## 背景

Bilibili real-run 安全门禁已经在后端和 Worker 层完成，但操作页仍只能看到平台是否具备 `action_adapter`，无法判断某个具体目标缺少哪些真实执行证据。本节点把后端证据门禁接入前端，使操作员在任务编排页直接看到每个目标的 real-run 可执行状态、阻塞原因和下一步动作。

## 本次改动

- 新增 `GET /api/lotteries/real-run/evidence` 批量接口，按目标返回 real-run gate 状态。
- 目标级 gate 输出包含：
  - `allowed`
  - `blockers`
  - `next_action`
  - `real_run_enabled`
  - `adapter_enabled`
  - `selector_ready`
  - `safe_accounts`
  - `probe_ready`
  - `shadow_ready`
- 前端任务编排页新增 **Real 门禁** 列。
- Bilibili 平台卡片新增最新探针进度和阻塞项展示。
- real-run 模式下，目标行按钮会按照 gate 状态禁用，并显示下一步动作。
- API 错误格式化支持结构化 `detail`，避免 `[object Object]` 出现在提示中。
- 中英文文案新增 real-run gate、blocker 和 next action 翻译。

## 验证记录

- `python -m compileall -q core\app worker\app` 通过。
- `npm run build` 通过。
- `docker compose up -d --build core-api nginx` 通过，Core API 与 nginx 健康。
- `GET /api/lotteries/real-run/evidence` 返回 200，Bilibili 目标显示：
  - `global_real_run_disabled`
  - `real_adapter_not_enabled`
  - `recent_complete_probe_required`
  - `recent_shadow_run_required`
- Playwright CLI 使用本机 Chrome 打开 `http://localhost/` 并切换到任务编排页：
  - 控制台错误 0，警告 0。
  - 网络请求 `GET /api/lotteries/real-run/evidence` 多次返回 200。
  - 页面出现 **Real 门禁** 列。
  - Bilibili 行显示 **证据不足 / 总开关关闭 / 适配器未启用 / 缺近期完整探针**。

## 当前状态

- Bilibili 的真实执行门禁已经从后台规则变成前台可见工作流。
- 当前 Bilibili 仍被正确阻断，原因是缺少近期完整探针、缺少同目标 shadow-run，且全局 real-run 开关关闭。
- 下一步应让操作页进一步引导执行顺序：先探针、再 shadow-run、再配置选择器、最后人工确认 real-run。
