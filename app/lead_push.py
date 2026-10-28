"""Minimal, lead-only integration with the gdb15 webhook."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .settings import get_setting

_MAX_CONCURRENT_PUSHES = 6


def lead_push_settings() -> dict:
    return {
        "enabled": get_setting("lead_push_enabled", "0") == "1",
        "url": get_setting("lead_push_url", "https://www.gdb15.com/api/v1/leads/webhook"),
        "api_key": get_setting("lead_push_api_key", ""),
    }


def _source_url(platform: str, item_id: str) -> str:
    if platform == "xhs":
        return f"https://www.xiaohongshu.com/explore/{item_id}"
    if platform in ("kuaishou", "ks"):
        return f"https://www.kuaishou.com/short-video/{item_id}"
    return f"https://www.douyin.com/video/{item_id}"


def _captured_at(create_time: int) -> str:
    seconds = int(create_time or 0)
    if seconds > 1_000_000_000_000:
        seconds //= 1000
    if seconds <= 0:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _matched_terms(text: str, terms: list) -> list[str]:
    folded = str(text or "").casefold()
    return [str(term) for term in terms if str(term).casefold() in folded]


async def push_comment_leads(
    comments: list[dict],
    *,
    platform: str,
    item_id: str,
    intent_keywords: list | None = None,
    intent_label: str = "",
    source_keyword: str = "",
) -> dict:
    """Push already-filtered public comments.

    Only public comment data is sent: the comment text, its public author id
    and nickname. Our own account credentials are never included.

    intent_label overrides the auto-matched intent words. Keyword collection
    uses it to carry the search keyword that surfaced the lead.

    source_keyword is the attribution of the lead: the search keyword for
    keyword collection, the watch alias for comment monitoring. The receiving
    CRM groups its funnel report by it.
    """
    settings = lead_push_settings()
    if not settings["enabled"] or not comments:
        return {"enabled": settings["enabled"], "pushed": 0, "skipped": 0, "errors": []}
    if not settings["url"] or not settings["api_key"]:
        return {"enabled": True, "pushed": 0, "skipped": len(comments),
                "errors": ["gdb15 URL 或 API Key 未配置"]}

    pushed = skipped = 0
    errors: list[str] = []
    # The webhook answers immediately, but a lead lost to a slow response is
    # a lead lost forever, so keep a generous budget.
    timeout = httpx.Timeout(30.0, connect=10.0)
    limit = asyncio.Semaphore(_MAX_CONCURRENT_PUSHES)

    async def push_payload(client, payload) -> tuple[int, int, list[str]]:
        async with limit:
            for attempt in range(2):
                try:
                    response = await client.post(
                        settings["url"], json=payload,
                        headers={"x-api-key": settings["api_key"]})
                except Exception as exc:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    return 0, 0, [repr(exc)]
                if response.status_code in (200, 201, 409):
                    if response.status_code == 409:
                        return 0, 1, []
                    return 1, 0, []
                if response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                return 0, 0, [f"HTTP {response.status_code}: {response.text[:180]}"]
        return 0, 0, []

    tasks = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for comment in comments:
            text = str(comment.get("text") or "").strip()
            comment_id = str(comment.get("comment_id") or "").strip()
            if not text or not item_id:
                skipped += 1
                continue
            if not comment_id:
                skipped += 1
                continue
            matched = _matched_terms(text, intent_keywords or [])
            payload = {
                "platform": platform,
                "sourceUrl": _source_url(platform, item_id),
                "commentId": comment_id,
                "commentText": text[:2000],
                "intentKeyword": intent_label or ", ".join(matched),
                "capturedAt": _captured_at(int(comment.get("create_time") or 0)),
                "source": "CREATORHUB",
            }
            attribution = str(source_keyword or "").strip()[:120]
            if attribution:
                payload["sourceKeyword"] = attribution
            user_id = str(comment.get("user_sec_uid") or "").strip()
            if user_id:
                payload["externalUserId"] = user_id
            nickname = str(comment.get("user_nickname") or "").strip()
            if nickname:
                payload["nickname"] = nickname
            tasks.append(push_payload(client, payload))
        # Push concurrently: one keyword collection can yield hundreds of
        # comments, and doing that one at a time would stall the collection.
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            errors.append(repr(result))
            continue
        got, missed, problems = result
        pushed += got
        skipped += missed
        errors.extend(problems)
    return {"enabled": True, "pushed": pushed, "skipped": skipped, "errors": errors[:5]}


def validate_lead_push_url(value: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("gdb15 Webhook URL 无效") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("gdb15 Webhook URL 必须是 HTTPS 地址，且不能包含账号密码")
    if len(value) > 2000:
        raise ValueError("gdb15 Webhook URL 过长")
    return value
