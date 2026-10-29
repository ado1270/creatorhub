"""服务端权威套餐（License）。

客户本地装了 CreatorHub，config.yaml 就在他自己电脑上，随手就能改。
所以套餐限制只写在本地等于没写——真正的额度必须由 gdb15 下发。

流程：
1. 启动时和每 ``REFRESH_SECONDS`` 秒向 gdb15 汇报心跳，拿到权威套餐。
2. 拿到的套餐直接覆盖本地 ``cfg.plan``，之后所有监控位/账号/额度校验
   都按服务端的算。
3. 服务端暂时连不上时，用上一次成功缓存的套餐继续工作（网络抖动不该让
   客户停工），缓存超过 ``CACHE_GRACE_DAYS`` 天才降级。
4. 全程记录校验状态，页面上直接显示，不让客户猜。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import socket
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .settings import get_setting, set_setting

REFRESH_SECONDS = 6 * 3600
CACHE_GRACE_DAYS = 30
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# 服务端套餐字段 -> 本地 PlanConfig 字段
_PLAN_FIELDS = {
    "name": "name",
    "active_monitor_slots": "active_monitor_slots",
    "max_accounts": "max_accounts",
    "min_monitor_interval_seconds": "min_monitor_interval_seconds",
    "monthly_lead_records": "monthly_lead_records",
    "max_collection_tasks": "max_collection_tasks",
    "max_keywords_per_task": "max_keywords_per_task",
}


def license_settings() -> dict:
    """License 开关与连接信息。

    API Key 默认复用线索推送那把（同一个租户同一把 Key），服务端认它。
    """
    return {
        "enabled": get_setting("license_enabled", "0") == "1",
        "url": (get_setting("license_url", "") or "").strip(),
        "api_key": (get_setting("license_api_key", "") or "").strip(),
    }


def license_enforced() -> bool:
    """是否处于服务端授权约束下。

    一旦成功授权过一次，缓存就留在本机。此后即使客户把「启用服务端套餐
    校验」的开关关掉，也必须继续按服务端下发的套餐限流——否则「关掉开关」
    就成了一条绕过套餐的后门。
    """
    if license_settings()["enabled"]:
        return True
    return cached_plan() is not None


def _derive_url(explicit: str) -> str:
    """没单独配 License 地址时，从推送地址推算出同一个 gdb15 站点。"""
    if explicit:
        return explicit.rstrip("/")
    push_url = get_setting(
        "lead_push_url", "https://www.gdb15.com/api/v1/leads/webhook")
    parsed = urlsplit(push_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/api/v1/license"
    return ""


def _api_key(settings: dict) -> str:
    if settings["api_key"]:
        return settings["api_key"]
    return (get_setting("lead_push_api_key", "") or "").strip()


def machine_code() -> str:
    """这台机器的稳定指纹。

    用来判断「同一套系统是不是被复制到了另一台机器」。要稳定，但不能
    精确到能被反推，所以只存哈希。
    """
    cached = get_setting("license_machine_code", "")
    if cached:
        return cached
    raw = "|".join([
        platform.node() or "",
        platform.system() or "",
        str(uuid.getnode()),
        socket.gethostname() or "",
    ])
    code = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    set_setting("license_machine_code", code)
    return code


def instance_id() -> str:
    """本次安装的唯一标识，重装会变。"""
    cached = get_setting("license_instance_id", "")
    if cached:
        return cached
    value = uuid.uuid4().hex
    set_setting("license_instance_id", value)
    return value


def _cache_key() -> str:
    return "license_plan_cache"


def cached_plan() -> dict | None:
    """上一次成功拿到的套餐，带抓取时间。"""
    raw = get_setting(_cache_key(), "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def store_plan(plan: dict, *, status: str, expires_at: str = "") -> None:
    set_setting(_cache_key(), json.dumps({
        "plan": plan or {},
        "status": status or "",
        "package_expires_at": expires_at or "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False))


def apply_cached_plan(cfg) -> bool:
    """把缓存的套餐重新套到 cfg 上。

    重启后如果一时连不上 gdb15，必须用上次的套餐继续限流——否则客户
    断网重启就能绕过监控位和额度限制。
    """
    cache = cached_plan()
    if not cache:
        return False
    return _apply_plan(cfg, cache.get("plan") or {})


def _lock_plan(cfg, *, name: str = "授权失效") -> None:
    """授权失效：把所有配额清零，保留读取能力但不能再建监控、收线索。"""
    cfg.plan.name = name
    cfg.plan.active_monitor_slots = 0
    cfg.plan.max_accounts = 0
    cfg.plan.monthly_lead_records = 0
    cfg.plan.max_collection_tasks = 0
    cfg.plan.max_keywords_per_task = 0
    # 检查频率也锁到 24 小时，别让人以为还能正常跑。
    cfg.plan.min_monitor_interval_seconds = 86400


def _apply_plan(cfg, plan: dict) -> bool:
    """把服务端套餐写进 cfg.plan。返回是否真的改了。"""
    if not plan:
        return False
    changed = False
    for remote_key, local_key in _PLAN_FIELDS.items():
        if remote_key not in plan:
            continue
        value = plan[remote_key]
        if local_key == "name":
            new_value = str(value or "")
        else:
            try:
                new_value = int(value)
            except (TypeError, ValueError):
                continue
            if new_value < 0:
                continue
        if getattr(cfg.plan, local_key) != new_value:
            setattr(cfg.plan, local_key, new_value)
            changed = True
    return changed


async def verify(cfg, *, settings: dict | None = None) -> dict:
    """向服务端校验一次，把返回结果落到本地。

    返回本次校验的状态摘要，供接口和日志使用。
    """
    settings = settings or license_settings()
    if not settings["enabled"]:
        # 开关被关了，但本机还留着上次的授权缓存：照样按服务端套餐限流。
        # 「关掉开关就变无限」是最容易被利用的一条后门，不能留。
        used = apply_cached_plan(cfg)
        if used:
            return {"checked": False, "used_cache": True, "enforced": True,
                    "reason": "服务端授权已生效，不能关闭（仍按 gdb15 套餐限流）"}
        return {"checked": False, "reason": "未启用服务端套餐校验"}

    url = _derive_url(settings["url"])
    key = _api_key(settings)
    if not url or not key:
        used = apply_cached_plan(cfg)
        if used:
            return {"checked": False, "used_cache": True,
                    "reason": "连不上 gdb15，暂时按上次缓存的套餐限流"}
        # 开了授权却没法校验，本机又没有缓存：宁可先停工，也不能先白嫖。
        _lock_plan(cfg, name="授权未校验")
        return {"checked": False, "locked": True,
                "reason": "未配置 gdb15 地址或 API Key，已暂停新建监控"}

    payload = {
        "instanceId": instance_id(),
        "machineCode": machine_code(),
        "appVersion": _app_version(),
        **collect_usage(),
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                f"{url}/heartbeat", json=payload,
                headers={"x-api-key": key})
    except Exception as exc:
        used = apply_cached_plan(cfg)
        if used:
            return {"checked": False, "used_cache": True,
                    "reason": f"连不上 gdb15，暂时按上次缓存的套餐限流：{exc!r}"}
        _lock_plan(cfg, name="授权未校验")
        return {"checked": False, "locked": True,
                "reason": f"连不上 gdb15 且本机没有授权缓存，已暂停新建监控：{exc!r}"}

    if response.status_code in (401, 403):
        # 授权被拒：Key 无效或账号被停用。不能用缓存套餐硬撑，直接锁死，
        # 否则「改掉 API Key」就能继续白嫖。
        _lock_plan(cfg)
        # 把「已拒绝」也写进缓存，重启后仍然是锁死状态。
        zero_plan = {key: 0 for key in _PLAN_FIELDS if key != "name"}
        zero_plan["name"] = "授权失效"
        zero_plan["min_monitor_interval_seconds"] = 86400
        store_plan(zero_plan, status="inactive")
        return {"checked": False, "locked": True,
                "reason": f"gdb15 拒绝授权：HTTP {response.status_code}"}
    if response.status_code >= 400:
        used = apply_cached_plan(cfg)
        if used:
            return {"checked": False, "used_cache": True,
                    "reason": f"gdb15 返回异常，暂时按上次缓存的套餐限流：HTTP {response.status_code}"}
        _lock_plan(cfg, name="授权未校验")
        return {"checked": False, "locked": True,
                "reason": f"gdb15 返回异常且没有授权缓存，已暂停新建监控：HTTP {response.status_code}"}

    try:
        data = response.json()
    except ValueError:
        return {"checked": False, "reason": "gdb15 返回的不是合法 JSON"}

    plan = data.get("plan") or {}
    status = str(data.get("status") or "")
    store_plan(plan, status=status,
               expires_at=str(data.get("packageExpiresAt") or ""))
    _apply_plan(cfg, plan)
    instance = data.get("instance") or {}
    return {
        "checked": True,
        "status": status,
        "plan": plan,
        "package_expires_at": data.get("packageExpiresAt") or "",
        "instance_allowed": bool(instance.get("allowed", True)),
        "active_instances": instance.get("activeInstances"),
        "max_instances": instance.get("maxInstances"),
    }


def collect_usage() -> dict:
    """本机真实占用，随心跳一起上报。

    套餐上限由服务端下发，但「这台机器上实际开了几个监控、绑了几个账号」
    只有本地知道。不上报的话，客户说「我被监控位卡住了」，服务端只能猜。
    采集失败就回空字典：心跳的目的是拿套餐，不能因为统计报错把授权流程打断。
    """
    try:
        from .db import get_session
        from .quota import local_usage_snapshot
        with get_session() as session:
            return local_usage_snapshot(session)
    except Exception:
        return {}


def _app_version() -> str:
    """客户端版本号，心跳带上去，后台才知道哪些客户还没升级。

    ``app/__init__.py`` 里并没有 ``__version__``，真正的版本号在安装目录的
    ``desktop-version.txt``（源码运行时是 "source"）。以前只试前者，所以
    后台看到的 appVersion 一直是空的。
    """
    try:
        from . import __version__  # type: ignore
        if __version__:
            return str(__version__)[:32]
    except Exception:
        pass
    try:
        from desktop.launcher import version as _installed_version
        return str(_installed_version() or "")[:32]
    except Exception:
        return ""


def effective_status(cfg) -> dict:
    """当前生效的套餐来源与状态，给设置页显示。"""
    settings = license_settings()
    cache = cached_plan()
    plan = getattr(cfg, "plan", None)
    result = {
        "enabled": settings["enabled"],
        "enforced": False,
        "source": "local",
        "status": "",
        "plan_name": getattr(plan, "name", "") or "",
        "fetched_at": "",
        "package_expires_at": "",
        "stale": False,
        "message": "",
        "limits": {
            "active_monitor_slots": getattr(plan, "active_monitor_slots", None),
            "max_accounts": getattr(plan, "max_accounts", None),
            "min_monitor_interval_seconds": getattr(
                plan, "min_monitor_interval_seconds", None),
            "monthly_lead_records": getattr(plan, "monthly_lead_records", None),
            "max_collection_tasks": getattr(plan, "max_collection_tasks", None),
            "max_keywords_per_task": getattr(plan, "max_keywords_per_task", None),
        },
    }
    if not settings["enabled"] and not cache:
        result["message"] = "未启用服务端套餐校验，当前使用本地 config.yaml 配置"
        return result
    if not cache:
        result["message"] = "尚未从 gdb15 获取过套餐，当前使用本地 config.yaml 配置"
        return result
    # 到这里说明本机有授权缓存。即使开关被关了，套餐仍然以服务端为准。
    result["enforced"] = True
    if not settings["enabled"]:
        result["message"] = "服务端授权已生效，不能关闭；仍按 gdb15 下发的套餐限流"
        result["source"] = "server"
        result["status"] = cache.get("status") or ""
        result["fetched_at"] = cache.get("fetched_at") or ""
        result["package_expires_at"] = cache.get("package_expires_at") or ""
        return result

    fetched = _parse_iso(cache.get("fetched_at"))
    age_days = (datetime.now(timezone.utc) - fetched).days if fetched else 999
    result["source"] = "server"
    result["status"] = cache.get("status") or ""
    result["fetched_at"] = cache.get("fetched_at") or ""
    result["package_expires_at"] = cache.get("package_expires_at") or ""
    result["stale"] = age_days > CACHE_GRACE_DAYS
    if result["stale"]:
        result["message"] = (
            f"已有 {age_days} 天没连上 gdb15，套餐缓存已过期，请检查网络或联系服务商")
    else:
        result["message"] = f"套餐来自 gdb15（{age_days} 天前校验）"
    return result


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def sync_cloud_resources() -> None:
    """顺带把云端授权的代理池拉下来，并回报本地实况。

    授权校验是主业，代理池是附加能力。所以这里失败只记日志，
    绝不能因为代理池连不上就让整个采集停摆。
    """
    try:
        from .resources import sync_and_report
        result = await sync_and_report()
    except Exception as exc:
        print(f"[license] 同步云端资源失败：{exc!r}")
        return
    if not result.get("ok"):
        reason = (result.get("sync") or {}).get("reason") or "未知原因"
        print(f"[license] 同步云端资源未完成：{reason}")
        return
    proxies = (result.get("sync") or {}).get("proxies") or {}
    assignments = (result.get("sync") or {}).get("assignments") or {}
    print(
        f"[license] 云端代理池已同步：新增 {proxies.get('added', 0)} 条，"
        f"更新 {proxies.get('updated', 0)} 条，停用 {proxies.get('revoked', 0)} 条，"
        f"指派生效 {assignments.get('applied', 0)} 个账号"
    )


async def license_loop(cfg) -> None:
    """后台心跳循环。启动时先校验一次，之后定时刷新。"""
    while True:
        try:
            result = await verify(cfg)
            if result.get("checked"):
                print(f"[license] 已从 gdb15 获取套餐：{result.get('status')}")
                await sync_cloud_resources()
            elif result.get("reason"):
                print(f"[license] 校验未完成：{result['reason']}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[license] 校验异常：{exc!r}")
        await asyncio.sleep(REFRESH_SECONDS)
