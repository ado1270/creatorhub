"""gdb15 那几把 Key 的形状识别。

gdb15 后台有三样都被叫做「Key」/「码」的东西，长相完全不同，但客户
手里拿到的都是一串看不懂的字符，很容易填错框：

  - 卡密（``JUFW-HDW6-TCK4-CDD4``）：4 组 16 位，在「套餐与用量」页
    兑换套餐和额度，**任何接口都不认它**。
  - 采集推送 Key（``lk_...``）：在「套餐与用量」页「对接用 API Key」，
    CreatorHub 填在「服务端授权」和「线索推送」两处，走 ``x-api-key``。
  - 开放 API Key（``gdb15_...``）：在「开放 API」页单独签发，走
    ``x-open-api-key``。

填错之后服务端一律回 401「API Key 无效」——服务端不知道客户本来想填
哪一把，只能说「这把不认」。客户看到这句话只会以为 Key 被吊销了，
跑去后台翻找不存在的那把，或者干脆重新签一把，绕一大圈还是不通。

所以这一层在请求发出之前先认一遍形状，把「你填的是哪一样、对的该去哪拿」
一次讲清楚。识别只用于给出更好的提示，不替代服务端鉴权：认不出来的
形状一律放行，让服务端说了算。
"""
from __future__ import annotations

import re

# 与 gdb15 服务端保持一致：开放 API Key 明文前缀见 api-keys.service.ts 的
# API_KEY_PREFIX，推送 Key 生成处见 license.controller.ts。
OPEN_KEY_PREFIX = "gdb15_"
PUSH_KEY_PREFIX = "lk_"

# 卡密：4 组，每组 4 位。服务端展示时就是带横线的这个样子。
_CARD_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3}$")


def looks_like_card_code(key: str) -> bool:
    """像不像一张卡密。

    服务端卡密剔掉了 O/0、I/1、L、Z/2、S/5、B/8、V/U、G/C 这些易混字母，
    但客户手抄难免出错，所以这里只认「4 组 16 位」这个形状，
    不逐位核对字母表 —— 认错的代价只是一句提示，不值得为此漏掉真卡密。
    """
    return bool(_CARD_CODE_RE.match((key or "").strip()))


def _card_code_hint(where_to: str) -> str:
    return (f"这填的是卡密（4 组 16 位），卡密只能拿去{where_to}兑换套餐和额度，"
            "接口不认它。")


def open_api_key_problem(key: str) -> str:
    """「开放 API Key」这一框：认出填错的东西，返回一句客户看得懂的话。

    返回空串表示形状没问题。
    """
    text = (key or "").strip()
    if not text:
        return ""
    if looks_like_card_code(text):
        return _card_code_hint("gdb15 后台「套餐与用量」页") + (
            "开放 API Key 要去「开放 API」页签发，开头是 " + OPEN_KEY_PREFIX + "。")
    if text.startswith(PUSH_KEY_PREFIX):
        return ("这填的是「线索推送」用的那把 Key（" + PUSH_KEY_PREFIX + " 开头），"
                "不是开放 API Key。两把 Key 互不通用：开放 API Key 要去 gdb15 后台"
                "「开放 API」页单独签发，开头是 " + OPEN_KEY_PREFIX + "。")
    return ""


def push_key_problem(key: str) -> str:
    """「授权 / 线索推送」的 API Key 这一框：该填 lk_ 开头那把。

    返回空串表示形状没问题。
    """
    text = (key or "").strip()
    if not text:
        return ""
    if looks_like_card_code(text):
        return _card_code_hint("gdb15 后台「套餐与用量」页") + (
            "这里要填的是同页「对接用 API Key」那把，开头是 " + PUSH_KEY_PREFIX + "。")
    if text.startswith(OPEN_KEY_PREFIX):
        return ("这填的是「开放 API」页签发的 Key（" + OPEN_KEY_PREFIX + " 开头），"
                "授权和线索推送不认它。这里要填 gdb15 后台「套餐与用量」页"
                "「对接用 API Key」那把，开头是 " + PUSH_KEY_PREFIX + "。")
    return ""
