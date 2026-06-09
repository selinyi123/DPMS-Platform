# DPMS Bilibili 动态发现与规则动作计划实施记录

## 目标

补齐 Bilibili 从活动发现到安全执行之间的关键缺口：

- 从已跟踪 UP 主的公开动态中发现抽奖候选。
- 保存活动标题、规则原文、发布时间和结构化动作计划。
- 仅执行活动规则明确要求的动作，不再默认执行关注、点赞、评论、转发四个阶段。
- 规则缺失或存在歧义时阻断 `real_run`，允许操作员在前端复核。

## 实现内容

### UP 主动态发现

- `up` 类型发现源使用 UP 主数字 UID。
- Core 使用一个已校准且状态为 `ready` 的 Bilibili 账号进行只读动态请求。
- 请求复用加密凭据中的 Cookie；Cookie 不写入日志、不返回前端。
- 单个发现源失败只增加失败计数，不中断其他发现源扫描。
- 只保留包含抽奖关键词的动态候选。

### 规则解析

解析器从动态正文及主要内容块中提取：

- `title`
- `rule_text`
- `published_at`
- `action_plan.required_actions`
- `action_plan.confidence`
- `action_plan.review_required`
- 命中的规则与歧义模式

当前支持的动作：

- `followed`
- `liked`
- `commented`
- `reposted`

包含“无需”“不用”“禁止”“可选”“任选”等歧义表达时，计划必须人工复核。

### 安全门禁与执行

- 无动作计划：`lottery_action_plan_required`
- 规则待复核：`lottery_rule_review_required`
- 未识别动作：`lottery_required_actions_missing`
- 上述任一状态均禁止 `real_run`。
- Core 将动作计划随任务写入 Redis Stream。
- Worker 的 dry-run、shadow-run 和 real-run 均按动作计划选择阶段。
- real-run 仍要求账号、探针、Shadow、适配器、熔断器和全局开关全部通过。

### 前端

- 活动池新增“规则计划”列。
- 展开后可查看或修正规则原文。
- 可勾选参与所需动作并确认计划。
- 人工确认写入审计日志和事件存储。
- 活动池使用稳定列宽和横向滚动，避免门禁标签被挤成竖排。

## 验证

- Core 单元测试：12 项通过。
- Python 编译检查通过。
- 前端生产构建通过。
- 数据库字段 `title`、`rule_text`、`action_plan`、`published_at` 已迁移。
- API 运行态验证：动作计划可保存、回读，并被 Real-run 门禁识别。
- Worker 容器验证：`liked`、`reposted` 计划只返回对应两个阶段。
- 已校准账号只读访问 Bilibili 动态接口成功，候选数量为 0，未触发任何互动动作。
- Docker 五个容器均为 `healthy`。
- Core 启动迁移改为先检查字段是否存在，并抑制 `CREATE TABLE IF NOT EXISTS` 的预期驱动告警；重启日志中无 warning、error、Traceback 或 Exception。
- 桌面与移动端浏览器验证通过，控制台 0 错误、0 警告。

## 安全边界

- 匿名动态 API 请求实测返回 `412`，因此不使用签名伪造、验证码绕过或反检测方式。
- 发现请求只使用用户现有登录会话执行公开内容读取。
- 系统不能承诺“零风险”；当前设计以低频、只读发现、规则明确、证据先行和遇风险停止为原则。
- 首次真实执行仍需要用户提供真实、低风险活动目标并完成探针与 Shadow 验证。
