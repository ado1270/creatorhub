"""Minimal, lead-only integration with the gdb15 webhook."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from sqlalchemy import func
from sqlmodel import select

from .db import get_session
from .models import LeadPushOutbox
from .settings import get_setting

log = logging.getLogger(__name__)

_MAX_CONCURRENT_PUSHES = 6
# 推送限速。gdb15 是对外服务商的站点，前面挂着防 CC 的 WAF：
# 一次推上百条会瞬间打出十几 QPS，直接把整条推送通道封掉，线索全丢。
# 慢一点无所谓——线索晚几分钟到 CRM，比永远推不到强得多。
_PUSH_RATE_PER_SECOND = 4.0
# 补推退避：1 分钟、5 分钟、15 分钟、1 小时，之后每 6 小时一次。
_RETRY_STEPS = (60, 300, 900, 3600, 21600)
# 重试多少次还不成功，就不再自动重试了，标成 failed 让客户看得见。
# 一直重试一条永远送不出去的线索没有意义，但把它悄悄丢掉也不行——
# 获客系统丢一条线索就是丢一次成交机会。
_MAX_OUTBOX_ATTEMPTS = 20
# 暂存箱最多留多少条待推。客户一直没配好 Key 的话，每小时的采集会不断往里塞，
# 不设上限迟早把磁盘撑满。超了就丢最旧的，并留下日志。
_MAX_OUTBOX_PENDING = 5000


class _RateLimiter:
    """全局令牌桶。所有推送请求共用一个节奏，不只是并发数受控。

    注意 asyncio.Lock 绑定在创建它的那个事件循环上，所以这个对象必须整个
    进程只有一个。测试里每个用例一个事件循环，模块级单例在换 loop 时要
    重建锁，否则会抛 "is bound to a different event loop"。
    """

    def __init__(self, rate_provider):
        # 速率从模块变量实时取，方便运行时调速和测试里临时改。
        self._rate_provider = rate_provider
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None
        self._next_at = 0.0

    def _current_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
            self._next_at = 0.0
        return self._lock

    async def acquire(self) -> None:
        async with self._current_lock():
            loop = asyncio.get_running_loop()
            wait = self._next_at - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            rate = max(0.1, float(self._rate_provider()))
            self._next_at = loop.time() + (1.0 / rate)


# 整个进程共用一个限速器：采集推送和后台补推加起来也只能是 _PUSH_RATE_PER_SECOND。
# 之前每次调用 send_payloads 都新建一个限速器，两条通道各打 4 QPS，叠加起来
# 还是会把 gdb15 前面的防 CC 打爆——而那正是最初丢线索的原因。
_PUSH_LIMITER = _RateLimiter(lambda: _PUSH_RATE_PER_SECOND)


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


def build_payloads(
    comments: list[dict],
    *,
    platform: str,
    item_id: str,
    intent_keywords: list | None = None,
    intent_label: str = "",
    source_keyword: str = "",
) -> list[dict]:
    """把评论转成 gdb15 webhook 需要的 payload。

    造 payload 和发送分开，是为了让失败的 payload 能落库补推——之前失败只
    写一行日志，水位线照旧往前推，线索就这么没了。
    """
    payloads: list[dict] = []
    for comment in comments:
        text = str(comment.get("text") or "").strip()
        comment_id = str(comment.get("comment_id") or "").strip()
        if not text or not item_id or not comment_id:
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
        payloads.append(payload)
    return payloads


async def send_payloads(payloads: list[dict]) -> tuple[list[dict], list[str]]:
    """并发推送。返回 (没送出去的 payload 列表, 错误说明列表)。

    gdb15 对重复的 commentId 回 409，那不算失败——之前已经送到了。
    """
    settings = lead_push_settings()
    errors: list[str] = []
    failed: list[dict] = []
    if not payloads:
        return failed, errors
    if not settings["url"] or not settings["api_key"]:
        # 错误列表和失败列表必须一一对应，调用方靠这个对应关系
        # 给每条线索记自己的原因。这里只给一条的话，
        # 补推时会把没对上号的线索误判成「已送达」。
        why = "gdb15 URL 或 API Key 未配置"
        return list(payloads), [why] * len(payloads)

    # The webhook answers immediately, but a lead lost to a slow response is
    # a lead lost forever, so keep a generous budget.
    timeout = httpx.Timeout(30.0, connect=10.0)
    limit = asyncio.Semaphore(_MAX_CONCURRENT_PUSHES)
    pace = _PUSH_LIMITER

    async def push_payload(client, payload) -> tuple[bool, str]:
        async with limit:
            for attempt in range(2):
                await pace.acquire()
                try:
                    response = await client.post(
                        settings["url"], json=payload,
                        headers={"x-api-key": settings["api_key"]})
                except Exception as exc:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    return False, repr(exc)
                if response.status_code in (200, 201, 409):
                    return True, ""
                if response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                return False, f"HTTP {response.status_code}: {response.text[:180]}"
        return False, "未知错误"

    tasks = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for payload in payloads:
            tasks.append(push_payload(client, payload))
        # Push concurrently: one keyword collection can yield hundreds of
        # comments, and doing that one at a time would stall the collection.
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for payload, result in zip(payloads, results):
        if isinstance(result, BaseException):
            failed.append(payload)
            errors.append(repr(result))
            continue
        ok, problem = result
        if not ok:
            failed.append(payload)
            errors.append(problem)
    return failed, errors


def _retry_delay(attempts: int) -> int:
    index = max(0, int(attempts) - 1)
    if index < len(_RETRY_STEPS):
        return _RETRY_STEPS[index]
    return _RETRY_STEPS[-1]


def enqueue_payloads(payloads: list[dict], reason: str,
                     reasons: list[str] | None = None) -> int:
    """把没送出去的线索存进暂存箱，等条件好了再补推。

    reasons 是每条线索各自的失败原因；不给就用统一的 reason。
    一批线索可能失败在不同的原因上（401、超时、500），
    混成一句话的话，排查时看不出是哪一条出的问题。
    """
    if not payloads:
        return 0
    now = datetime.utcnow()
    saved = 0
    with get_session() as session:
        for index, payload in enumerate(payloads):
            comment_id = str(payload.get("commentId") or "").strip()
            if not comment_id:
                continue
            why = (reasons[index] if reasons and index < len(reasons) else reason)
            platform = str(payload.get("platform") or "douyin")
            row = session.exec(
                select(LeadPushOutbox)
                .where(LeadPushOutbox.platform == platform)
                .where(LeadPushOutbox.comment_id == comment_id)).first()
            body = json.dumps(payload, ensure_ascii=False)
            if row:
                row.payload_json = body
                row.attempts = int(row.attempts or 0) + 1
                row.last_error = why[:300]
                if row.attempts >= _MAX_OUTBOX_ATTEMPTS:
                    # 试了这么多回还送不出去，就别再自动重试了。
                    # 标成 failed 留在暂存箱里，客户能在设置页看到，
                    # 人工修好 Key 后可以重新入箱。
                    row.status = "failed"
                    row.next_retry_at = None
                else:
                    row.status = "pending"
                    row.next_retry_at = now + timedelta(
                        seconds=_retry_delay(row.attempts))
            else:
                source_url = str(payload.get("sourceUrl") or "")
                session.add(LeadPushOutbox(
                    platform=platform, comment_id=comment_id,
                    item_id=source_url.rsplit("/", 1)[-1][:60],
                    payload_json=body, attempts=1, last_error=why[:300],
                    status="pending",
                    next_retry_at=now + timedelta(seconds=_retry_delay(1))))
            saved += 1
        session.commit()
    _trim_outbox()
    return saved


def _trim_outbox() -> int:
    """暂存箱满了就丢最旧的待推记录。丢之前先记一笔，别悄悄消失。"""
    with get_session() as session:
        total = session.exec(
            select(func.count(LeadPushOutbox.id))
            .where(LeadPushOutbox.status != "sent")).one()
        total = int(total or 0)
        if total <= _MAX_OUTBOX_PENDING:
            return 0
        overflow = total - _MAX_OUTBOX_PENDING
        rows = session.exec(
            select(LeadPushOutbox)
            .where(LeadPushOutbox.status != "sent")
            .order_by(LeadPushOutbox.created_at)
            .limit(overflow)).all()
        for row in rows:
            session.delete(row)
        session.commit()
    log.warning("线索暂存箱已满，丢弃最旧的 %d 条待推记录", overflow)
    return overflow


def mark_delivered(payloads: list[dict]) -> int:
    """推送成功后，把暂存箱里同一条线索的旧记录一并标成已送达。

    一条线索可能先失败入了箱，之后又被新一轮采集推成功。入箱那条记录
    如果不自愈，后台会再把它推一遍（服务端按 commentId 去重，不会重复
    建线索，但白打一次请求），设置页也会一直显示「待补推」，
    客户看着像没送到——而实际上 gdb15 早就有了。
    """
    wanted: dict[str, set[str]] = {}
    for payload in payloads or []:
        comment_id = str(payload.get("commentId") or "").strip()
        if comment_id:
            wanted.setdefault(str(payload.get("platform") or "douyin"),
                              set()).add(comment_id)
    if not wanted:
        return 0
    now = datetime.utcnow()
    touched = 0
    with get_session() as session:
        for platform, comment_ids in wanted.items():
            rows = session.exec(
                select(LeadPushOutbox)
                .where(LeadPushOutbox.platform == platform)
                .where(LeadPushOutbox.comment_id.in_(list(comment_ids)))
                .where(LeadPushOutbox.status != "sent")).all()
            for row in rows:
                row.status = "sent"
                row.sent_at = now
                row.last_error = ""
                row.next_retry_at = None
                touched += 1
        if touched:
            session.commit()
    return touched


def cleanup_outbox(keep_days: int = 7) -> int:
    """删掉已经送达的旧记录。暂存箱只留未送达的和近期送达的。

    重试到放弃的记录也一并清掉，但留得久一点（keep_days 的四倍），
    客户想复盘的时候还看得见。
    """
    # keep_days=0 表示「已送达的全部清掉」，方便测试和手工收拾。
    cutoff = datetime.utcnow() - timedelta(days=max(0, keep_days))
    failed_cutoff = datetime.utcnow() - timedelta(days=max(0, keep_days) * 4)
    with get_session() as session:
        rows = session.exec(
            select(LeadPushOutbox)
            .where(LeadPushOutbox.status == "sent")
            .where(LeadPushOutbox.sent_at.is_(None)
                   | (LeadPushOutbox.sent_at < cutoff))).all()
        rows += session.exec(
            select(LeadPushOutbox)
            .where(LeadPushOutbox.status == "failed")
            .where(LeadPushOutbox.created_at < failed_cutoff)).all()
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)


def outbox_stats() -> dict:
    """暂存箱概况，给设置页显示。"""
    with get_session() as session:
        pending = session.exec(
            select(func.count(LeadPushOutbox.id))
            .where(LeadPushOutbox.status == "pending")).one()
        sent = session.exec(
            select(func.count(LeadPushOutbox.id))
            .where(LeadPushOutbox.status == "sent")).one()
        failed = session.exec(
            select(func.count(LeadPushOutbox.id))
            .where(LeadPushOutbox.status == "failed")).one()
        rows = session.exec(
            select(LeadPushOutbox)
            .where(LeadPushOutbox.status != "sent")
            .order_by(LeadPushOutbox.created_at.desc())
            .limit(20)).all()
    return {
        "pending": int(pending or 0),
        "sent": int(sent or 0),
        "failed": int(failed or 0),
        "items": [{
            "platform": row.platform,
            "comment_id": row.comment_id,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        } for row in rows],
    }


async def retry_outbox(limit: int = 200, *, force: bool = False) -> dict:
    """补推暂存箱里的线索。修好 Key 或网络恢复后调用。

    force=True 时无视退避时间立即发送——运维刚修好 Key，需要马上看到效果。
    """
    settings = lead_push_settings()
    if not settings["enabled"]:
        # 客户把推送开关关了，就别再偷偷往外发数据。
        # 暂存箱里的记录留着，等他重新打开开关再补推。
        return {"pushed": 0, "failed": 0, "remaining": outbox_stats()["pending"],
                "disabled": True}
    now = datetime.utcnow()
    with get_session() as session:
        query = (select(LeadPushOutbox)
                 .order_by(LeadPushOutbox.created_at)
                 .limit(limit))
        if not force:
            query = query.where(
                LeadPushOutbox.status == "pending"
            ).where((LeadPushOutbox.next_retry_at.is_(None))
                    | (LeadPushOutbox.next_retry_at <= now))
        else:
            # 手动点「立即补推」时，把之前放弃的也捡回来重新试一次。
            # 自动重试已经放弃的条目是没意义的，但人工刚修好 Key 时
            # 应该给客户一次翻盘的机会。
            query = query.where(
                LeadPushOutbox.status.in_(["pending", "failed"]))
        rows = session.exec(query).all()
        payloads = []
        for row in rows:
            try:
                payloads.append(json.loads(row.payload_json))
            except (TypeError, ValueError):
                # 这条记录本身坏了，送不出去。标成 failed 让人能看见，
                # 之前标成 sent 就等于假装送达，线索就此消失。
                row.status = "failed"
                row.last_error = "暂存箱记录损坏，无法解析"
                row.next_retry_at = None
        session.commit()
    if not payloads:
        return {"pushed": 0, "failed": 0, "remaining": outbox_stats()["pending"]}

    failed, errors = await send_payloads(payloads)
    # 每条线索记住自己的失败原因，别把第一条的错误写到所有行头上。
    failed_keys = {}
    for payload, why in zip(failed, errors):
        key = (str(payload.get("platform") or ""),
               str(payload.get("commentId") or ""))
        failed_keys[key] = why
    now = datetime.utcnow()
    with get_session() as session:
        for payload in payloads:
            key = (str(payload.get("platform") or ""),
                   str(payload.get("commentId") or ""))
            row = session.exec(
                select(LeadPushOutbox)
                .where(LeadPushOutbox.platform == key[0])
                .where(LeadPushOutbox.comment_id == key[1])).first()
            if not row:
                continue
            if key in failed_keys:
                row.attempts = int(row.attempts or 0) + 1
                row.last_error = str(failed_keys[key])[:300]
                if row.attempts >= _MAX_OUTBOX_ATTEMPTS:
                    row.status = "failed"
                    row.next_retry_at = None
                else:
                    row.status = "pending"
                    row.next_retry_at = now + timedelta(
                        seconds=_retry_delay(row.attempts))
            else:
                row.status = "sent"
                row.sent_at = now
                row.last_error = ""
        session.commit()
    pushed = len(payloads) - len(failed)
    if pushed or failed_keys:
        log.info("gdb15 线索补推：成功 %d 条，失败 %d 条", pushed, len(failed))
    return {"pushed": pushed, "failed": len(failed),
            "remaining": outbox_stats()["pending"]}


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

    送不出去的线索会落进暂存箱，等条件好了自动补推，绝不静默丢弃。
    """
    settings = lead_push_settings()
    if not settings["enabled"] or not comments:
        return {"enabled": settings["enabled"], "pushed": 0, "skipped": 0,
                "errors": [], "queued": 0}

    payloads = build_payloads(
        comments, platform=platform, item_id=item_id,
        intent_keywords=intent_keywords, intent_label=intent_label,
        source_keyword=source_keyword)
    skipped = len(comments) - len(payloads)
    if not settings["url"] or not settings["api_key"]:
        # 客户开了推送却还没填 Key，这不是「不用推」，是「还没推成」。
        # 采集到的线索就这一份，丢了就没了，所以照样入暂存箱，
        # 等客户填好 Key 之后自动补推。
        queued = enqueue_payloads(
            payloads, "gdb15 URL 或 API Key 未配置")
        return {"enabled": True, "pushed": 0, "skipped": skipped,
                "errors": ["gdb15 URL 或 API Key 未配置"], "queued": queued}
    failed, errors = await send_payloads(payloads)
    queued = enqueue_payloads(failed, "推送失败", reasons=errors)
    # 按对象身份挑出推成功的，一次采集几百条时别拿字典相等性去比。
    failed_ids = {id(p) for p in failed}
    delivered = [p for p in payloads if id(p) not in failed_ids]
    if delivered:
        try:
            mark_delivered(delivered)
        except Exception as exc:
            # 线索已经躺在 gdb15 里了，结掉本地暂存箱只是收尾。
            # 收尾失败不能反过来把已经成功的推送报成失败，
            # 否则客户以为没送到，又去手工补推一遍。
            log.warning("gdb15 送达回执更新失败（线索已送达，不影响结果）: %r", exc)
    return {"enabled": True, "pushed": len(payloads) - len(failed),
            "skipped": skipped, "errors": errors[:5], "queued": queued}
