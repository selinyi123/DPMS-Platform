# DPMS_BilibiliGateNextAction_实施记录_v1.0_20260608

## 背景

在上一阶段中，Bilibili real-run 已接入证据门禁，前端活动池可以展示 `real-run/evidence` 的阻断原因。但当用户切换到 Real 模式时，阻断状态主要表现为提示，不能直接引导用户执行下一步准备动作。

本次改动目标是把门禁状态从“只说明问题”推进到“给出可执行下一步”，优先保证 Bilibili 的真实执行链路按安全顺序推进。

## 改动范围

- 后端 `real_run_gate_status` 调整 `next_action` 优先级。
- 前端活动池 Real 模式按钮接入门禁下一步动作。
- 中英文 UI 文案新增不可直接执行动作的提示。
- 重新构建前端静态资源并通过 Docker 部署验证。

## 门禁下一步优先级

后端现在按以下顺序返回 `next_action`：

1. 无可用账号：`add_account`
2. 账号存在近期风险：`review_risk`
3. 缺少近期完整探针：`probe`
4. 适配器选择器未就绪：`configure_adapter`
5. 缺少近期 shadow-run：`shadow_run`
6. 全局 real-run 开关关闭：`enable_real_run`
7. 全部条件通过：`real_run`

该顺序确保 Bilibili 活动在未完成完整探针时，优先引导执行探针，而不是停留在适配器配置提示上。

## 前端行为

在活动池中切换到 Real 模式后：

- 若门禁阻断且 `next_action=probe`，主按钮显示 **先跑探针**，点击后执行探针流程。
- 若门禁阻断且 `next_action=shadow_run`，主按钮显示 **先跑影子**，点击后执行 shadow-run。
- 若门禁已满足且 `next_action=real_run`，主按钮显示 **派发 Real**，点击后进入 real-run 派发。
- 若下一步是 `add_account`、`configure_adapter`、`review_risk` 或 `enable_real_run`，主按钮保持阻断态，点击时只提示用户到对应页面处理。

## 验证记录

验证时间：2026-06-08

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| Python 编译 | 通过 | `python -m compileall -q core\app worker\app` |
| 前端构建 | 通过 | `npm run build` |
| Docker 重建 | 通过 | `docker compose up -d --build core-api nginx` |
| 容器健康 | 通过 | `core-api`、`nginx`、`redis`、`postgres` 均为 healthy |
| 门禁证据接口 | 通过 | `/api/lotteries/real-run/evidence` 返回 200 |
| 浏览器任务页 | 通过 | Real 模式下 Bilibili 行显示 **先跑探针** |
| 浏览器控制台 | 通过 | 0 warning，0 error |

## 当前边界

- 本次改动没有放开真实 Bilibili 动作。
- real-run 仍必须同时满足完整探针、shadow-run、适配器选择器、账号风险和全局确认开关。
- 系统不实现验证码绕过或反检测规避能力；遇到验证码、限流、封禁、审核拒绝等风险信号时，应进入风险复查和降级流程。

## 后续建议

1. 将探针结果中的可见阶段转成适配器配置草案，减少人工配置成本。
2. 在运维通知页增加 Bilibili real-run 准备度清单。
3. 增加从 **先跑探针** 到 **保存选择器** 到 **先跑影子** 的连续向导。
