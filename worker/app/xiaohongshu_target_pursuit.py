"""Read-only Xiaohongshu browser discovery consumer.

This lane may navigate, expand text, and read DOM evidence.  It never invokes
follow, like, favorite, comment, share, or publish controls.  Browser
credentials remain inside the account profile and are never serialized into
Redis results.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit

from app.db import database, redis
from app.safety import detect_page_risk
from app.task_runner import prepare_account_login
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from shared.redis_consumer_groups import verify_redis_consumer_group
from shared.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA
from shared.xiaohongshu_target_pursuit_streams import (
    XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
    XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH,
    XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
    validate_target_pursuit_stream_fields,
    xiaohongshu_target_pursuit_result_key,
)


TARGET_PURSUIT_RESULT_TTL_SECONDS = 600
TARGET_PURSUIT_RESULT_MAX_BYTES = 512 * 1024
TARGET_PURSUIT_REQUEST_MAX_AGE_MILLISECONDS = 90_000
TARGET_PURSUIT_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS = 30_000
TARGET_PURSUIT_RECLAIM_IDLE_MILLISECONDS = (
    TARGET_PURSUIT_REQUEST_MAX_AGE_MILLISECONDS
    + TARGET_PURSUIT_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS
)
TARGET_PURSUIT_RECLAIM_INTERVAL_SECONDS = 15
TARGET_PURSUIT_RECLAIM_COUNT = 20
TARGET_PURSUIT_PAGE_TIMEOUT_MILLISECONDS = 20_000
TARGET_PURSUIT_SCAN_TIMEOUT_SECONDS = 75
TARGET_PURSUIT_TEXT_LIMIT = 8_000
TARGET_PURSUIT_CONSUMER_NAME = WORKER_ID
XIAOHONGSHU_HOSTS = frozenset(
    {"xiaohongshu.com", "www.xiaohongshu.com"}
)
XIAOHONGSHU_NOTE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
XIAOHONGSHU_PROFILE_ID_PATTERN = re.compile(
    r"^[0-9A-Za-z_-]{5,64}$"
)
MUTATING_REQUEST_MARKERS = (
    "/like",
    "/likes",
    "/follow",
    "/unfollow",
    "/collect",
    "/favorite",
    "/comment/post",
    "/comment/create",
    "/comment/delete",
    "/publish",
    "/share/report",
)
READ_ONLY_CONTEXT_INIT_SCRIPT = f"""
(() => {{
  'use strict';
  const safeMethods = new Set(['GET', 'HEAD', 'OPTIONS']);
  const mutatingMarkers = Object.freeze(
    {json.dumps(list(MUTATING_REQUEST_MARKERS))}
  );
  const reject = () => {{
    throw new DOMException(
      'xiaohongshu_target_pursuit_read_only',
      'SecurityError'
    );
  }};
  const assertReadOnly = (method, rawUrl) => {{
    const normalizedMethod = String(method || 'GET').toUpperCase();
    let path = '';
    try {{
      path = new URL(String(rawUrl || ''), window.location.href)
        .pathname.toLowerCase();
    }} catch (_error) {{
      reject();
    }}
    if (
      !safeMethods.has(normalizedMethod)
      || mutatingMarkers.some(marker => path.includes(marker))
    ) {{
      reject();
    }}
  }};

  Object.defineProperty(window, 'WebSocket', {{
    configurable: false,
    writable: false,
    value: function ReadOnlyBlockedWebSocket() {{ reject(); }}
  }});
  Object.defineProperty(window, 'EventSource', {{
    configurable: false,
    writable: false,
    value: function ReadOnlyBlockedEventSource() {{ reject(); }}
  }});
  Object.defineProperty(window, 'Worker', {{
    configurable: false,
    writable: false,
    value: function ReadOnlyBlockedWorker() {{ reject(); }}
  }});
  Object.defineProperty(window, 'SharedWorker', {{
    configurable: false,
    writable: false,
    value: function ReadOnlyBlockedSharedWorker() {{ reject(); }}
  }});
  try {{
    Object.defineProperty(navigator, 'sendBeacon', {{
      configurable: false,
      writable: false,
      value: () => false
    }});
  }} catch (_error) {{
    navigator.sendBeacon = () => false;
  }}

  const originalFetch = window.fetch.bind(window);
  Object.defineProperty(window, 'fetch', {{
    configurable: false,
    writable: false,
    value: (input, init = undefined) => {{
      const method = (
        init && init.method
      ) || (
        input && typeof input === 'object' && input.method
      ) || 'GET';
      const url = (
        input && typeof input === 'object' && input.url
      ) || input;
      assertReadOnly(method, url);
      return originalFetch(input, init);
    }}
  }});

  const xhrOpen = XMLHttpRequest.prototype.open;
  Object.defineProperty(XMLHttpRequest.prototype, 'open', {{
    configurable: false,
    writable: false,
    value: function(method, url, ...rest) {{
      assertReadOnly(method, url);
      return xhrOpen.call(this, method, url, ...rest);
    }}
  }});

  const disableForm = function() {{ reject(); }};
  Object.defineProperty(HTMLFormElement.prototype, 'submit', {{
    configurable: false,
    writable: false,
    value: disableForm
  }});
  Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', {{
    configurable: false,
    writable: false,
    value: disableForm
  }});
  window.addEventListener('submit', event => {{
    event.preventDefault();
    event.stopImmediatePropagation();
  }}, true);
}})();
"""


class TargetPursuitBrowserError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def _bounded_text(value, limit: int = TARGET_PURSUIT_TEXT_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _canonical_profile_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise TargetPursuitBrowserError(
            "xiaohongshu_author_profile_url_invalid"
        ) from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or host not in XIAOHONGSHU_HOSTS
        or len(parts) != 3
        or parts[:2] != ["user", "profile"]
        or not XIAOHONGSHU_PROFILE_ID_PATTERN.fullmatch(parts[2])
    ):
        raise TargetPursuitBrowserError(
            "xiaohongshu_author_profile_url_invalid"
        )
    return urlunsplit(
        ("https", "www.xiaohongshu.com", f"/user/profile/{parts[2]}", "", "")
    )


def _canonical_note_url(value: str) -> str | None:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    note_id = None
    if (
        parsed.scheme.casefold() == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and host in XIAOHONGSHU_HOSTS
    ):
        if (
            len(parts) == 2
            and parts[0] == "explore"
            and XIAOHONGSHU_NOTE_ID_PATTERN.fullmatch(parts[1])
        ):
            note_id = parts[1]
        elif (
            len(parts) == 3
            and parts[:2] == ["discovery", "item"]
            and XIAOHONGSHU_NOTE_ID_PATTERN.fullmatch(parts[2])
        ):
            note_id = parts[2]
    if note_id is None:
        return None
    return f"https://www.xiaohongshu.com/explore/{note_id.casefold()}"


def _source_entry_url(source_type: str, source_value: str) -> str:
    if source_type == "keyword":
        keyword = str(source_value or "").strip()
        if not keyword:
            raise TargetPursuitBrowserError(
                "xiaohongshu_keyword_required"
            )
        if len(keyword) > (
            XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH
        ):
            raise TargetPursuitBrowserError(
                "xiaohongshu_keyword_too_long"
            )
        return (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={quote(keyword, safe='')}"
            "&source=web_search_result_notes"
        )
    if source_type == "author_profile":
        return _canonical_profile_url(source_value)
    raise TargetPursuitBrowserError(
        "xiaohongshu_target_pursuit_browser_source_unsupported"
    )


def _safe_error_code(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or "").strip()
    if re.fullmatch(r"[a-z0-9_:-]{1,128}", code):
        return code
    if isinstance(exc, asyncio.TimeoutError):
        return "xiaohongshu_target_pursuit_timeout"
    return f"xiaohongshu_target_pursuit_{type(exc).__name__}".casefold()[:128]


def _request_is_read_only(method: str, url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    lowered_path = parsed.path.casefold()
    return (
        str(method or "").upper() in {"GET", "HEAD", "OPTIONS"}
        and not any(
            marker in lowered_path
            for marker in MUTATING_REQUEST_MARKERS
        )
    )


async def _install_read_only_guard(context) -> None:
    async def guard(route):
        request = route.request
        url = str(request.url or "")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").casefold()
        if request.is_navigation_request():
            if (
                parsed.scheme.casefold() != "https"
                or host not in XIAOHONGSHU_HOSTS
            ):
                await route.abort()
                return
        if not _request_is_read_only(request.method, url):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", guard)
    await context.add_init_script(READ_ONLY_CONTEXT_INIT_SCRIPT)


async def _new_isolated_read_only_context(
    pool,
    persistent_context,
):
    storage_state = await persistent_context.storage_state()
    browser, _browser_id = await pool.get_available_browser()
    context = await browser.new_context(
        storage_state=storage_state,
        service_workers="block",
        accept_downloads=False,
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    try:
        await _install_read_only_guard(context)
    except BaseException:
        await context.close()
        raise
    return context


async def _select_read_only_account() -> dict:
    row = await database.fetch_one(
        """SELECT a.id, a.execution_revision
           FROM accounts a
           WHERE a.platform = 'xiaohongshu'
             AND a.status = 'ready'
             AND a.deleted_at IS NULL
             AND NOT EXISTS (
               SELECT 1
               FROM account_operation_leases lease_row
               WHERE lease_row.account_id = a.id
                 AND lease_row.released_at IS NULL
                 AND lease_row.expires_at > NOW()
             )
           ORDER BY COALESCE(a.last_active_at, a.created_at) DESC, a.id ASC
           LIMIT 1"""
    )
    if not row:
        raise TargetPursuitBrowserError(
            "xiaohongshu_target_pursuit_ready_account_required"
        )
    return {
        "id": int(row["id"]),
        "execution_revision": int(row["execution_revision"]),
    }


async def _expand_read_only_text(page) -> None:
    for label in ("展开全文", "展开"):
        locator = page.get_by_text(label, exact=True)
        count = min(await locator.count(), 3)
        for index in range(count):
            try:
                await locator.nth(index).click(timeout=800)
            except Exception:
                continue


async def _collect_note_links(page, limit: int) -> list[dict]:
    rows = await page.locator(
        'a[href*="/explore/"], a[href*="/discovery/item/"]'
    ).evaluate_all(
        """(anchors) => anchors.slice(0, 300).map(anchor => {
          const card = anchor.closest(
            'section, article, [class*="note-item"], [class*="search-result"]'
          ) || anchor.parentElement;
          const author = card && card.querySelector('a[href*="/user/profile/"]');
          return {
            raw_url: anchor.href || anchor.getAttribute('href') || '',
            card_text: (card && card.innerText) || anchor.innerText || '',
            author_profile_url: author ? (author.href || author.getAttribute('href') || '') : '',
            author_name: author ? (author.innerText || '') : ''
          };
        })"""
    )
    candidates = []
    seen = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        canonical = _canonical_note_url(row.get("raw_url"))
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        profile_url = ""
        try:
            profile_url = _canonical_profile_url(
                row.get("author_profile_url")
            )
        except TargetPursuitBrowserError:
            pass
        candidates.append(
            {
                "raw_url": canonical,
                "card_text": _bounded_text(row.get("card_text"), 1_500),
                "author": {
                    "profile_url": profile_url or None,
                    "display_name": _bounded_text(
                        row.get("author_name"), 128
                    )
                    or None,
                },
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


async def _detail_dom_snapshot(page) -> dict:
    return await page.evaluate(
        """() => {
          const firstText = (selectors) => {
            for (const selector of selectors) {
              const node = document.querySelector(selector);
              const text = node && (node.innerText || node.textContent || '').trim();
              if (text) return text;
            }
            return '';
          };
          const profile = document.querySelector(
            '[class*="author"] a[href*="/user/profile/"],'
            + ' a[href*="/user/profile/"]'
          );
          const commentNodes = Array.from(document.querySelectorAll(
            '[class*="comment-item"], [class*="commentItem"], .comment-item'
          )).slice(0, 100);
          let pinned = null;
          for (const node of commentNodes) {
            const text = (node.innerText || node.textContent || '').trim();
            if (!text.includes('置顶')) continue;
            const author = node.querySelector('a[href*="/user/profile/"]');
            pinned = {
              text,
              author_profile_url: author ? (author.href || author.getAttribute('href') || '') : '',
              author_name: author ? (author.innerText || '') : ''
            };
            break;
          }
          const original = Array.from(document.querySelectorAll(
            'a[href*="/explore/"], a[href*="/discovery/item/"]'
          )).find(node => /原帖|原笔记|查看原文/.test(
            node.innerText || node.textContent || ''
          ));
          const body = firstText([
            '#detail-desc', '[class*="note-text"]', '[class*="desc"]',
            '[class*="content"]'
          ]);
          const title = firstText([
            '#detail-title', '[class*="title"]', 'h1'
          ]);
          const published = firstText([
            '[class*="date"]', '[class*="time"]'
          ]);
          const pageText = (document.body && document.body.innerText) || '';
          return {
            title,
            body_text: body,
            expanded_text: body,
            published_text: published,
            page_text: pageText,
            author_profile_url: profile ? (profile.href || profile.getAttribute('href') || '') : '',
            author_name: profile ? (profile.innerText || '') : '',
            pinned_comment: pinned,
            original_note_url: original ? (original.href || original.getAttribute('href') || '') : '',
            is_collection: /合集|系列笔记|收录于/.test(pageText)
          };
        }"""
    )


async def _capture_detail_snapshots(page) -> dict:
    """Preserve the exact pre-expansion body separately from expanded text."""

    initial = dict(await _detail_dom_snapshot(page) or {})
    raw_body = initial.get("body_text")
    await _expand_read_only_text(page)
    expanded = dict(await _detail_dom_snapshot(page) or {})
    snapshot = {**initial, **expanded}
    snapshot["body_text"] = raw_body
    snapshot["expanded_text"] = (
        expanded.get("body_text")
        or expanded.get("expanded_text")
        or raw_body
    )
    return snapshot


def _source_observation_fields(
    source_type: str,
    source_value: str,
    entry_url: str,
    note_url: str,
    *,
    observed_at: str,
) -> dict:
    observation = {
        "source_type": source_type,
        "source_value": (
            source_value if source_type == "keyword" else entry_url
        ),
        "entry_url": entry_url,
        "note_url": note_url,
        "observed_at": observed_at,
    }
    fields = {"source_observation": observation}
    if source_type == "keyword":
        fields["search_result"] = {
            "query": source_value,
            "note_url": note_url,
            "search_url": entry_url,
            "observed_at": observed_at,
        }
    else:
        observation["author_profile_url"] = entry_url
    return fields


def _profile_identity(profile_url: str | None) -> dict:
    if not profile_url:
        return {"profile_url": None, "id": None}
    try:
        canonical = _canonical_profile_url(profile_url)
    except TargetPursuitBrowserError:
        return {"profile_url": None, "id": None}
    return {
        "profile_url": canonical,
        "id": canonical.rsplit("/", 1)[-1],
    }


async def _capture_original_note(
    context,
    original_url: str,
    *,
    account: dict,
) -> dict:
    page = await context.new_page()
    try:
        await page.goto(
            original_url,
            wait_until="domcontentloaded",
            timeout=TARGET_PURSUIT_PAGE_TIMEOUT_MILLISECONDS,
        )
        if _canonical_note_url(page.url) != original_url:
            raise TargetPursuitBrowserError(
                "xiaohongshu_original_note_identity_mismatch"
            )
        await page.wait_for_timeout(500)
        await detect_page_risk(
            page,
            account["id"],
            "xiaohongshu",
            expected_execution_revision=account["execution_revision"],
        )
        snapshot = await _capture_detail_snapshots(page)
        author_identity = _profile_identity(
            snapshot.get("author_profile_url")
        )
        pinned = snapshot.get("pinned_comment")
        if isinstance(pinned, dict):
            pinned_author = _profile_identity(
                pinned.get("author_profile_url")
            )
            pinned = {
                "text": _bounded_text(pinned.get("text"), 4_000),
                "is_pinned": True,
                "author": {
                    **pinned_author,
                    "display_name": _bounded_text(
                        pinned.get("author_name"), 128
                    )
                    or None,
                },
            }
        return {
            "raw_url": original_url,
            "title": _bounded_text(snapshot.get("title"), 256),
            "author": {
                **author_identity,
                "display_name": _bounded_text(
                    snapshot.get("author_name"), 128
                )
                or None,
            },
            "body_text": _bounded_text(snapshot.get("body_text")),
            "expanded_text": _bounded_text(
                snapshot.get("expanded_text")
            ),
            "pinned_comment": pinned,
            "published_text": _bounded_text(
                snapshot.get("published_text"), 256
            ),
            "is_collection": bool(snapshot.get("is_collection")),
            "original_note_url": _canonical_note_url(
                snapshot.get("original_note_url")
            ),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "capture_method": "playwright_read_only_original_dom_v1",
        }
    finally:
        await page.close()


async def _hydrate_candidate(
    context,
    candidate: dict,
    *,
    account: dict,
) -> dict:
    page = await context.new_page()
    try:
        await page.goto(
            candidate["raw_url"],
            wait_until="domcontentloaded",
            timeout=TARGET_PURSUIT_PAGE_TIMEOUT_MILLISECONDS,
        )
        final_url = _canonical_note_url(page.url)
        if final_url != candidate["raw_url"]:
            raise TargetPursuitBrowserError(
                "xiaohongshu_target_note_identity_mismatch"
            )
        await page.wait_for_timeout(750)
        await detect_page_risk(
            page,
            account["id"],
            "xiaohongshu",
            expected_execution_revision=account["execution_revision"],
        )
        snapshot = await _capture_detail_snapshots(page)
        author_profile = _profile_identity(
            snapshot.get("author_profile_url")
            or (candidate.get("author") or {}).get("profile_url")
        )
        pinned = snapshot.get("pinned_comment")
        if isinstance(pinned, dict):
            pinned_identity = _profile_identity(
                pinned.get("author_profile_url")
            )
            pinned = {
                "text": _bounded_text(pinned.get("text"), 4_000),
                "is_pinned": True,
                "author": {
                    **pinned_identity,
                    "display_name": _bounded_text(
                        pinned.get("author_name"), 128
                    )
                    or None,
                },
            }
        original_url = _canonical_note_url(
            snapshot.get("original_note_url")
        )
        result = {
            "raw_url": candidate["raw_url"],
            "title": _bounded_text(snapshot.get("title"), 256)
            or _bounded_text(candidate.get("card_text"), 256),
            "card_text": _bounded_text(
                candidate.get("card_text"), 1_500
            ),
            "author": {
                **author_profile,
                "display_name": _bounded_text(
                    snapshot.get("author_name")
                    or (candidate.get("author") or {}).get("display_name"),
                    128,
                )
                or None,
            },
            "body_text": _bounded_text(snapshot.get("body_text")),
            "expanded_text": _bounded_text(
                snapshot.get("expanded_text")
            ),
            "pinned_comment": pinned,
            "published_text": _bounded_text(
                snapshot.get("published_text"), 256
            ),
            "is_collection": bool(snapshot.get("is_collection")),
            "original_note_url": original_url,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "capture_method": "playwright_read_only_dom_v1",
            "source_observation": candidate.get(
                "source_observation"
            ),
        }
        if isinstance(candidate.get("search_result"), dict):
            result["search_result"] = candidate["search_result"]
        if original_url and original_url != candidate["raw_url"]:
            try:
                result["original_note"] = await _capture_original_note(
                    context,
                    original_url,
                    account=account,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result["original_note"] = {
                    "raw_url": original_url,
                    "capture_error": _safe_error_code(exc),
                }
        return result
    finally:
        await page.close()


async def collect_xiaohongshu_target_evidence(
    pool,
    source_type: str,
    source_value: str,
    *,
    max_candidates: int,
) -> list[dict]:
    entry_url = _source_entry_url(source_type, source_value)
    account = await _select_read_only_account()
    persistent_context = await pool.get_account_context(
        account["id"],
        f"/profiles/xiaohongshu/account_{account['id']}",
        platform="xiaohongshu",
    )
    await prepare_account_login(
        persistent_context,
        account["id"],
        "xiaohongshu",
    )
    context = await _new_isolated_read_only_context(
        pool,
        persistent_context,
    )
    try:
        page = await context.new_page()
        try:
            await page.goto(
                entry_url,
                wait_until="domcontentloaded",
                timeout=TARGET_PURSUIT_PAGE_TIMEOUT_MILLISECONDS,
            )
            await page.wait_for_timeout(1_500)
            await detect_page_risk(
                page,
                account["id"],
                "xiaohongshu",
                expected_execution_revision=account[
                    "execution_revision"
                ],
            )
            cards = await _collect_note_links(
                page,
                max_candidates,
            )
        finally:
            await page.close()

        for card in cards:
            observed_at = datetime.now(timezone.utc).isoformat()
            card.update(
                _source_observation_fields(
                    source_type,
                    source_value,
                    entry_url,
                    card["raw_url"],
                    observed_at=observed_at,
                )
            )

        evidence = []
        detail_deadline = asyncio.get_running_loop().time() + 50
        for index, card in enumerate(cards):
            remaining = (
                detail_deadline
                - asyncio.get_running_loop().time()
            )
            if remaining < 1:
                observed_at = datetime.now(timezone.utc).isoformat()
                evidence.extend(
                    {
                        **pending_card,
                        "observed_at": observed_at,
                        "capture_method": (
                            "playwright_read_only_card_v1"
                        ),
                        "capture_error": (
                            "xiaohongshu_target_pursuit_"
                            "detail_budget_exhausted"
                        ),
                    }
                    for pending_card in cards[index:]
                )
                break
            try:
                evidence.append(
                    await asyncio.wait_for(
                        _hydrate_candidate(
                            context,
                            card,
                            account=account,
                        ),
                        timeout=min(remaining, 15),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                evidence.append(
                    {
                        **card,
                        "observed_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "capture_method": (
                            "playwright_read_only_card_v1"
                        ),
                        "capture_error": _safe_error_code(exc),
                    }
                )
        return evidence
    finally:
        await context.close()


def _request_age_rejection(
    requested_at_ms: str,
    *,
    now_ms: int | None = None,
) -> str | None:
    try:
        value = int(requested_at_ms)
    except (TypeError, ValueError):
        return "invalid"
    observed = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if value <= 0:
        return "invalid"
    if value - observed > TARGET_PURSUIT_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS:
        return "future"
    if observed - value >= TARGET_PURSUIT_REQUEST_MAX_AGE_MILLISECONDS:
        return "stale"
    return None


async def _retire_message(message_id: str) -> None:
    result = list(
        await redis.eval(
            SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
            1,
            XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
            XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
            str(message_id),
        )
        or ()
    )
    if len(result) != 2:
        raise RuntimeError(
            "xiaohongshu_target_pursuit_terminal_ack_invalid"
        )


async def _publish_result(request_id: str, result: dict) -> None:
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > TARGET_PURSUIT_RESULT_MAX_BYTES:
        candidates = result.get("candidates")
        if result.get("status") == "completed" and isinstance(
            candidates, list
        ):
            envelope = {
                key: value
                for key, value in result.items()
                if key != "candidates"
            }
            kept = []
            for candidate in candidates:
                trial = {
                    **envelope,
                    "candidates": [*kept, candidate],
                    "truncated": True,
                    "dropped_count": len(candidates) - len(kept) - 1,
                }
                trial_encoded = json.dumps(
                    trial,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if len(trial_encoded.encode("utf-8")) > (
                    TARGET_PURSUIT_RESULT_MAX_BYTES
                ):
                    break
                kept.append(candidate)
            result = {
                **envelope,
                "candidates": kept,
                "truncated": True,
                "dropped_count": len(candidates) - len(kept),
            }
            encoded = json.dumps(
                result,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        if len(encoded.encode("utf-8")) > TARGET_PURSUIT_RESULT_MAX_BYTES:
            encoded = json.dumps(
                {
                    "request_id": request_id,
                    "status": "failed",
                    "error_code": (
                        "xiaohongshu_target_pursuit_result_too_large"
                    ),
                    "candidates": [],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
    await redis.set(
        xiaohongshu_target_pursuit_result_key(request_id),
        encoded,
        ex=TARGET_PURSUIT_RESULT_TTL_SECONDS,
        nx=True,
    )


async def _handle_request(pool, message_id: str, fields: dict) -> None:
    request_id = None
    try:
        request = validate_target_pursuit_stream_fields(fields)
        request_id = request["request_id"]
        age_rejection = _request_age_rejection(
            request["requested_at_ms"]
        )
        if age_rejection is not None:
            raise TargetPursuitBrowserError(
                f"xiaohongshu_target_pursuit_request_{age_rejection}"
            )
        candidates = await asyncio.wait_for(
            collect_xiaohongshu_target_evidence(
                pool,
                request["source_type"],
                request["source_value"],
                max_candidates=int(request["max_candidates"]),
            ),
            timeout=TARGET_PURSUIT_SCAN_TIMEOUT_SECONDS,
        )
        result = {
            "request_id": request_id,
            "status": "completed",
            "source_type": request["source_type"],
            "source_value": request["source_value"],
            "candidates": candidates,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if request_id is None:
            structured_log(
                "warning",
                "xiaohongshu_target_pursuit_request_rejected",
                message_id=message_id,
                cause_type=type(exc).__name__,
            )
            await _retire_message(message_id)
            return
        result = {
            "request_id": request_id,
            "status": "failed",
            "error_code": _safe_error_code(exc),
            "candidates": [],
        }
    await _publish_result(request_id, result)
    await _retire_message(message_id)


async def _reclaim_stale_requests() -> int:
    pending = await redis.xpending_range(
        XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
        XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
        min="-",
        max="+",
        count=TARGET_PURSUIT_RECLAIM_COUNT,
        idle=TARGET_PURSUIT_RECLAIM_IDLE_MILLISECONDS,
    )
    retired = 0
    for entry in pending or ():
        message_id = str(entry.get("message_id") or "").strip()
        idle_ms = int(entry.get("time_since_delivered") or 0)
        if (
            not message_id
            or idle_ms < TARGET_PURSUIT_RECLAIM_IDLE_MILLISECONDS
        ):
            continue
        claimed = await redis.xclaim(
            XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
            XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
            TARGET_PURSUIT_CONSUMER_NAME,
            min_idle_time=TARGET_PURSUIT_RECLAIM_IDLE_MILLISECONDS,
            message_ids=[message_id],
        )
        for claimed_id, fields in claimed or ():
            request_id = None
            try:
                request = validate_target_pursuit_stream_fields(
                    dict(fields or {})
                )
                request_id = request["request_id"]
            except (TypeError, ValueError, UnicodeError):
                pass
            if request_id is not None:
                await _publish_result(
                    request_id,
                    {
                        "request_id": request_id,
                        "status": "failed",
                        "error_code": (
                            "xiaohongshu_target_pursuit_request_stale"
                        ),
                        "candidates": [],
                    },
                )
            await _retire_message(str(claimed_id))
            retired += 1
    return retired


async def xiaohongshu_target_pursuit_loop(
    pool,
    shutdown_event: asyncio.Event,
) -> None:
    await verify_redis_consumer_group(
        redis,
        stream_key=XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
        group_name=XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
    )
    read_id = "0"
    last_reclaim_at = float("-inf")
    while not shutdown_event.is_set():
        loop = asyncio.get_running_loop()
        if (
            read_id == ">"
            and loop.time() - last_reclaim_at
            >= TARGET_PURSUIT_RECLAIM_INTERVAL_SECONDS
        ):
            try:
                await _reclaim_stale_requests()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "xiaohongshu_target_pursuit_reclaim_failed",
                    cause_type=type(exc).__name__,
                )
            last_reclaim_at = loop.time()
        messages = await redis.xreadgroup(
            XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
            TARGET_PURSUIT_CONSUMER_NAME,
            {XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY: read_id},
            count=1,
            block=(None if read_id == "0" else 5_000),
        )
        if not messages:
            read_id = ">"
            continue
        for stream_name, entries in messages:
            if str(stream_name) != XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY:
                raise RuntimeError(
                    "xiaohongshu_target_pursuit_stream_mismatch"
                )
            for message_id, fields in entries:
                await _handle_request(
                    pool,
                    str(message_id),
                    dict(fields or {}),
                )
        read_id = ">"


__all__ = (
    "MUTATING_REQUEST_MARKERS",
    "TargetPursuitBrowserError",
    "_request_is_read_only",
    "collect_xiaohongshu_target_evidence",
    "xiaohongshu_target_pursuit_loop",
)
