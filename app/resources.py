"""云端授权的代理池 / 账号池（CreatorHub 侧）。

云端（gdb15）记着每个租户名下有哪些代理、哪些账号，以及「哪个账号该用哪条代理」。
本机在心跳时把这些授权拉下来，落进本地代理池，然后把本地的实际绑定情况报回去。

两条硬规矩：

1. **云端撤销的代理，本地「停用」而不是删除。** 删了客户就看不出自己少过什么，
   账号上残留的绑定也失去了上下文；重新授权时还得从零重建一遍。停用则保留现场。

2. **云端指派的代理只作用于「本地还没绑代理」的账号。** 出口 IP 是设备指纹的
   一部分，中途换 IP 等于告诉平台「这个号突然换人了」，风控直接盯上。已绑定的
   保留本地配置，把不一致报回云端，由人决定怎么处理 —— 工具不替客户做这个决定。

账号登录态（cookie / storage_state）永远不上云。这里只报身份和绑定关系。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlmodel import select

from .settings import get_setting

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


def _site_base() -> str:
    """gdb15 站点根地址（scheme://host）。

    优先用授权地址；没单独配就沿用线索推送的地址 —— 两个功能本来就是一家。
    """
    for candidate in (
        (get_setting("license_url", "") or "").strip(),
        (get_setting("lead_push_url", "") or "").strip(),
    ):
        if not candidate:
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def resource_base_url() -> str:
    """资源接口基地址。"""
    base = _site_base()
    return f"{base}/api/v1/resources" if base else ""


def resource_api_key() -> str:
    """和推送线索、授权心跳用同一把 Key —— 客户只需要填一次。"""
    key = (get_setting("license_api_key", "") or "").strip()
    if key:
        return key
    return (get_setting("lead_push_api_key", "") or "").strip()


def account_resource_key(account) -> str:
    """账号在云端台账里的稳定标识。

    用抖音号 / 小红书号（人认得出来），取不到就退回平台 sec_uid（唯一但不好认）。
    不用本地 id：换台机器 id 就变了，对账毫无意义。
    """
    for field in ("douyin_id", "sec_uid", "uid"):
        value = str(getattr(account, field, "") or "").strip()
        if value:
            return value
    return ""


def _proxy_host_port(url: str) -> str:
    """代理的 host:port，用于和云端指派比对。不含账号密码。"""
    if not url:
        return ""
    from .browser.proxy import try_proxy_plan
    plan = try_proxy_plan(url)
    if not plan:
        return ""
    return f"{plan.host}:{plan.port}"


def _normalize(url: str) -> str:
    """把云端给的地址规范成本地认得的格式（补协议头、密码转义）。"""
    from .browser.manager import normalize_proxy
    return normalize_proxy((url or "").strip())


def collect_local_resources() -> dict:
    """本地实况：账号清单 + 云端代理的连通性状态。

    只报身份和绑定关系，账号登录态永远不上云。
    """
    from .db import get_session
    from .models import DouyinAccount, ProxyPool

    accounts: list[dict] = []
    proxies: list[dict] = []
    with get_session() as session:
        for account in session.exec(select(DouyinAccount)).all():
            key = account_resource_key(account)
            if not key:
                continue
            accounts.append({
                "platform": account.platform or "douyin",
                "accountKey": key,
                "nickname": account.nickname or "",
                "status": account.status or "active",
                "boundProxyHost": _proxy_host_port(account.proxy or ""),
            })
        for pool in session.exec(select(ProxyPool)).all():
            if pool.origin != "cloud" or not pool.remote_id:
                continue
            proxies.append({
                "remoteId": pool.remote_id,
                "status": pool.status or "unknown",
                "exitIp": pool.exit_ip or "",
                "country": pool.country or "",
                "region": pool.region or "",
                "city": pool.city or "",
                "isp": pool.isp or "",
            })
    return {"accounts": accounts, "proxies": proxies}


def apply_bundle(bundle: dict) -> dict:
    """把云端下发的授权落到本地。

    返回一份摘要，给设置页显示「同步了什么」。
    """
    from .db import get_session
    from .models import DouyinAccount, ProxyPool

    cloud_proxies = bundle.get("proxies") or []
    assignments = bundle.get("assignments") or []
    cloud_ids: set[str] = set()

    added = updated = adopted = revoked = 0
    applied = 0
    skipped = 0

    with get_session() as session:
        all_rows = session.exec(select(ProxyPool)).all()
        by_url = {row.url: row for row in all_rows}
        by_remote = {
            row.remote_id: row for row in all_rows
            if row.origin == "cloud" and row.remote_id
        }

        for item in cloud_proxies:
            remote_id = str(item.get("id") or "").strip()
            if not remote_id:
                continue
            cloud_ids.add(remote_id)
            url = _normalize(str(item.get("url") or ""))
            if not url:
                continue
            label = str(item.get("label") or "").strip()

            existing = by_remote.get(remote_id)
            if existing is not None:
                # 云端是权威：地址、备注、状态都跟着云端走。
                existing.url = url
                if label:
                    existing.label = label
                existing.status = item.get("status") or existing.status
                existing.enabled = True
                session.add(existing)
                updated += 1
                continue

            same = by_url.get(url)
            if same is not None:
                # 本地已经有这条代理 —— 不覆盖它，只登记成「云端也授权了这条」。
                # 客户本地配的备注、停用状态都保留。
                same.origin = "cloud"
                same.remote_id = remote_id
                session.add(same)
                adopted += 1
                continue

            session.add(ProxyPool(
                url=url,
                label=label,
                status=item.get("status") or "unknown",
                enabled=True,
                origin="cloud",
                remote_id=remote_id,
                country=str(item.get("country") or ""),
                region=str(item.get("region") or ""),
                city=str(item.get("city") or ""),
                isp=str(item.get("isp") or ""),
            ))
            added += 1

        # 云端已经撤销的代理：本地停用，不删。
        for remote_id, row in by_remote.items():
            if remote_id in cloud_ids:
                continue
            if row.enabled:
                row.enabled = False
                session.add(row)
                revoked += 1

        # 账号 → 代理 指派。只对「本地还没绑代理」的账号生效。
        accounts = session.exec(select(DouyinAccount)).all()
        by_key: dict[tuple[str, str], object] = {}
        for account in accounts:
            key = account_resource_key(account)
            if key:
                by_key[(account.platform or "douyin", key)] = account

        for item in assignments:
            platform = str(item.get("platform") or "douyin").strip().lower()
            account_key = str(item.get("accountKey") or "").strip()
            proxy_url = _normalize(str(item.get("proxyUrl") or ""))
            if not account_key or not proxy_url:
                continue
            account = by_key.get((platform, account_key))
            if account is None:
                # 云端台账里有这台机器上还没有的账号（换机器了？）。跳过，
                # 等本地有这个账号再说 —— 不能凭空造一个没有登录态的账号。
                skipped += 1
                continue
            if str(getattr(account, "proxy", "") or "").strip():
                # 本地已经绑了代理。不动它：出口 IP 是指纹的一部分，
                # 中途换 IP 等于告诉平台「这个号突然换人了」。
                skipped += 1
                continue
            account.proxy = proxy_url
            session.add(account)
            applied += 1

        session.commit()

    return {
        "ok": True,
        "proxies": {"added": added, "updated": updated, "adopted": adopted, "revoked": revoked},
        "assignments": {"applied": applied, "skipped": skipped},
    }


async def sync_resources() -> dict:
    """从云端拉一次授权资源并落到本地。

    拿不到就原样返回，什么都不改 —— 本地已有的配置不能被一次网络抖动搞坏。
    """
    base = resource_base_url()
    key = resource_api_key()
    if not base or not key:
        return {"ok": False, "reason": "未配置 gdb15 地址或 API Key"}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(f"{base}/bundle", headers={"x-api-key": key})
    except Exception as exc:
        return {"ok": False, "reason": f"拉取授权资源失败：{exc!r}"}

    if response.status_code in (401, 403):
        return {"ok": False, "reason": f"gdb15 拒绝授权：HTTP {response.status_code}"}
    if response.status_code >= 400:
        return {"ok": False, "reason": f"gdb15 返回异常：HTTP {response.status_code}"}
    try:
        bundle = response.json()
    except ValueError:
        return {"ok": False, "reason": "gdb15 返回的不是合法 JSON"}
    if not isinstance(bundle, dict):
        return {"ok": False, "reason": "gdb15 返回的授权内容格式不对"}

    try:
        return apply_bundle(bundle)
    except Exception as exc:
        return {"ok": False, "reason": f"落地授权资源失败：{exc!r}"}


async def report_resources() -> dict:
    """把本地实况报回云端。"""
    base = resource_base_url()
    key = resource_api_key()
    if not base or not key:
        return {"ok": False, "reason": "未配置 gdb15 地址或 API Key"}
    try:
        payload = collect_local_resources()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                f"{base}/report", json=payload, headers={"x-api-key": key})
    except Exception as exc:
        return {"ok": False, "reason": f"上报资源状态失败：{exc!r}"}

    if response.status_code in (401, 403):
        return {"ok": False, "reason": f"gdb15 拒绝授权：HTTP {response.status_code}"}
    if response.status_code >= 400:
        return {"ok": False, "reason": f"gdb15 返回异常：HTTP {response.status_code}"}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "reason": "gdb15 返回的不是合法 JSON"}
    return {"ok": True, **(data if isinstance(data, dict) else {})}


async def sync_and_report() -> dict:
    """先拉授权、再报实况。

    顺序是有讲究的：先把云端的指派落到本地，本地状态才是最新的，
    这时候报回去的绑定关系才准。
    """
    synced = await sync_resources()
    reported = await report_resources()
    return {"ok": bool(synced.get("ok")), "sync": synced, "report": reported}
