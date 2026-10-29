"""gdb15 开放 API 客户端（CreatorHub 侧）。

客户在 gdb15 后台签发一把「开放 API Key」，就能把 CRM 里的线索拉到自己机器上，
做成 Excel 表格；也能把本机跟进的状态回写回 CRM，让两边的口径一致。

这件事有两个坑，不处理的话客户会以为「同步完了」，其实少了一截：

1. **翻页要用服务端给的游标，不要自己拿时间戳当游标。**
   服务端的过滤是严格大于（gt），同一毫秒进来的一批线索，
   拿时间戳当游标就永远拉不到，而且下次也不会补回来。
   ``nextCursor`` 里带的是「时间戳 + id」，原样传回去就既不重也不漏。

2. **开放 API 的 Key 和采集推送用的 Key 不是同一把。**
   开放 API 走 ``x-open-api-key``，采集推送走 ``x-api-key``。填错一把就是 401。

另外一条边界：Key 只存在本机，导出表格时一把明文 Key 都不写，
接口返回给页面的也只有「有没有配」这一个布尔值。
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .settings import get_setting, set_setting

OPEN_API_PREFIX = "/api/v1/open"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# 服务端 pageSize 上限就是 200，再大不给了。
PAGE_SIZE = 200

# 一次导出最多拉多少条。表格是拿来看的，不是拿来当数据仓库的；
# 真要全量同步，客户该用开放 API 的分页接口自己接。
MAX_EXPORT_LEADS = 5000

XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
CSV_MIME = "text/csv; charset=utf-8"

STATUS_LABELS = {
    "PENDING": "待跟进",
    "CONTACTED": "已触达",
    "REPLIED": "已回复",
    "CONVERTED": "已成交",
    "LOST": "已流失",
}

INTENT_LABELS = {
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
}

PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
    "channels": "视频号",
    "weixin": "微信",
    "bilibili": "哔哩哔哩",
    "weibo": "微博",
}

_HEADER_FILL = PatternFill("solid", fgColor="17324D")
_HEADER_FONT = Font(name="Microsoft YaHei", color="FFFFFF", bold=True)
_LINK_FONT = Font(name="Microsoft YaHei", size=10, color="2563EB", underline="single")
_BODY_FONT = Font(name="Microsoft YaHei", size=10)
_WRAP = Alignment(vertical="top", wrap_text=True)


class OpenApiError(Exception):
    """一句能直接给客户看的错误。

    原始异常（HTTP 状态码、堆栈）对客户没有意义 —— 客户要知道自己下一步干什么。
    """


def _text(value: Any) -> str:
    """把用户控制的内容变成无害的 Excel 字符串。

    评论正文是从网上收来的，第一字符如果是 ``=``/``+``/``-``/``@``，
    Excel 打开时会当成公式执行 —— 这叫公式注入。所以这里统一加前缀转义。
    """
    if value is None:
        return ""
    result = str(value)
    if result[:1] in ("=", "+", "-", "@"):
        return "'" + result
    return result


def site_base() -> str:
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


def normalize_base_url(raw: str) -> str:
    """把客户填的地址规范成 ``https://host/api/v1/open``。

    客户多半只填 ``https://www.gdb15.com``，也有人直接粘了带接口前缀的地址。
    两种都要收下 —— 但只收 HTTPS：明文地址里躺着的是一把能拉全量线索的 Key。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlsplit(text)
    if parsed.scheme != "https":
        raise ValueError("开放 API 地址必须是 HTTPS")
    if not parsed.netloc:
        raise ValueError("开放 API 地址不完整，缺少域名")
    if parsed.query or parsed.fragment:
        raise ValueError("开放 API 地址里不能带参数或锚点")
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    if not base.endswith(OPEN_API_PREFIX):
        base = f"{base}{OPEN_API_PREFIX}"
    return base


def open_api_base() -> str:
    """开放 API 基地址。没单独配就沿用 gdb15 站点地址。"""
    configured = (get_setting("gdb15_open_base_url", "") or "").strip()
    if configured:
        try:
            return normalize_base_url(configured)
        except ValueError:
            return ""
    base = site_base()
    return f"{base}{OPEN_API_PREFIX}" if base else ""


def open_api_key() -> str:
    """开放 API Key。和推送用的 Key 是两把，客户在 gdb15 后台单独签发。"""
    return (get_setting("gdb15_open_api_key", "") or "").strip()


def open_api_status() -> dict:
    """给设置页看的情况。**永远不返回明文 Key。**"""
    base = open_api_base()
    key = open_api_key()
    return {
        "base_url": base,
        "api_key_set": bool(key),
        "configured": bool(base and key),
    }


def save_open_api_settings(base_url: str | None = None,
                            api_key: str | None = None) -> dict:
    """保存开放 API 配置。

    api_key 传空串等于「不修改」—— 页面上留空表示不改已保存的 Key，
    这是这套设置页一贯的约定。
    """
    if base_url is not None:
        set_setting("gdb15_open_base_url", normalize_base_url(base_url))
    if api_key is not None and api_key.strip():
        set_setting("gdb15_open_api_key", api_key.strip())
    return open_api_status()


def _error_message(status_code: int, payload: Any) -> str:
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("error") or "").strip()
    if status_code == 401:
        return ("开放 API Key 无效或已被吊销。去 gdb15「开放 API」页确认这把还在；"
                "完整 Key 只显示一次，丢了只能重新签发。")
    if status_code == 403:
        return f"这把 Key 没有权限做这件事（{detail or '可能只给了读权限，或套餐已到期'}）。"
    if status_code == 429:
        return "拉得太快了，等一分钟再试。"
    if status_code >= 500:
        return f"gdb15 服务端出错（HTTP {status_code}），过一会儿再试。"
    return f"gdb15 返回异常（HTTP {status_code}）{('：' + detail) if detail else ''}。"


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   api_key: str, *, params: dict | None = None,
                   body: dict | None = None) -> dict:
    headers = {"x-open-api-key": api_key, "Accept": "application/json"}
    try:
        response = await client.request(
            method, url, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        raise OpenApiError(f"连不上 gdb15：{exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        raise OpenApiError(_error_message(response.status_code, payload))
    if not isinstance(payload, dict):
        raise OpenApiError("gdb15 返回的内容格式不对。")
    return payload


async def collect_leads(*, base_url: str | None = None,
                        api_key: str | None = None,
                        page_size: int = PAGE_SIZE,
                        max_leads: int = MAX_EXPORT_LEADS,
                        platform: str = "",
                        status: str = "",
                        intent_level: str = "",
                        source_keyword: str = "",
                        since: str = "",
                        client: httpx.AsyncClient | None = None) -> dict:
    """把线索全量拉回来。

    第一页走翻页模式（不带游标），之后一律走游标模式。理由：游标定位的是一个
    确定的位置（时间戳 + id），翻页定位的是一个偏移量 —— 拉的过程中正好有新线索
    进来时，偏移量会往后挪，中间那几条就被跳过了，而且是静悄悄地漏掉。

    返回 ``{"leads": [...], "truncated": bool, "total": int}``。
    """
    base = (base_url or open_api_base()).rstrip("/")
    key = (api_key or open_api_key()).strip()
    if not base:
        raise OpenApiError("还没填 gdb15 地址。")
    if not key:
        raise OpenApiError("还没填开放 API Key。")

    params: dict[str, str] = {"pageSize": str(min(max(page_size, 1), PAGE_SIZE))}
    for name, value in (
        ("platform", platform),
        ("status", status),
        ("intentLevel", intent_level),
        ("sourceKeyword", source_keyword),
    ):
        if value and value.strip():
            params[name] = value.strip()
    if since:
        params["since"] = since

    leads: list[dict] = []
    cursor = ""
    truncated = False

    async def _pull(active: httpx.AsyncClient) -> None:
        nonlocal cursor, truncated
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data = await _request(active, "GET", f"{base}/leads", key,
                                  params=page_params)
            items = data.get("items") or []
            if isinstance(items, list):
                leads.extend(item for item in items if isinstance(item, dict))

            next_cursor = data.get("nextCursor") or ""
            has_more = bool(data.get("hasMore"))

            if len(leads) >= max_leads:
                del leads[max_leads:]
                truncated = True
                return
            if not has_more or not items:
                return
            if not next_cursor or next_cursor == cursor:
                # 服务端说还有下一页却不给游标（或游标原地不动）。
                # 再拉一遍只会把同一页重复写进表格，不如停在这里。
                return
            cursor = next_cursor

    if client is not None:
        await _pull(client)
    else:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     follow_redirects=True) as active:
            await _pull(active)

    return {"leads": leads, "truncated": truncated, "total": len(leads)}


async def push_status(lead_id: str, status: str, note: str = "",
                      *, base_url: str | None = None,
                      api_key: str | None = None,
                      client: httpx.AsyncClient | None = None) -> dict:
    """把跟进状态回写到 gdb15。

    只有签发时给了 ``leads:write`` 权限的 Key 才调得动。
    """
    base = (base_url or open_api_base()).rstrip("/")
    key = (api_key or open_api_key()).strip()
    if not base:
        raise OpenApiError("还没填 gdb15 地址。")
    if not key:
        raise OpenApiError("还没填开放 API Key。")
    state = (status or "").strip().upper()
    if state not in STATUS_LABELS:
        raise OpenApiError("线索状态不合法。")
    body: dict[str, Any] = {"status": state}
    if (note or "").strip():
        body["note"] = note.strip()

    if client is not None:
        return await _request(client, "PUT",
                              f"{base}/leads/{lead_id}/status", key, body=body)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 follow_redirects=True) as active:
        return await _request(active, "PUT",
                              f"{base}/leads/{lead_id}/status", key, body=body)


def _label(value: Any, labels: dict[str, str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return labels.get(text, text)


def _platform_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return PLATFORM_LABELS.get(text.lower(), text)


def _local_time(value: Any) -> datetime | None:
    """把接口的 ISO 时间转成本地时间。

    接口给的是 UTC（末尾带 Z）。表格里直接写 UTC，客户会以为比实际早了八小时，
    于是「昨天的线索」被算到今天头上。
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone().replace(tzinfo=None)


def lead_rows(leads: Iterable[dict]) -> list[list[str]]:
    """把线索摊成表格行。字段顺序和表头一一对应。"""
    rows: list[list[str]] = []
    for lead in leads:
        owner = lead.get("owner") or {}
        owner_name = ""
        if isinstance(owner, dict):
            owner_name = str(owner.get("nickname") or owner.get("email") or "").strip()
        captured = _local_time(lead.get("capturedAt"))
        updated = _local_time(lead.get("updatedAt"))
        rows.append([
            _platform_label(lead.get("platform")),
            _text(lead.get("nickname")),
            _text(lead.get("commentText")),
            _text(lead.get("sourceUrl")),
            _text(lead.get("sourceKeyword")),
            _text(lead.get("intentKeyword")),
            _label(lead.get("intentLevel"), INTENT_LABELS),
            _label(lead.get("status"), STATUS_LABELS),
            _text(owner_name),
            captured.strftime("%Y-%m-%d %H:%M:%S") if captured else "",
            updated.strftime("%Y-%m-%d %H:%M:%S") if updated else "",
        ])
    return rows


HEADERS = [
    "平台", "昵称", "评论内容", "来源链接", "来源关键词",
    "意向关键词", "意向等级", "状态", "负责人", "采集时间", "更新时间",
]

COLUMN_WIDTHS = [10, 16, 48, 40, 16, 16, 10, 10, 14, 20, 20]


def build_xlsx(leads: Iterable[dict], *, note: str = "") -> bytes:
    """把线索做成 Excel 表格。"""
    rows = lead_rows(leads)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "线索"
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
    for row in rows:
        sheet.append(row)

    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        for cell in row:
            cell.font = _BODY_FONT
            cell.alignment = _WRAP
    # 来源链接做成可点的超链接 —— 客户拿到表格第一步就是想点回原评论。
    for index in range(2, sheet.max_row + 1):
        cell = sheet.cell(row=index, column=4)
        value = str(cell.value or "")
        if value.startswith("http://") or value.startswith("https://"):
            cell.hyperlink = value
            cell.font = _LINK_FONT

    for index, width in enumerate(COLUMN_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{sheet.max_row}"
    if note:
        sheet.insert_rows(1)
        sheet["A1"] = note
        sheet["A1"].font = Font(name="Microsoft YaHei", size=9, color="64748B")
        sheet.freeze_panes = "A3"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_csv(leads: Iterable[dict]) -> bytes:
    """把线索做成 CSV。

    逗号、引号、换行都要按 RFC 4180 转义 —— 评论正文里出现一个英文逗号，
    转义错了整列就串位，客户导进自己的表格里全是乱的。
    Excel 打开中文 CSV 需要 BOM，没有它第一列会变成乱码。
    """
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for row in lead_rows(leads):
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def since_days_ago(days: int) -> str:
    """最近 N 天的起始时间（ISO）。days=0 表示不限。"""
    if days <= 0:
        return ""
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.isoformat()


def build_export_query(**filters: Any) -> str:
    """拼查询串（调试用）。"""
    params = {k: v for k, v in filters.items() if v}
    return urlencode(params)
