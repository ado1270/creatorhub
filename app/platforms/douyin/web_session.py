"""Account-scoped browser/API session compatibility; no network operations."""
from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import parse_qsl, urlsplit


DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
DETAIL_ORIGINS = {"https://www.douyin.com", "https://www-hj.douyin.com"}
CONTEXT_TTL = 6 * 60 * 60
_PARAMS = set("""device_platform aid channel pc_client_type version_code version_name
update_version_code support_h265 support_dash cpu_core_num device_memory
cookie_enabled screen_width screen_height browser_language browser_platform
browser_name browser_version browser_online engine_name engine_version os_name
os_version platform downlink effective_type round_trip_time pc_libra_divert
webid uifid verifyFp fp origin_type request_source""".split())
_HEADERS = {"sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-language"}
_AUTH_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}


def read_state(value) -> dict:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def cookies_for_url(cookies, url: str, *, now: float | None = None) -> list[dict]:
    """Apply browser domain/path/expiry rules, including host-only cookies."""
    parsed = urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path or "/"
    sampled = time.time() if now is None else now
    result = []
    for cookie in cookies if isinstance(cookies, list) else []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if not domain or not (host == domain.lstrip(".") or (
                domain.startswith(".") and host.endswith(domain))):
            continue
        cp = str(cookie.get("path") or "/")
        if not (path == cp or path.startswith(cp.rstrip("/") + "/")):
            continue
        if cookie.get("secure") and parsed.scheme != "https":
            continue
        try:
            expires = float(cookie.get("expires", -1))
            if expires >= 0 and expires <= sampled:
                continue
        except (TypeError, ValueError):
            continue
        name, value = str(cookie["name"]), str(cookie.get("value", ""))
        if any(ch in name + value for ch in ("\r", "\n", ";")):
            continue
        result.append(cookie)
    return sorted(result, key=lambda c: len(str(c.get("path") or "/")), reverse=True)


def cookie_header(cookies, url: str) -> str:
    return "; ".join(f"{c['name']}={c.get('value', '')}" for c in cookies_for_url(cookies, url))


def cookie_value(cookies, url: str, name: str) -> str:
    return next((str(c.get("value") or "") for c in cookies_for_url(cookies, url)
                 if c["name"] == name), "")


def context_key(account_id: int) -> str:
    return f"douyin_detail_context:{account_id}"


def context_binding(account, state, ua: str) -> str:
    cookies = read_state(state).get("cookies", [])
    auth = sorted((c["name"], c.get("domain", ""), c.get("path", "/"), c.get("value", ""))
                  for c in cookies_for_url(cookies, "https://www.douyin.com/")
                  if c["name"] in _AUTH_COOKIES)
    if not auth:
        return ""
    environment = [getattr(account, field, "") for field in (
        "id", "profile_dir", "browser_backend", "browser_runtime_id", "proxy", "fp_seed")]
    return hashlib.sha256(json.dumps([environment, ua, auth], ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def observed_detail_context(url: str, headers: dict, *, ua: str, fingerprint: str = "") -> dict:
    """Save a recipe, never a signed URL, msToken, cookie or response body."""
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in DETAIL_ORIGINS or parsed.path != DETAIL_PATH:
        return {}
    params = {k: v for k, v in parse_qsl(parsed.query) if k in _PARAMS and len(v) <= 1024}
    selected_headers = {str(k).lower(): str(v) for k, v in headers.items()
                        if str(k).lower() in _HEADERS and len(str(v)) <= 1024
                        and "\r" not in str(v) and "\n" not in str(v)}
    return {"version": 1, "origin": origin, "params": params, "headers": selected_headers,
            "ua": ua, "fingerprint": fingerprint[:1024], "captured_at": time.time()}


def validated_detail_context(value, account, state, ua: str) -> dict:
    data = read_state(value)
    try:
        age = time.time() - float(data.get("captured_at", 0))
    except (TypeError, ValueError):
        return {}
    binding = context_binding(account, state, ua)
    if (not binding or data.get("binding") != binding or data.get("ua") != ua
            or data.get("origin") not in DETAIL_ORIGINS or data.get("version") != 1
            or age < -60 or age > CONTEXT_TTL):
        return {}
    # Revalidate persisted fields: local state is not permission to send secrets
    # to arbitrary origins or to replay a captured one-use signature.
    from urllib.parse import urlencode
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    result = observed_detail_context(data["origin"] + DETAIL_PATH + "?" + urlencode(params),
                                    headers, ua=ua, fingerprint=str(data.get("fingerprint") or ""))
    result.update(captured_at=data["captured_at"], binding=binding)
    return result
