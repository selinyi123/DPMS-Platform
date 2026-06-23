#!/usr/bin/env python3
"""Live self-test for the Bilibili direct-API engine.

Run this on your own machine with a **throwaway** Bilibili account cookie to
verify the engine talks to the real Bilibili API correctly. Claude never sees
your cookie: it is read from the BILI_COOKIE environment variable (or --cookie).

SAFETY
------
* Default mode is READ-ONLY: it validates the cookie, checks wbi signing works,
  and (optionally) scans a UP's dynamics and parses any lotteries. No follows,
  reposts, or comments.
* Performing real state-changing actions requires BOTH ``--act ...`` AND
  ``--yes``. Automating follow/repost/comment may violate Bilibili's ToS and can
  get an account restricted. Use a low-value test account and run sparingly.

Examples
--------
  # 1) cookie + wbi sanity check (read-only)
  BILI_COOKIE='SESSDATA=...; bili_jct=...; DedeUserID=...' \
      python worker/tools/bilibili_api_selftest.py

  # 2) scan a UP's space for lottery dynamics (read-only)
  python worker/tools/bilibili_api_selftest.py --scan 401742377

  # 3) parse one dynamic by id or URL (read-only)
  python worker/tools/bilibili_api_selftest.py --detail https://t.bilibili.com/123456789

  # 4) ACTUALLY participate in one lottery (opt-in, live writes!)
  python worker/tools/bilibili_api_selftest.py \
      --target https://t.bilibili.com/123456789 --act follow,like,repost,comment --yes
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> import app.*

from app.bilibili import (  # noqa: E402
    BiliEngineConfig,
    BilibiliApiClient,
    BilibiliApiExecutor,
    looks_like_lottery,
    parse_dynamic_card,
    parse_feed,
)

_DYID_RE = re.compile(r"(?:/opus/|t\.bilibili\.com/|/dynamic/)(\d{10,})|(\d{12,})")


def extract_dynamic_id(value: str) -> str:
    m = _DYID_RE.search(value.strip())
    if not m:
        raise SystemExit(f"无法从 '{value}' 解析出 dynamic_id（请用完整 t.bilibili.com 链接或纯数字 id；b23.tv 短链请先在浏览器展开）")
    return m.group(1) or m.group(2)


def _print(title, *lines):
    print(f"\n=== {title} ===")
    for ln in lines:
        print(f"  {ln}")


async def run(args) -> int:
    cookie = args.cookie or os.environ.get("BILI_COOKIE", "")
    if not cookie:
        raise SystemExit("缺少 cookie：设置环境变量 BILI_COOKIE 或传 --cookie（仅本地使用）")

    async with BilibiliApiClient(cookie, config=BiliEngineConfig()) as client:
        # 1) login + wbi
        logged_in = await client.check_login()
        _print("登录校验", f"uid={client.uid}", f"csrf={'有' if client.csrf else '缺失!'}", f"isLogin={logged_in}")
        if not logged_in:
            print("\n[X] Cookie 无效或已失效，后续测试中止。")
            return 1
        info = (await client.get_myinfo()).get("data") or {}
        _print("账号信息", f"name={info.get('name')}", f"mid={info.get('mid')}", f"level={info.get('level')}")
        print("\n[OK] Cookie 有效，wbi 签名链路正常（myinfo/nav 已成功）。")

        # 2) optional scan
        if args.scan:
            cfg = client.config
            offset, found, page = "", 0, 0
            while page < cfg.scan_pages:
                data = await client.get_space_dynamics(args.scan, offset)
                d = data.get("data") or {}
                cards = parse_feed(data)
                for c in cards:
                    if looks_like_lottery(c):
                        found += 1
                        tag = "官方互动抽奖" if (c.has_official_lottery or (c.origin and c.origin.has_official_lottery)) else "疑似转发抽奖"
                        _print(f"发现抽奖 #{found}", f"dynamic_id={c.dynamic_id}", f"up={c.uname}({c.uid})", f"类型={tag}", f"描述={c.description[:60]}")
                page += 1
                offset = d.get("offset", "")
                if not d.get("has_more") or not offset:
                    break
                await asyncio.sleep(cfg.scan_page_wait)
            _print("扫描完成", f"扫描 {page} 页", f"命中疑似抽奖 {found} 条")

        # 3) optional detail
        if args.detail:
            dyid = extract_dynamic_id(args.detail)
            data = await client.get_dynamic_detail(dyid)
            card = parse_dynamic_card(((data.get("data") or {}).get("item")))
            _print("动态解析", f"dynamic_id={card.dynamic_id}", f"up={card.uname}({card.uid})",
                   f"rid_str={card.rid_str}", f"chat_type={card.chat_type}",
                   f"官方抽奖={card.has_official_lottery}", f"是抽奖={looks_like_lottery(card)}")

        # 4) opt-in live actions
        if args.act:
            actions = [a.strip() for a in args.act.split(",") if a.strip()]
            if not args.target:
                raise SystemExit("--act 需要同时指定 --target <动态链接或id>")
            if not args.yes:
                raise SystemExit("将执行真实写操作（关注/转发/评论）。确认无误请加 --yes 重跑。")
            dyid = extract_dynamic_id(args.target)
            data = await client.get_dynamic_detail(dyid)
            card = parse_dynamic_card(((data.get("data") or {}).get("item")))
            if not card.dynamic_id:
                raise SystemExit(f"未能解析目标动态 {dyid}")
            print(f"\n[!] 即将对 dynamic_id={card.dynamic_id} (up={card.uname}) 执行真实动作: {actions}")
            result = await BilibiliApiExecutor(client, client.config).participate(card, actions)
            _print("执行结果", f"success={result.success}", f"aborted={result.aborted} {result.abort_reason}")
            for name, r in result.actions.items():
                print(f"    - {name}: {r.outcome.value} (code={r.code}) {r.message}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Bilibili direct-API engine live self-test")
    p.add_argument("--cookie", help="raw Cookie header (否则取环境变量 BILI_COOKIE)")
    p.add_argument("--scan", type=int, metavar="UID", help="扫描某 UP 空间动态，列出疑似抽奖（只读）")
    p.add_argument("--detail", metavar="ID_OR_URL", help="解析单条动态（只读）")
    p.add_argument("--target", metavar="ID_OR_URL", help="真实参与的目标动态（配合 --act）")
    p.add_argument("--act", metavar="a,b,c", help="真实执行的动作，逗号分隔: follow,like,repost,comment")
    p.add_argument("--yes", action="store_true", help="确认执行真实写操作（与 --act 同用）")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
