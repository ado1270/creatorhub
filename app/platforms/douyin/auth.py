"""Interpret only the current-user endpoint as authentication evidence."""
from __future__ import annotations


def parse_self_profile_response(payload, *, http_status: int = 200) -> tuple[dict | None, str]:
    """Return a verified self profile, explicit auth loss, or an inconclusive error.

    Public profile/other responses and cached localStorage identities must never
    be passed here: both remain available after the session has expired.
    """
    if http_status == 401:
        return None, "logged_out"
    if http_status != 200:
        return None, f"http_{http_status}"
    if not isinstance(payload, dict):
        return None, "invalid_response"
    code = payload.get("status_code")
    message = str(payload.get("status_msg") or payload.get("message") or "").strip()
    if any(marker in message.lower() for marker in (
            "未登录", "请先登录", "登录态已失效", "登录已失效",
            "not login", "not logged in", "login required", "login expired", "session expired")):
        return None, "logged_out"
    if str(code) != "0":
        return None, f"status_{code}:{message}" if code is not None else "invalid_response"
    nested = payload.get("data")
    nested = nested if isinstance(nested, dict) else {}
    user = (payload.get("user") or payload.get("user_info")
            or nested.get("user") or nested.get("user_info"))
    if payload.get("guest") is True or nested.get("guest") is True:
        return None, "logged_out"
    if not isinstance(user, dict):
        return None, "missing_user"
    if user.get("guest") is True:
        return None, "logged_out"
    if not any(str(user.get(key) or "").strip() not in {"", "0"}
               for key in ("sec_uid", "uid")):
        return None, "logged_out" if str(user.get("uid")) == "0" else "missing_user"
    return user, ""
