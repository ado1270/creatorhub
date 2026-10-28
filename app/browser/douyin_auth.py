"""Shared visible-login evidence for Douyin login, health and manual windows."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_LOGIN_PROMPT = re.compile(
    r"^\s*(?:登\s*录|扫码登录|验证码登录|密码登录|登录后免费畅享高清视频)\s*$")


async def douyin_login_prompt_visible(page) -> bool | None:
    """Inspect every matching element; hidden duplicates must not hide a dialog.

    None means navigation/inspection was inconclusive, not authenticated.
    False only means there is no visible login prompt, not proof of a session.
    """
    try:
        parsed = urlsplit(str(page.url or ""))
        host = (parsed.hostname or "").lower()
        if host != "douyin.com" and not host.endswith(".douyin.com"):
            return None
        path = parsed.path.lower()
        if "passport" in host or any(
                part in {"passport", "login", "login.html"} for part in path.split("/")):
            return True
        for locator in await page.get_by_text(_LOGIN_PROMPT).all():
            if await locator.is_visible():
                return True
        return False
    except Exception:
        return None
