"""手机采集入口（CreatorHub 侧）。

客户在手机上刷到一条想监控的作品，复制分享文案，贴进 gdb15 的手机页面提交。
手机不装 CreatorHub、不放账号登录态 —— 它只是个输入框。

云端把这条作品收进一个信箱，本机定期去取，取到就在本地按「加评论监控」
的正常流程建监控，再把结果回报给云端，手机上就能看到「已加入评论监控」。

所以手机端的体感是「扫码 → 粘贴 → 提交」，真正的活儿还是在本机跑：
账号、Cookie、指纹、出口全在客户自己的电脑上，一号一机的隔离结论不变。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .settings import get_setting, set_setting

# 取件间隔。手机提交到电脑建好监控之间，延迟几分钟完全可以接受；
# 但也别太慢 —— 客户提交完会盯着手机等回执。
POLL_SECONDS = 90
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


def _site_base() -> str:
    """gdb15 站点根地址（scheme://host）。

    手机入口和授权心跳、线索推送是同一家站点，地址取一样的。

    只取 scheme 和 host：授权地址填的是 `https://站点/api/v1/license`，
    线索推送地址填的是 `https://站点/api/v1/leads/webhook`，
    带着路径去拼接口地址就会拼出 `.../api/v1/license/api/v1/watch-intake`。
    """
    from urllib.parse import urlsplit

    for candidate in (
        (get_setting("license_url", "") or "").strip(),
        (get_setting("lead_push_url", "") or "").strip(),
    ):
        if not candidate:
            continue
        text = candidate.strip()
        if not text.startswith(("http://", "https://")):
            text = f"https://{text}"
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _api_key() -> str:
    """和授权心跳、线索推送共用一把 Key —— 客户只需要填一次。"""
    key = (get_setting("license_api_key", "") or "").strip()
    if key:
        return key
    return (get_setting("lead_push_api_key", "") or "").strip()


def intake_base_url() -> str:
    base = _site_base()
    return f"{base}/api/v1/watch-intake" if base else ""


def intake_ready() -> bool:
    """有没有配好地址和 Key。没配好就完全不碰这条链路。"""
    return bool(intake_base_url()) and bool(_api_key())


def _headers() -> dict:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


async def create_mobile_session() -> dict:
    """向云端要一个一次性扫码链接。

    链接里带的是一次性令牌，不是客户的 API Key —— 别人截到二维码，
    最多能往评论监控里塞作品，碰不到客户的线索和账号。
    """
    base = intake_base_url()
    key = _api_key()
    if not base or not key:
        raise RuntimeError("还没配置 gdb15 地址或 API Key，无法生成手机入口")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(f"{base}/session", headers=_headers())
    if response.status_code >= 400:
        raise RuntimeError(f"gdb15 返回异常：HTTP {response.status_code}")
    return response.json()


async def fetch_recent(limit: int = 20) -> list[dict]:
    """最近的手机提交记录，给桌面端列表用。拉不到就当没有，别打断客户。"""
    base = intake_base_url()
    key = _api_key()
    if not base or not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(f"{base}", headers=_headers())
        if response.status_code >= 400:
            return []
        data = response.json()
    except Exception:
        return []
    return list(data.get("items") or [])[:max(1, limit)]


async def _claim(take: int = 5) -> list[dict]:
    base = intake_base_url()
    key = _api_key()
    if not base or not key:
        return []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(f"{base}/claim", headers=_headers(),
                                     params={"take": str(take)})
    if response.status_code >= 400:
        raise RuntimeError(f"gdb15 返回异常：HTTP {response.status_code}")
    return list((response.json() or {}).get("items") or [])


async def _ack(item_id: str, payload: dict) -> None:
    base = intake_base_url()
    key = _api_key()
    if not base or not key:
        return
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            await client.post(f"{base}/{item_id}/ack", headers=_headers(),
                              json=payload)
    except Exception as exc:
        print(f"[intake] 回报结果失败：{exc!r}")


def _plan_interval() -> int:
    """手机提交的作品用多快的频率扫。

    取套餐允许的最快间隔 —— 客户买的正是「更及时」，
    但绝不能比套餐下限更快，否则等于绕开套餐限流。
    """
    from . import main
    minimum = int(getattr(main.cfg.plan, "min_monitor_interval_seconds", 0) or 0)
    return max(600, minimum or 1800)


def _active_accounts(platform: str) -> list:
    """该平台上还能用的账号，按 id 排好，保证分片顺序稳定。"""
    from sqlmodel import select

    from .db import get_session
    from .models import DouyinAccount
    with get_session() as session:
        rows = session.exec(
            select(DouyinAccount)
            .where(DouyinAccount.platform == platform)
            .where(DouyinAccount.status == "active")
        ).all()
    return sorted(rows, key=lambda r: r.id or 0)


def _pick_account_id(platform: str) -> int | None:
    """给这条作品挑一个执行账号。

    挑账号不是为了「多账号并行」，是为了**分片**：

    - 小红书笔记页必须登录才能读评论，没有账号这条监控必然失败；
    - 抖音绑账号后走的是这个号的 Cookie/API 通道，而不是匿名浏览器抓取，
      暴露面更小、请求更省；
    - 客户有 2 个号时，作品在两个号之间轮流分，每个号只跑一半，
      单号崩了不会把另一半一起带走，平台也看不到「一堆号在同一时刻
      扫同一批作品」这种同步行为。

    轮转指针存在本机 settings 里：换了机器就重新开始，不影响正确性。
    """
    accounts = _active_accounts(platform)
    if not accounts:
        return None
    logged_in = [a for a in accounts if a.storage_state]
    pool = logged_in or accounts
    if platform == "xhs" and not logged_in:
        # 小红书没有登录态就等于读不到评论，早点失败比建个空监控强。
        return None
    key = f"intake_account_cursor_{platform}"
    try:
        cursor = int(get_setting(key, "0") or 0)
    except (TypeError, ValueError):
        cursor = 0
    chosen = pool[cursor % len(pool)]
    set_setting(key, str(cursor + 1))
    return chosen.id


async def _create_watch(item: dict) -> tuple[bool, str, dict]:
    """把一条手机提交的作品建成评论监控。

    走的是和界面上「添加评论监控」完全一样的入口，套餐监控位、
    重复检查、频率下限都在同一条校验链上 —— 手机入口不是后门。
    """
    from fastapi import HTTPException

    from . import main
    platform = str(item.get("platform") or "douyin")
    if platform not in ("douyin", "xhs", "kuaishou"):
        platform = "douyin"
    raw_text = str(item.get("rawText") or "").strip()
    if not raw_text:
        return False, "提交内容为空", {}

    account_id = _pick_account_id(platform)
    if platform == "xhs" and not account_id:
        return False, "小红书评论监控需要先在电脑上登录一个小红书账号", {}

    body = main.WatchIn(
        url_or_id=raw_text,
        platform=platform,
        kind="auto",
        mode="public",
        account_id=account_id,
        enabled=None,
        interval_seconds=_plan_interval(),
        recent_works=0,
        recent_days=0,
        max_scrolls=0,
        comment_max_age_minutes=300,
        group_name="手机采集",
    )
    try:
        watch = await main.add_watch(body)
    except HTTPException as exc:
        return False, str(exc.detail), {}
    except Exception as exc:
        return False, f"添加失败：{exc!r}", {}
    return True, "", dict(watch or {})


async def process_pending() -> dict:
    """取一次件并处理掉。返回本次的处理统计。"""
    stats = {"claimed": 0, "done": 0, "failed": 0}
    items = await _claim(5)
    stats["claimed"] = len(items)
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        ok, error, watch = await _create_watch(item)
        if ok:
            stats["done"] += 1
            await _ack(item_id, {
                "status": "done",
                "watchId": int(watch.get("id") or 0),
                "watchTitle": str(watch.get("title") or "")[:200],
            })
            print(f"[intake] 手机提交的作品已加入评论监控：{watch.get('title') or item_id}")
        else:
            stats["failed"] += 1
            await _ack(item_id, {"status": "failed", "error": error[:500]})
            print(f"[intake] 手机提交的作品添加失败：{error}")
    return stats


async def intake_loop() -> None:
    """后台取件循环。

    没有配置 gdb15 地址或 Key 时完全不启动 —— 这条链路是可选增强，
    不能因为它没配就影响正常采集。
    """
    if not intake_ready():
        print("[intake] 未配置 gdb15 地址或 API Key，手机采集入口未启用")
        return
    while True:
        try:
            stats = await process_pending()
            if stats.get("claimed"):
                print(f"[intake] 处理手机提交 {stats['claimed']} 条："
                      f"成功 {stats['done']}，失败 {stats['failed']}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[intake] 取件异常：{exc!r}")
        await asyncio.sleep(POLL_SECONDS)
