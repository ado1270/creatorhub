"""Account-bound signing of the web detail request, without a browser runtime."""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qsl, quote


_WEB_REQUEST_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
_SIGNATURE = "x-secsdk-web-signature"
UIFID_COOKIES = ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP")

# Paths in Douyin's first-party webSign protectedHost registry. These requests
# need both a_bogus and the UIFID-bound x-secsdk-web-signature.
DOUYIN_WEB_SIGNED_PATHS = frozenset({
    "/aweme/v1/web/aweme/detail/",
    "/aweme/v1/web/aweme/post/",
    "/aweme/v1/web/aweme/favorite/",
    "/aweme/v1/web/aweme/listcollection/",
    "/aweme/v1/web/mix/aweme/",
    "/aweme/v1/web/tab/feed/",
    "/aweme/v1/web/mix/list/",
    "/aweme/v1/web/music/aweme/",
    "/aweme/v1/web/music/list/",
    "/aweme/v1/web/mix/detail/",
    "/aweme/v1/web/mix/listcollection/",
    "/aweme/v1/web/music/detail/",
    "/aweme/v1/web/collects/list/",
    "/aweme/v1/web/collects/video/list/",
})


def requires_web_signature(path: str) -> bool:
    normalized = "/" + str(path or "").lstrip("/")
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized in DOUYIN_WEB_SIGNED_PATHS


def valid_device_id(value: str) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 4096 and not re.search(r"[\s;\x00-\x1f\x7f]", value)


def sign_web_query(query: str, device_id: str, *, timestamp: int | None = None) -> str:
    """Replace volatile fields and seal precisely the query bytes sent on wire.

    The caller supplies the selected account's scoped device identifier. This
    function never mints an identity or reuses a signature from a prior request.
    """
    if not valid_device_id(device_id):
        raise ValueError("invalid_device_id")
    stamp = str(int(time.time() if timestamp is None else timestamp))
    fields = []
    has_device = False
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name in {_SIGNATURE, "timestamp"}:
            continue
        if name == "uifid":
            if has_device:
                continue
            value, has_device = device_id, True
        fields.append((name, value))
    if not has_device:
        fields.append(("uifid", device_id))
    fields.append(("timestamp", stamp))
    covered = "&".join(name + "=" + quote(value, safe="!~*'()-._") for name, value in fields)
    message = "_".join((device_id, stamp, _WEB_REQUEST_SALT, covered))
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    return f"{covered}&{_SIGNATURE}={digest}"


def web_query_headers(query: str) -> dict[str, str]:
    values = dict(parse_qsl(query, keep_blank_values=True))
    device_id = values.get("uifid", "")
    signature, stamp = values.get(_SIGNATURE, ""), values.get("timestamp", "")
    if not (valid_device_id(device_id) and re.fullmatch(r"[0-9a-f]{32}", signature)
            and re.fullmatch(r"[0-9]{1,12}", stamp)):
        return {}
    return {"uifid": device_id, _SIGNATURE: signature, "x-secsdk-web-expire": stamp}
