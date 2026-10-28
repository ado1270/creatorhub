"""Target-specific work extraction and failure classification."""
from __future__ import annotations

import re


def normalize_detail_error(error: str) -> str:
    if error == "http_401":
        return "logged_out"
    if error == "http_403":
        return "risk:request_denied:http_403"
    if error == "http_429":
        return "risk:rate_limited:http_429"
    if error in {"http_404", "http_410"}:
        return "aweme_unavailable"
    return error


def detail_http_error(status: int, body: bytes) -> tuple[str, str]:
    """Recognize explicit gateway reasons without exposing response contents.

    Signature configuration failures are not evidence of an expired account or
    a captcha. Unknown denials still stop; never infer a reason from body size.
    """
    if status == 403 and isinstance(body, bytes) and re.search(
            r"<title[^>]*>[^<]*(?:验证码中间页|安全验证|人机验证)",
            body[:4096].decode("utf-8", errors="replace"), re.I):
        return "risk:verification_required:html", "verification_required"
    if status == 403 and isinstance(body, bytes) and len(body) <= 512:
        text = body.decode("utf-8", errors="replace").strip().lower()
        prefix = "blocked by argussecurityplugin "
        if text.startswith(prefix):
            reason = {"uifid not found": "uifid_not_found", "signature not found": "signature_not_found",
                      "sign invalid": "sign_invalid", "sign expired": "sign_expired"}.get(text[len(prefix):])
            if reason:
                return f"api_signature:{reason}", reason
    return normalize_detail_error(f"http_{status}"), ""


def detail_response_error(payload, http_status: int = 200) -> str:
    if http_status != 200:
        return normalize_detail_error(f"http_{http_status}")
    if not isinstance(payload, dict):
        return "invalid_json"
    message = str(payload.get("status_msg") or payload.get("message") or "").strip()
    lowered = message.lower()
    if any(word in lowered for word in (
            "未登录", "请先登录", "登录态已失效", "登录已失效",
            "not login", "not logged in", "login expired", "session expired")):
        return "logged_out"
    if any(word in lowered for word in (
            "验证码", "安全验证", "访问频繁", "captcha", "verify", "verification")):
        return "risk:verification_required"
    if any(word in lowered for word in (
            "作品不存在", "视频不存在", "作品已删除", "视频已删除", "私密作品", "无权访问")):
        return "aweme_unavailable"
    code = payload.get("status_code")
    # Keep the existing risk classifier's status_code=8 contract consistent
    # for direct responses and passive browser interception.
    if str(code) == "8":
        return "risk:api_rejected:status_code=8"
    if code is not None and str(code) != "0":
        return f"api_rejected:status_code={code}"
    return ""


def extract_aweme_detail(payload, aweme_id: str) -> dict | None:
    """Accept only this work, never a recommendation loaded alongside it."""
    pending = [(payload, 0)]
    visited = set()
    while pending and len(visited) < 5000:
        value, depth = pending.pop()
        if depth > 12 or not isinstance(value, (dict, list)) or id(value) in visited:
            continue
        visited.add(id(value))
        if isinstance(value, dict):
            if (str(value.get("aweme_id") or "") == str(aweme_id)
                    and (value.get("video") or value.get("images"))):
                return value
            pending.extend((item, depth + 1) for item in value.values())
        else:
            pending.extend((item, depth + 1) for item in value[:200])
    return None


def parse_detail_response(payload, aweme_id: str, http_status: int = 200) -> tuple[dict | None, str]:
    error = detail_response_error(payload, http_status)
    if error:
        return None, error
    item = extract_aweme_detail(payload, aweme_id)
    return item, "" if item else "missing_aweme_detail"
