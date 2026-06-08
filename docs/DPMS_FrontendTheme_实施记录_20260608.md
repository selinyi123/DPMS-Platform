# DPMS_FrontendTheme_实施记录_v1.0_20260608

## 背景

用户反馈前端深浅色模式存在问题：切换主题时只有局部框内颜色变化，页面整体仍然偏深，无法形成完整的浅色界面。

## 目标

- 保留中英文语言系统。
- 保留 **浅色**、**深色**、**跟随系统** 三种主题模式。
- 让主题切换作用于全局页面，而不是只作用于局部卡片。
- 确保表单、表格、按钮、面板、侧栏、通知等核心界面元素都使用主题变量。

## 改动内容

### 主题变量扩展

在 `frontend/src/index.css` 中补齐以下变量族：

- `--app-bg`
- `--surface-bg`
- `--surface-muted-bg`
- `--surface-border`
- `--text-primary`
- `--text-secondary`
- `--text-muted`
- `--text-soft`
- `--control-bg`
- `--control-muted-bg`
- `--control-border`
- `--table-border`
- `--table-row-border`
- `--table-row-hover-bg`
- `--segmented-bg`
- `--segmented-active-bg`
- 状态色变量：成功、警告、危险、信息、冻结、日志等

### 全局背景修复

为 `html`、`body`、`#root`、`.app-shell`、`.main-pane` 统一接入 `--app-bg`，避免页面外层仍停留在深色背景。

### 组件硬编码迁移

将以下区域从硬编码色值迁移到主题变量：

- 页面标题与面板标题
- 指标卡片
- 表单标签与输入框
- 二维码登录区域
- 分段按钮
- 表格表头、单元格、悬停行
- 知识统计块
- 操作计划行
- 生产检查行
- 状态 Badge
- 通知 Toast
- 运维通知与环境配置区域

### 深色兜底统一

旧的 `:root[data-theme="dark"]` 兜底覆盖改为引用主题变量，避免深色模式成为第二套孤立样式入口。

## 验证记录

验证时间：2026-06-08

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 前端构建 | 通过 | `npm run build` |
| Docker 刷新 | 通过 | `docker compose up -d --build nginx` |
| 容器健康 | 通过 | `core-api`、`worker`、`nginx`、`redis`、`mysql` 均为 healthy |
| 浅色主题 DOM | 通过 | `documentElement.dataset.theme=light` |
| 浅色全局背景 | 通过 | `html/body/app-shell/main` 为浅灰背景 |
| 浅色面板 | 通过 | `.panel` 为白色背景 |
| 深色主题 DOM | 通过 | `documentElement.dataset.theme=dark` |
| 深色全局背景 | 通过 | `html/body/app-shell/main` 为深色背景 |
| 浏览器控制台 | 通过 | 切换验证期间未发现前端 error |

## 验证色值摘要

浅色模式关键色值：

- `html/body/app-shell/main`: `rgb(238, 242, 247)`
- `.sidebar`: `rgb(255, 255, 255)`
- `.panel`: `rgb(255, 255, 255)`
- `.input`: `rgb(255, 255, 255)`

深色模式关键色值：

- `html/body/app-shell/main`: `rgb(15, 23, 42)`
- `.sidebar`: `rgb(16, 24, 39)`
- `.panel`: `rgb(23, 32, 51)`
- `.data-table td`: `rgb(17, 28, 46)`

## 当前边界

- 本次重点修复主题系统，不重做整体信息架构。
- 日志区域仍保留深色控制台样式，这是运行日志的领域语义，不视为浅色模式失效。
- 后续 UI 美化应继续围绕任务流、账号资产、Bilibili real-run 准备度与通知健康状态展开。
