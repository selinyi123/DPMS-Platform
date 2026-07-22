# DPMS 抖音人工辅助抽奖模块：实施与调研记录

- 调研日期：2026-07-22
- 实施边界：目标导入、规则结构化、人工清单、无副作用 Shadow 观察、证据绑定和失败关闭
- 明确不做：模拟普通参与者执行关注、点赞、顶层评论、收藏或转发；私有接口签名、设备伪装、验证码绕过和批量账号规避

## 1. 结论

当前抖音开放平台没有覆盖“普通参与者对任意抽奖作品执行关注、点赞、顶层评论、收藏、转发”的公开写接口。`video.comment` 面向媒体后台、MCN、SaaS 等管理已授权账号的视频评论，提供列表、回复和置顶；`item.comment` 也不是任意作品的顶层评论发布能力。`aweme.share` / `im.share` 用于把第三方应用内容分享或发布到抖音，不等同于转发某个既有抽奖作品。沙盒虽默认具备 scope，但接口返回 MOCK 数据，不能证明真实参与者动作可执行。

因此本阶段采用 `douyin_manual_v1`：系统准确解析和固定规则，生成操作员清单，并以只读 Shadow 检查页面和独立状态证据；所有自动真实互动永久失败关闭。未来若官方新增参与者写接口，应新增独立 provider，而不是放宽当前人工 provider。

官方依据：

- [权限说明](https://open.douyin.com/platform/resource/docs/accession-guide/type-and-permission)
- [视频评论管理接入方案](https://open.douyin.com/platform/resource/docs/ability/interaction-management/video-comment-management-solution)
- [视频搜索管理接入方案](https://open.douyin.com/platform/resource/docs/ability/search-management/video/)
- [Android 分享给抖音好友或群](https://open.douyin.com/platform/resource/docs/develop/share/android-share-with-douyin)
- [沙盒环境](https://open.douyin.com/platform/resource/docs/develop/common-tools/sandbox)

## 2. 开源项目取舍

| 项目（固定版本） | 许可与维护观察 | 本项目取舍 |
| --- | --- | --- |
| [bytedance/douyin-openapi-sdk-go@a50593c](https://github.com/bytedance/douyin-openapi-sdk-go/tree/a50593c5ccdc31ab5b7cb2153947dfad612bbed9) | 官方 SDK，Apache-2.0；适合 OAuth、公开模型和错误契约 | 只作为官方 API 边界参考；DPMS 当前是 Python 服务，不新增 Go sidecar |
| [NanmiCoder/MediaCrawler@0625e01](https://github.com/NanmiCoder/MediaCrawler/tree/0625e01a6bc717a3fc9c96d3dac7fb8957043838) | 2026-07-22 所见许可证为 Non-Commercial Learning License 1.1，并限制商业用途和大规模抓取 | 只参考 provider 隔离、结构化导出和多平台适配思想；不复制代码，不接入登录态、签名、代理或反检测实现 |
| [Johnserf-Seed/f2@7dab3e2](https://github.com/Johnserf-Seed/f2/tree/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3) | Apache-2.0；偏下载和数据获取，页面变化维护成本高 | 仅以 clean-room 方式参考 aweme/note 标识归一化；不接入私有端点、Cookie、A-Bogus 或设备模拟 |
| [JoeanAmier/TikTokDownloader@96c6ece](https://github.com/JoeanAmier/TikTokDownloader/tree/96c6ece8c2bb4ec7676f1529160df959b0804fa2) | GPL-3.0，强 copyleft；定位为下载器 | 不直接集成或复制，避免许可证与产品边界扩散 |
| [wenyg/douyin-creator-tools@e35dbe](https://github.com/wenyg/douyin-creator-tools/tree/e35dbe27548cff292cc2d709417467a3bd464ed1) | 仓库页面未展示明确许可证 | 只参考单实例、预览和台账概念；许可证不明，不复制代码 |

本次没有新增、升级或复制任何第三方依赖。复用的是可独立实现的架构概念和公开数据形状，避免把抓取/下载工具误当作抽奖互动执行器。

## 3. 已实现契约

### 3.1 目标导入

- 支持抖音分享链接、19 位作品 ID、MediaCrawler/F2 风格 JSON/JSONL/CSV 数据。
- `aweme_type=68` 明确归一化为 `/note/{id}`，不再把图文 ID 猜成视频。
- 只接受内容上下文中的 `aweme_id` / `video_id` 和受信任 URL 字段；评论 ID、媒体 ID、描述里的链接及冲突 ID 全部失败关闭。
- 导入前移除查询参数、敏感字段和 provider 私有元数据；短链接必须单条导入。

### 3.2 Action Plan v2

- 执行路径固定为 `douyin_manual_v1`，`executable=false`。
- 支持按规则选择关注、点赞、评论、收藏、转发，不强制虚构不存在的转发文案。
- 评论、带话题、@、翻译等要求按精确文本与 token 绑定；关注目标与规则快照、规则哈希、计划哈希共同参与审查。
- 收藏与转发是两个独立动作和证据字段，禁止用通用“分享”按钮同时推断二者完成。

### 3.3 执行和账号边界

- dry-run 与 real-run 均不适用于人工辅助 provider；策略层直接建议无副作用 Shadow。
- Worker 在读取凭据、页面证据或触发任何动作前阻断真实执行；五种互动方法自身也永久抛出不支持错误，形成双层失败关闭。
- 抖音校准只验证所需 Cookie 和会话页面。无法通过官方接口核验账号身份时，结果标记为 `session_only`，账号保持 `warming`，由操作员查看证据后手动标记可用；不会再自动误报 `ready`。
- Core 排队时将平台、账号执行修订和 selector 配置计算哈希；Worker 在同一数据库事务内重新读取配置并核对哈希。配置变化会让旧 Probe 失败，不会静默观察另一套 selector。

### 3.4 人工清单与 Shadow

- 人工清单只从已审查、已确认完整、快照/哈希有效的计划生成。
- 清单包含精确关注对象、评论文本，以及需要时的转发文本；普通“转发本作品”允许空文本。
- Shadow 只读取页面可见性，不点击。收藏和转发必须分别配置明确的完成态 selector；通用 share icon 不合格。

## 4. 已知限制与后续路线

1. 页面 selector 会变化，且收藏/转发完成态没有安全的通用默认值；首次配置需要操作员在当前页面结构下人工确认，再由 Probe 绑定版本。
2. `session_only` 证据不能证明 Cookie 对应哪个抖音身份。未来只有在官方提供可用身份 API 后，才应恢复自动 `ready`。
3. 当前模块不读取参与者列表、不判定最终中奖结果，也不替代活动主办方使用的开奖工具。
4. 若未来获得官方参与者互动权限，建议新增 `douyin_official_v1` provider，并分别实现 OAuth scope、幂等键、官方回执、速率限制和审计；不得复用 `douyin_manual_v1` 的开关直接升级能力。
5. 真实平台操作仍需单独的人类确认与合规评估；本实施没有执行任何真实关注、点赞、评论、收藏或转发。

## 5. 验证范围

- Core：Action Plan v2、规则解析、目标归一化、策略模式、selector 配置和 Probe 绑定。
- Worker：人工 provider 永久阻断、校准状态、五动作独立性、导航安全、Shadow/Probe 配置绑定。
- Frontend：结构化导入、CSV 错误语义、人工清单、计划复核、账号 `session_only` 提示和生产构建。

具体命令及最终结果以本次任务交付记录为准。
