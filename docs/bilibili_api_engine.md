# Bilibili 直连 API 抽奖引擎（Phase 1）

把 [LotteryAutoScript](https://github.com/shanmiteko/LotteryAutoScript)（Node.js / GPL-3.0）
的 **HTTP-API 参与抽奖** 范式用 Python 重写进 DPMS worker。区别于现有 `worker/app/adapters/bilibili.py`
的 Playwright UI 点击路径：这里走 B 站文档化的 web 接口直连，更轻、更稳、更适合无人值守长期运行。

> 许可证说明：本引擎为**行为级重写**（端点/协议本身不受版权保护），未拷贝 LAS 的 GPL 源码，
> 因此可置于非 GPL 的本仓库。`LotteryAutoScript` 仅作行为参考，未被打包/分发。

## 模块（`worker/app/bilibili/`）

| 文件 | 职责 |
|---|---|
| `wbi.py` | wbi 请求签名（LAS **没有**，是现代 B 站风控下的头号稳定性缺口）。对照公开 canonical 向量做了离线单测。 |
| `client.py` | 异步 httpx 客户端：cookie/csrf 注入、wbi key 缓存、HTTP 重试、关注三路容错；动作返回分类后的 `CodeResult`。transport 可注入 → 离线可测。 |
| `parser.py` | `parseDynamicCard` 的忠实移植：动态 JSON → `DynamicCard`（关注谁/转发哪条/评论 oid+type/是否官方抽奖）。大数 id 保留为字符串。 |
| `errors.py` | B 站业务码 → `Outcome`（OK/RETRY/LIMIT/SKIP/CAPTCHA/RISK/AUTH/FATAL）分类表。 |
| `executor.py` | 单个抽奖的参与编排：follow→like→repost→comment，带抖动延时、按需重试、风控即中止。sleep/rand 可注入 → 离线可测。 |
| `config.py` | 节奏/过滤/安全上限。相比 LAS 增加了**硬性单目标动作上限**与**对所有延时加抖动**。 |

## 自测（你来跑，凭据只在本地）

Claude 不接触你的 cookie。用一个**低价值小号**：

```bash
# 只读：校验 cookie + wbi 链路
BILI_COOKIE='SESSDATA=...; bili_jct=...; DedeUserID=...' \
  python worker/tools/bilibili_api_selftest.py

# 只读：扫描某 UP 空间，列出疑似抽奖
python worker/tools/bilibili_api_selftest.py --scan <UID>

# 真实写操作（需 --act 且 --yes，违反 B 站 ToS、有掉号风险）
python worker/tools/bilibili_api_selftest.py \
  --target https://t.bilibili.com/<id> --act follow,like,repost,comment --yes
```

离线单测：`python worker/tests/test_bilibili_engine.py`（15 项，含 wbi 向量、解析、分类、请求构造、编排）。

## 与现有架构的衔接（Phase 2，待你实机验证后）

1. 在 `worker/app/adapters/bilibili.py` 增加 “API 执行通道”，real_run 改用本引擎而非 Playwright 选择器。
2. 真实凭据沿用现有路径：`accounts.encrypted_credential`（AES）→ worker 按 `account_id` 解密取 cookie。
3. 复用现有可靠性底座（outbox/恢复租约/死信/心跳/配额）。
4. **无人值守**：新增自动派发循环 + `daily_task_count` 每日重置（注意：该重置已在待合并的 PR #22 `scheduler.py` 里实现）。
5. 用本引擎替换/下线死代码 `core/app/adapters/bilibili/hybrid_executor.py`（破损 import、`path/to/...` 占位，从未接入）。
