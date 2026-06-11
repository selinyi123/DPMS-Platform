# DPMS_DeployReadinessGeneralization_实施记录_v1.0_20260611

## 背景

`DPMS_BilibiliReadinessWizard_实施记录_20260608` 在运维通知页（Deploy 页）增加了「Bilibili 真实执行准备度」向导，把账号、探针、选择器、Shadow-run 和全局开关五项前置条件集中展示，并提供载入探针草稿、保存运行时选择器等操作入口。

随着微博、小红书、抖音相继接入选择器驱动执行链路（见 [[DPMS_WeiboXiaohongshuLotteryModule_实施记录_20260611]]、[[DPMS_AllPlatformLotteryRules_实施记录_20260611]]），四个平台均已具备 `real-run/evidence`、`adapters/config`、探针、Shadow-run 等完整数据源，但 Deploy 页向导仍硬编码为 Bilibili 专属。`DPMS_AllPlatformLotteryRules_实施记录_20260611` 的「下一步」中已记录此项待办：「评估将 Deploy 页就绪向导泛化为多平台」。

本次改动将该向导从 Bilibili 专属重构为四平台通用组件。

## 改动内容

### 平台选择

- `loadNotify` 新增读取 `/api/accounts/platforms`，返回的 `[{id, label, ...}]` 列表既作为平台 Tab 的数据源，也提供本地化的平台显示名（避免硬编码中英文标签）。
- 新增状态 `readinessPlatform`（默认 `bilibili`）与 `platforms`。
- 向导标题下方新增 `.segmented.platform-tabs` 平台切换条，复用既有 `.segmented` 样式（`index.css` 新增 `.platform-tabs { margin-top: 12px; }` 控制与标题的间距）。

### 数据与状态泛化

- 新增 `buildPlatformWorkflow(platform)`，取代原先 9 个硬编码的 `bilibili*` 派生状态变量（`bilibiliEvidence`、`bilibiliPlatformEvidence`、`invalidBilibiliTarget`、`bilibiliAdapter`、`bilibiliProbeCandidate`、`bilibiliSelectorDraftReady`、`bilibiliNextAction`、`bilibiliActiveProbe`、`bilibiliActiveShadow`、`bilibiliWorkflowActive`），统一返回 `{evidence, platformEvidence, invalidTarget, adapter, probeCandidate, selectorDraftReady, nextAction, activeProbe, activeShadow, workflowActive, readiness}`。
- `const workflow = buildPlatformWorkflow(readinessPlatform)`，所有 JSX 引用改为 `workflow.*`。
- 轮询 `useEffect` 的依赖从 `bilibiliWorkflowActive` 改为 `workflow.workflowActive`。
- `advanceBilibiliWorkflow` 重命名为 `advanceWorkflow`，从 `workflow` 中解构 `evidence`/`probeCandidate`，无目标时的提示文案改为 `formatText(t('deploy.noTargetForPlatform'), { platform: readinessPlatformLabel })`。
- `buildBilibiliReadiness` 重命名为 `buildReadiness`（函数体本身已是平台无关的）。
- `hasBilibiliSelectorDraft(value)` 重命名为 `hasSelectorDraft(value, platform)`，由硬编码 `parsed?.bilibili` 改为 `parsed?.[platform]`，从而正确识别微博/小红书/抖音的选择器草稿。

### 文案泛化（`uiContext.jsx`）

zh/en 字典中 `deploy.*` 命名空间：

| 旧 key | 新 key | 变化 |
| --- | --- | --- |
| `bilibiliReadiness` | `realRunReadiness` | 文案改为 `{platform} 真实执行准备度` / `{platform} real-run readiness` |
| `bilibiliReadinessHint` | `realRunReadinessHint` | 文案不变（已是平台无关描述） |
| `loadBilibiliProbeDraft` | `loadProbeDraft` | 去掉平台名 |
| `noBilibiliTarget` | `noTargetForPlatform` | 改为 `{platform}` 占位符 |
| `invalidBilibiliTargetsIgnored` | `invalidTargetsIgnored` | 文案改为通用「可执行的活动链接」表述 |
| `bilibiliSteps` | `readinessSteps` | `accountDetail`/`targetDetail`/`selectorDetail` 改为平台无关或 `{platform}` 占位符表述（如「平台校验通过的可执行抽奖链接（如视频、动态、微博或笔记）」） |

`readinessSteps.*Detail` 中含 `{platform}` 的条目通过 `formatText` 注入当前选中平台的本地化标签 `readinessPlatformLabel`。

### 样式

- `index.css` 新增 `.platform-tabs { margin-top: 12px; }`，用于平台 Tab 与标题区之间留白；`.bilibili-workflow-bar`/`.bilibili-readiness-grid`/`.bilibili-readiness-step` 类名保持不变（已是布局类，与平台无关，未重命名以减少 diff）。

## 验证记录

验证时间：2026-06-11

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 文案 key 残留检查 | 通过 | `frontend/src` 中无 `bilibiliReadiness\|loadBilibiliProbeDraft\|noBilibiliTarget\|invalidBilibiliTargetsIgnored\|bilibiliSteps\|buildBilibiliReadiness\|hasBilibiliSelectorDraft\|advanceBilibiliWorkflow` 等旧标识符残留 |
| `esbuild` 转译 | 通过 | `Deploy.jsx`、`uiContext.jsx` 均转译成功 |
| 前端构建 | 通过 | `npm run build`（vite v5.4.21，119 modules transformed） |
| 后端单测 | 通过 | `python3 -m unittest discover -s tests`：44 项全部通过（本次未改动 Python 代码） |

## 当前状态

Deploy 页「真实执行准备度」向导现支持 Bilibili / 微博 / 小红书 / 抖音四个平台切换：

- 平台 Tab 数据源：`/api/accounts/platforms`
- 准备度数据源：`/api/lotteries/real-run/evidence`、`/api/lotteries/adapters/config`、探针列表、`/api/metrics/runtime/settings`
- 五项准备度检查（安全账号、完整探针、运行时选择器、Shadow-run 证据、全局开关）对所有平台统一展示
- 默认选中平台：Bilibili（与既往行为一致）

## 安全边界

- 本次未保存任何平台的运行时选择器，未开启 real-run 总开关，未触发真实执行。
- 本次仅将既有 Bilibili 专属向导泛化为多平台共用组件，未新增风险识别或限速逻辑（相关能力已在各平台适配器中落地）。

## 后续建议

1. 为非 Bilibili 平台录入真实活动链接后，验证向导在微博/小红书/抖音下的探针草稿载入与选择器保存路径。
2. 评估是否在平台 Tab 上叠加「待处理」徽标（例如该平台存在 `target_valid=false` 的活动时提示）。
3. 待四平台均完成首次校准后，复核 `readinessSteps` 文案在不同平台下的可读性。

## 改动文件

- `frontend/src/pages/Deploy.jsx`
- `frontend/src/uiContext.jsx`
- `frontend/src/index.css`
- `dashboard/dist/*`（前端重新构建产物）

## 对应 Git 提交

- `d7ad892 Generalize Deploy readiness wizard to all platforms`
- 时间线见 [[DPMS_活动时间线]] 2026-06-11 条目。
