"""Douyin APIs that require the official browser-bound SecureSDK."""
from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from typing import Optional
from urllib.parse import quote

from .identity import Identity
from .manager import BrowserManager

_SECURE_WRITE_PATHS = {
    "/aweme/v1/web/comment/publish/",
    "/aweme/v1/web/commit/follow/user/",
}
_DOUYIN_WRITE_BASE = "https://www-hj.douyin.com"
_SEARCH_PATH = "/aweme/v1/web/general/search/stream/"


async def douyin_secure_search_api(
        context, keyword: str, *, max_results: int = 20, max_pages: int = 3,
        search_sort: str = "general", publish_time: str = "all",
        content_type: str = "all", min_likes: int = 0,
        min_comments: int = 0, timeout_ms: int = 30000) -> tuple[list[dict], str]:
    """Call the current search/stream API through Douyin's own SecSDK runtime.

    This does not read search-page DOM or click search controls. Chromium only
    supplies the first-party runtime that appends volatile security fields; the
    returned works come exclusively from the JSON streaming API.
    """
    from .fetcher import (decode_douyin_search_payloads,
                          extract_search_awemes,
                          _douyin_search_item_matches,
                          _sort_douyin_search_items)

    page = await context.new_page()
    found: dict[str, dict] = {}
    offset = 0
    sort_codes = {"general": "0", "most_liked": "1", "latest": "2"}
    time_codes = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
    try:
        cookies = await context.cookies("https://www.douyin.com/")
        cookie_values = {
            str(cookie.get("name") or ""): str(cookie.get("value") or "")
            for cookie in cookies if isinstance(cookie, dict)
        }
        screen_width = cookie_values.get("dy_swidth") or "1536"
        screen_height = cookie_values.get("dy_sheight") or "864"
        await page.goto("https://www.douyin.com/jingxuan",
                        wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(min(6000, max(2500, timeout_ms // 6)))
        for _ in range(max(1, min(10, int(max_pages or 1)))):
            params = {
                "device_platform": "webapp", "aid": "6383",
                "channel": "channel_pc_web", "pc_client_type": "1",
                "version_code": "190600", "version_name": "19.6.0",
                "cookie_enabled": "true", "browser_language": "zh-CN",
                "browser_platform": "Win32", "browser_name": "Chrome",
                "browser_online": "true", "engine_name": "Blink",
                "os_name": "Windows", "os_version": "10", "platform": "PC",
                "downlink": "10", "effective_type": "4g", "round_trip_time": "150",
                "keyword": keyword, "search_channel": "aweme_general",
                "search_source": "normal_search", "query_correct_type": "1",
                "is_filter_search": "0", "offset": str(offset),
                # The official first page currently fixes count at 10 even
                # when callers only consume a smaller result limit.
                "count": "10",
                "list_type": "single", "need_filter_settings": "1",
                "enable_history": "1", "disable_rs": "0", "from_group_id": "",
                "pc_libra_divert": "Windows",
                "pc_search_top_1_params": '{"enable_ai_search_top_1":1}',
                "support_h265": "0", "support_dash": "1",
                "update_version_code": "0",
                "screen_width": screen_width,
                "screen_height": screen_height,
            }
            if search_sort != "general":
                params["sort_type"] = sort_codes.get(search_sort, "0")
            if publish_time != "all":
                params["publish_time"] = time_codes.get(publish_time, "0")
            if content_type == "video":
                params["type"] = "video"
            result = await page.evaluate("""
            async input => {
              const query = new URLSearchParams({
                ...input.params,
                browser_version: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [,'130'])[1] + '.0.0.0',
                engine_version: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [,'130'])[1] + '.0.0.0',
                cpu_core_num: String(navigator.hardwareConcurrency || 8),
                device_memory: String(navigator.deviceMemory || 8),
              });
              const response = await fetch(input.path + '?' + query.toString(), {
                method:'GET', credentials:'include',
                headers:{Accept:'application/json, text/plain, */*'},
              });
              return {status:response.status, text:await response.text()};
            }
            """, {"path": _SEARCH_PATH, "params": params})
            if int(result.get("status") or 0) != 200:
                return list(found.values()), f"http_{result.get('status')}"
            payloads = decode_douyin_search_payloads(result.get("text") or "")
            if not payloads:
                return list(found.values()), "invalid_stream_response"
            has_more = False
            next_offset = offset
            for payload in payloads:
                code = payload.get("status_code")
                message = str(payload.get("status_msg") or payload.get("message") or "")
                marker = message.casefold()
                if code not in (None, 0, "0"):
                    if str(code) in {"8", "2483"} or any(
                            word in marker for word in ("未登录", "登录后", "login")):
                        return list(found.values()), "logged_out"
                    if any(word in marker for word in (
                            "verify", "captcha", "risk", "验证", "风控")):
                        return list(found.values()), "verification_required"
                    return list(found.values()), f"api_rejected:status_code={code}"
                nil_info = payload.get("search_nil_info") or {}
                nil_marker = " ".join(str(nil_info.get(k) or "") for k in
                                      ("search_nil_type", "search_nil_item", "text_type"))
                if any(word in nil_marker.casefold() for word in (
                        "verify", "captcha", "risk")):
                    return list(found.values()), "verification_required"
                for item in extract_search_awemes(payload):
                    aid = str(item.get("aweme_id") or "")
                    if aid and _douyin_search_item_matches(
                            item, content_type=content_type,
                            publish_time=publish_time, min_likes=min_likes,
                            min_comments=min_comments):
                        found.setdefault(aid, item)
                has_more = has_more or bool(
                    payload.get("has_more") or payload.get("has_more_item"))
                next_offset = max(next_offset, int(payload.get("offset") or 0))
            if len(found) >= max_results or not has_more:
                break
            new_offset = next_offset or (offset + 10)
            if new_offset <= offset:
                break
            offset = new_offset
            await page.wait_for_timeout(500)
        return _sort_douyin_search_items(
            list(found.values()), search_sort)[:max_results], ""
    except Exception as exc:
        return list(found.values()), f"secure_search:{type(exc).__name__}"
    finally:
        with suppress(Exception):
            await page.close()


async def douyin_secure_post_api(
        mgr: BrowserManager, identity: Identity, path: str, params: dict,
        form: dict, *, base_url: str = _DOUYIN_WRITE_BASE,
        timeout_ms: int = 30000) -> tuple[Optional[dict], str, bool]:
    """Use Douyin's official POST wrapper without clicking page controls."""
    path = str(path or "")
    if path not in _SECURE_WRITE_PATHS:
        return None, "unsupported_secure_write_path", False
    page = await mgr.new_page(identity, block_media=False)
    runtime_injected = False

    async def expose_runtime(route, request):
        nonlocal runtime_injected
        response = await route.fetch()
        try:
            body = await response.text()
        except Exception:
            await route.fulfill(response=response)
            return
        if (not runtime_injected and "webpackChunkdouyin_web" in body
                and "bundler=rspack@" in body and ".ruid=" in body):
            match = re.search(r"([A-Za-z_$][\w$]*)\.ruid=", body)
            if match:
                name = match.group(1)
                replacement = (
                    "Object.defineProperty(self,'__creatorhub_require__',"
                    f"{{value:{name},configurable:false,writable:false}}),"
                    f"{name}.ruid=")
                body = body[:match.start()] + replacement + body[match.end():]
                runtime_injected = True
                await route.fulfill(response=response, body=body)
                return
        await route.fulfill(response=response)

    try:
        await page.route("**/runtime~client-entry*.js", expose_runtime)
        await page.goto(
            "https://www.douyin.com/jingxuan",
            wait_until="domcontentloaded", timeout=timeout_ms)
        # The wrapper module may be registered before its dependent chunks and
        # page singletons finish booting. Calling it immediately can make the
        # webpack runtime require an undefined factory ("reading 'call'").
        await page.wait_for_timeout(min(10000, max(3000, timeout_ms // 3)))
        payload = {
            "path": path, "params": dict(params or {}),
            "form": dict(form or {}), "baseUrl": str(base_url or ""),
            "timeoutMs": max(5000, int(timeout_ms)),
        }
        script = """
        (async input => {
          const root = document.documentElement;
          const finish = value => root.setAttribute(
            'data-creatorhub-secure-api', JSON.stringify(value));
          const deadline = Date.now() + input.timeoutMs;
          while (!window.__creatorhub_require__ && Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, 100));
          }
          try {
            const req = window.__creatorhub_require__;
            if (!req) return finish({error:'webpack_runtime_unavailable'});
            const findModule = () => {
              if (req.m && req.m[599090]) return 599090;
              if (req.m) {
                for (const [id, factory] of Object.entries(req.m)) {
                  const source = String(factory);
                  if (source.includes('application/x-www-form-urlencoded') &&
                      source.includes('method:\"POST\"') &&
                      source.includes('/aweme/v1/web/aweme/detail/')) {
                    return id;
                  }
                }
              }
              return null;
            };
            let moduleId = findModule();
            while (moduleId === null && Date.now() < deadline) {
              await new Promise(resolve => setTimeout(resolve, 100));
              moduleId = findModule();
            }
            if (moduleId === null) return finish({error:'post_wrapper_module_unavailable'});
            const exports = req(moduleId);
            let post = exports && exports.v_;
            if (typeof post !== 'function' && exports) {
              post = Object.values(exports).find(value => {
                if (typeof value !== 'function') return false;
                const source = String(value);
                return source.includes('method:\"POST\"') &&
                       source.includes('application/x-www-form-urlencoded');
              });
            }
            if (typeof post !== 'function') return finish({error:'post_wrapper_unavailable'});
            const response = await post(
              input.path, input.params, input.form, {}, input.baseUrl);
            finish({response});
          } catch (error) {
            finish({error:String(error), stack:error && error.stack});
          }
        })(%s);
        """ % json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"))
        await page.add_script_tag(content=script)
        raw = ""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            raw = await page.locator("html").get_attribute(
                "data-creatorhub-secure-api") or ""
            if raw:
                break
            await page.wait_for_timeout(100)
        if not raw:
            return None, "write_uncertain:secure_api_timeout", True
        result = json.loads(raw)
        if result.get("error"):
            detail = str(result.get("error") or "secure_api_error")
            stack = " ".join(str(result.get("stack") or "").split())[:500]
            if stack:
                detail = f"{detail} [{stack}]"
            return None, f"secure_api:{detail}", False
        response = result.get("response")
        if not isinstance(response, dict):
            return None, "write_uncertain:secure_api_invalid_response", True
        return response, "", False
    except Exception as exc:
        return None, f"secure_api:{type(exc).__name__}", False
    finally:
        with suppress(Exception):
            await page.close()
