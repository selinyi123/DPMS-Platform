# DPMS 微博抽奖模块：实施与调研（2026-07-22）

## 结论

微博与小红书、抖音的参与路径不同：微博开放平台仍公开了关注、点赞、评论、收藏和转发写接口，但接口可用性取决于应用类型、审核状态、OAuth 授权和逐动作权限。DPMS 因此采用 `weibo_oauth_v1` 作为官方执行路径，并保留不可自动写入的 `weibo_manual_v1` 人工回退路径。

选择器只承担登录态和页面形态的 Shadow 观察，不得因为选择器完整而升级为真实写执行器。每次真实动作仍必须经过账号绑定、规则快照、Action Plan v2、最新能力证据、风险门禁、用户主动确认和外部动作意图账本。

## 官方能力矩阵

| DPMS 动作 | 官方接口 | 权限与执行约束 |
| --- | --- | --- |
| 关注 | `POST /2/friendships/create.json` | 高级接口；只有微博类客户端可申请；需真实用户 IP `rip` |
| 点赞 | `POST /2/attitudes/create.json` | 高级接口；文档与实际权限可能漂移，校准时探测 |
| 评论 | `POST /2/comments/create.json` | OAuth；需要真实用户 IP `rip`；不得静默截断；不超过 140 个 UTF-16 字符单元 |
| 收藏 | `POST /2/favorites/create.json` | OAuth；按账号和应用探测当前授权 |
| 转发 | `POST /2/statuses/repost.json` | OAuth；需要真实用户 IP `rip`；空文案与显式文案必须区分；显式文案不超过 140 个 UTF-16 字符单元 |

官方资料：

- [关注接口](https://open.weibo.com/wiki/2/friendships/create)
- [点赞接口](https://open.weibo.com/wiki/2/attitudes/create)
- [评论接口](https://open.weibo.com/wiki/2/comments/create)
- [收藏接口](https://open.weibo.com/wiki/2/favorites/create)
- [转发接口](https://open.weibo.com/wiki/2/statuses/repost)
- [Base62 mblog ID 转数字 ID](https://open.weibo.com/wiki/2/statuses/queryid)
- [OAuth access token](https://open.weibo.com/wiki/Oauth2/access_token)
- [频率限制与用户主动行为要求](https://open.weibo.com/wiki/Rate-limiting)

“普通接口”不等于可匿名、批量或无人值守调用。未审核应用最多只能使用有限测试账号，测试写入不能作为公开活动成功证据。DPMS 不把测试状态、旧文档或单次成功响应推断成长期权限。

## 运行时合同

OAuth 凭据严格只允许 `credential_kind`、`access_token`、`uid`、`expires_at` 四个字段。凭据导入只能通过官方 `account/get_uid` 核验身份，不能自我声明应用审核状态或动作权限。

管理员必须根据微博开放平台后台中已核对的结果，在独立端点通过确认请求声明应用审核状态、客户端类型和五类动作的布尔权限。这是可审计的管理员声明，不应命名或展示为系统自动获取的“官方授权证明”。工作端核验 OAuth 身份后，`weibo_oauth_v1` 校准结果只保存不含 Token 的 `oauth_capabilities` 证据：

- `contract_version=1`
- 账号 ID 与凭据 `execution_revision`
- `credential_kind=weibo_oauth`
- OAuth 身份已核验
- 应用审核状态与客户端类型
- 核验时间、证据来源
- 管理员 ID、声明时间和校准 ID
- 五类动作各自的 endpoint、permission 和 granted 状态

证据来源固定为 `operator_attested_app_capabilities`，有效期为 24 小时。关注动作额外要求 `client_type=weibo`。应用为 `test_only`、身份未核验、动作未声明可用、证据过期或账号/版本不一致时，真实执行门禁保持关闭。

OAuth Token 在导入、门禁和 Worker 解密时都必须至少剩余 900 秒有效期。只读预检总时限为 300 秒；预检结束后和每个动作开始前，还会按“剩余动作数 × 20 秒 + 120 秒结算缓冲”再次检查 Token 余量。任何一步不足都在创建外部动作意图前 fail closed。

真实写入按以下顺序进行：

1. 固化目标、完整规则、逐动作精确载荷和计划哈希。
2. 绑定唯一账号、当前凭据版本、校准证明和受信任入口观测到的真实公网用户 IP。
3. 完成一次与账号、目标、规则、计划和适配器配置精确绑定的成功 OAuth `dry_run`；该证据不能替代官方能力声明。
4. 接收用户针对单次活动的主动确认。
5. 在网络调用前写入 `external_action_intents`。
6. 每个动作只调用一次；网络超时或响应不确定时标记 `unknown`，不得自动重试。
7. 核验官方回执的目标身份并写入不可混淆的分动作证据。

远端错误按[微博开放平台当前 Error_code 表](https://open.weibo.com/wiki/Error_code)采用小型白名单。只有官方语义明确、且适用于当前动作的认证、权限或限流码，才可把意图结算为“确认未生效”，并分别把账号转为 `login_required`、`warming` 或 `cooling`。`20032`（远端可能已成功但处理延迟）、未知码、非规范错误码、裸非 2xx、5xx、超时、断连、取消和无法验证的回执一律视为结果未知：保留对账锁、隔离账号且不得自动重试。远端错误文案不参与分类，也不得进入 Token、凭据或原始响应日志。

OAuth Token 在管理员从账号页导入时会短暂存在于前端表单内存和当次 HTTPS（纯本地时为 loopback HTTP）请求中；提交成功后立即清空，后端不回显。Token 不得进入应用日志、命令行参数、校准证据或队列；Core 只以加密凭据载荷持久化。浏览器开发者工具和本机内存仍是导入时的敏感边界。

`rip` 是敏感网络标识。Core 到 Worker 的 outbox、Redis 和死信传递只允许加密密文；Worker 只在内存中短暂解密，日志、审计和意图哈希不得持久化原值。
意图账本只记录使用同一主密钥派生、按用途隔离的 HMAC，不记录可被离线枚举的裸哈希；回执结算还必须核对意图中固化的 HTTP 方法、官方端点和不含秘密值的精确请求语义。

`weibo_oauth_v1` 允许不访问微博网络的本地 `dry_run`，用于验证计划和持久化链路。真实执行要求最近一次成功的精确绑定 OAuth `dry_run`，但该结果不能生成或替代能力证据；`weibo_manual_v1` 只允许 `shadow` 观察，不能进入自动写入路径。

## 规则与好友 @ 语义

微博规则可以要求评论或转发时“至少 @N 位好友”或“恰好 @N 位好友”。DPMS 将数量约束按动作保存为：

```json
{
  "commented": { "mode": "minimum", "count": 3 }
}
```

具体好友账号仍必须逐个写入对应动作的 `mentions`，并原样出现在评论或转发文本中。Worker 会在任何写动作发生前通过官方用户查询逐个解析账号，按不同 UID 计数；别名重复、无法解析或伪造账号都会使整项任务失败。规则指定的品牌账号和关注目标不计入好友数量；源内容要求与绑定后的内容要求分别纳入计划哈希，避免把“@品牌 + @2 位好友”误判成“@3 位好友”。数量、动作或绑定不一致时计划不可执行。

一个计划在源规则、已绑定要求、评论/转发载荷和关注目标中合计最多允许 32 个规范化后的不同 `@账号`；Core、前端和 Worker 使用相同的 NFKC + 不区分大小写身份规则。`@alice` 与 `@alice2` 必须按完整 token 边界匹配，不能用前缀冒充。

## 目标导入与数据最小化

前端支持微博状态长链接、数字 mid、Base62 mblogid、官方风格 JSON/JSONL/CSV 导出。导入器会：

- 拒绝同一记录内冲突的 ID 与 URL；
- 忽略用户、评论和被转发微博子树中的无关 ID；
- 移除 Cookie、Token、会话、查询参数和提供方私有字段；
- 限制文件大小、节点数、目标数和清洗后输出大小；
- 要求 `t.cn` 短链接单独导入。

数字状态 ID 必须是规范 ASCII 十进制正整数：不得为 `0`、不得带前导零或 Unicode 数字，且不得超过有符号 64 位上限 `9223372036854775807`。即使目标已经是数字 ID，Worker 仍会先通过官方状态查询核对目标，再允许任何写动作。

## 创作者开奖侧

[微博官方抽奖平台说明](https://kefu.weibo.com/faqdetail?id=20090)（2026-01-12 更新）已经支持转发、评论或点赞单一主模式，以及关注、@好友、关键词等筛选条件。DPMS 应负责规则解析、计划审核、证据和官方开奖交接，不应通过私有接口抓取参与者后重复实现官方参与池。

官方时间线和评论 API 只覆盖对当前应用授权的用户，无法形成完整、公平的全量参与者名单。若未来支持自建开奖，只能使用合法取得的完整导出，并在截止前固定活动、资格规则、UID 排序名单哈希和未来公开随机源。

## 复用调研

- [微博官方 CLI](https://open.weibo.com/cli/index)：当前页面展示评论、转发和 Agent 工作流能力，但命令目录由服务端同步且存在套餐/额度。它可作为未来可选 Provider，不应成为核心硬依赖。采用前必须固定版本与 integrity、审查包内容、限制命令白名单并使用系统密钥存储。
- [evaun/weibo-lottery@77911ef](https://github.com/evaun/weibo-lottery/tree/77911efefdde497943dfe91c36d2fa062ae9b889)：MIT，近期有更新但成熟度低。只 clean-room 借鉴“名单承诺 + 未来随机种子 + SHA-256 可复算”思路，不复用页面脚本和私有接口抓取。
- [dataabc/weibo-crawler@a3bfe51](https://github.com/dataabc/weibo-crawler/tree/a3bfe515e9886b84151c609debdc636cbb0e9730)：活跃但未发现明确许可证，且依赖 Cookie/私有网页接口，不复制、不集成。
- [SpiderClub/weibospider@e1f2898](https://github.com/SpiderClub/weibospider/tree/e1f289871187da9e1c9096cd61984066c73625a8)：MIT，但多年未维护且引入账号密码、验证码、Redis/MySQL/Celery 等高成本与高风险依赖，不采用。
- [DecryptLogin@bb4228c](https://github.com/CharlesPikachu/DecryptLogin/tree/bb4228c0535ffd7060b7816cbd1da51ba8d95ab8)：Apache-2.0，但私有登录与批量自动互动方向不符合本模块安全边界，不采用。

本次没有引入新依赖。必须依次执行两个数据库迁移：`core/migrations/0012_task_phase_favorited.sql` 为 `task_phases.phase` 枚举加入 `favorited`，避免远端收藏成功后本地阶段落库失败；`core/migrations/0013_redact_legacy_plaintext_weibo_rip.sql` 清除 MySQL outbox/死信中的旧 `weibo_rip` 明文，并用一条带精确任务、活动锁和账号租约谓词的原子多表更新把尚未执行的 `queued` 任务终结为 `failed`、设置 `reconciliation_required=0`，同时只释放其准确持有的活动锁和账号操作租约。`running` 任务不由迁移直接改写，租约到期后继续交给现有恢复状态机按“外部结果未知”路径隔离和结算。通用加密凭据 JSON、校准结果 JSON、Action Plan JSON 和外部动作意图表继续承载其余合同。若未来采用官方 CLI，应作为独立、可禁用、经过包审计的适配器单独评审。

`0013` 只处理 MySQL，不能泛化为 Redis 全历史清理保证。当前 Worker 会在消费到不合法明文消息时先 `XDEL` 再 `XACK`，Core 也会处理消费者组 PEL 中仍待恢复的消息；但已被历史版本 `XACK` 的 `lottery_tasks` 条目，以及历史 `failed_task_messages` 条目，不会被现有升级路径主动全流扫描。当前本地两个 stream 已核验为空；其他部署升级前必须在停写窗口做一次受控 `XRANGE` 字段审计，并按独立、可回滚的运维方案清理，不得输出字段值。

本地 Compose 默认把 nginx 绑定到 `127.0.0.1:${DPMS_HTTP_PORT:-8080}`，Core 与 Worker 的 `REAL_RUN_ENABLED` 默认均为 `false`。覆盖文件可以继续提供本机配置，但缺失覆盖文件也不会意外暴露服务或开启真实写入。

## 已知限制

- 没有正式审核的微博应用和逐动作授权时，无法做真实线上写入验证。
- `rip` 必须来自用户本次主动操作的真实出口，不得伪造或用代理替代。
- 当前本地入口只监听 `127.0.0.1:8080`；Docker Desktop 下 nginx 通常只能看到回环或 bridge 地址。因此需要 `rip` 的关注/评论/转发真实动作在纯本地环境必须 fail closed，当前部署不得宣称可真实运行这三类动作。
- 生产入口需要单独部署设计：必须 TLS 终止，只对明确 allowlist 的反向代理/LB 信任客户端 IP 头，在 nginx `real_ip` 链路中安全恢复原始地址，并阻断直达 Core 的网络路径。未完成该评审和实装前，不能把 nginx 直接暴露公网，也不能接受客户端自填的 `X-Real-IP`/`X-Forwarded-For`。
- API 权限和套餐会变化；每次执行只信任最新能力证明，不信任静态文档或历史成功。
- 多动作任务若在部分动作成功后被平台拒绝或中断，完整任务不得自动重放；当前缺失动作 repair 的逐动作意图绑定尚未启用，系统会把修复标为不可执行并要求人工对账，不能宣称端到端自动恢复。
- 参与者抓取和公平开奖不属于本次参与动作模块的自动化范围。
