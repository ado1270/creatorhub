"""Read one shared work in its account browser without issuing extra API calls."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from .douyin_auth import douyin_login_prompt_visible
from ..platforms.douyin.detail import (
    detail_response_error, extract_aweme_detail, parse_detail_response,
)


_DETAIL_SNAPSHOT_JS = """(awemeId) => {
  const roots = [window._ROUTER_DATA, window.__INITIAL_STATE__];
  const render = document.getElementById('RENDER_DATA');
  if (render) {
    try { roots.push(JSON.parse(render.textContent)); }
    catch (_) { try { roots.push(JSON.parse(decodeURIComponent(render.textContent))); } catch (_) {} }
  }
  const seen = new WeakSet(); let count = 0;
  const visit = (value, depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 12 || count++ > 5000 || seen.has(value)) return null;
    seen.add(value);
    if (String(value.aweme_id || '') === awemeId && (value.video || value.images)) return value;
    for (const child of Object.values(value).slice(0, 200)) {
      const found = visit(child, depth + 1); if (found) return found;
    }
    return null;
  };
  const text = (document.body?.innerText || '').slice(0, 20000);
  // The platform can serve a nearly empty verification interstitial: its
  // only visible evidence is the document title, with no slider text yet.
  const title = document.title || '';
  return {item: visit(roots),
    verification: /验证码中间页|安全验证|人机验证/.test(title) ||
      /请完成安全验证|拖动滑块|访问过于频繁|完成验证后继续/.test(text),
    unavailable: /作品不存在|视频不存在|作品已删除|视频已删除|该作品为私密作品/.test(text)};
}"""


def _platform_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"https", "http"} and any(
            host == base or host.endswith("." + base) for base in ("douyin.com", "iesdouyin.com"))
    except ValueError:
        return False


async def fetch_douyin_video_detail(mgr, identity, aweme_id: str, *,
                                   source_url: str = "", timeout_ms: int = 15000) -> tuple[dict, str]:
    page = None
    result, last_error, denied = {}, "", ""

    async def on_response(response):
        nonlocal result, last_error, denied
        if not _platform_url(response.url):
            return
        path = urlsplit(response.url).path.rstrip("/")
        is_detail = path == "/aweme/v1/web/aweme/detail"
        if not is_detail and path not in {
                "/aweme/v1/web/aweme/post", "/aweme/v1/web/aweme/feed", "/aweme/v1/web/feed"}:
            return
        try:
            payload = await response.json() if response.status == 200 else None
            # An unrelated recommendation failure is not this work's failure.
            if is_detail:
                params = dict(parse_qsl(urlsplit(response.url).query))
                if params.get("aweme_id") not in (None, aweme_id):
                    return
                item, error = parse_detail_response(payload, aweme_id, response.status)
                last_error = error
                if error == "logged_out" or error == "aweme_unavailable" or error.startswith("risk:"):
                    denied = error
            else:
                error = detail_response_error(payload, response.status)
                item = None if error else extract_aweme_detail(payload, aweme_id)
            if item:
                result = item
        except Exception:
            if is_detail:
                last_error = "detail_body_unavailable"

    try:
        page = await mgr.new_page(identity, block_media=False)
        page.on("response", on_response)
        target = source_url if _platform_url(source_url) else f"https://www.douyin.com/video/{aweme_id}"
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        for elapsed in range(0, max(500, timeout_ms), 500):
            await page.wait_for_timeout(500)
            if denied:
                return {}, denied
            if result:
                return result, ""
            try:
                snapshot = await page.evaluate(_DETAIL_SNAPSHOT_JS, aweme_id)
            except Exception:
                snapshot = {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            if snapshot.get("verification"):
                return {}, "risk:verification_required"
            if snapshot.get("unavailable"):
                return {}, "aweme_unavailable"
            item = extract_aweme_detail(snapshot.get("item"), aweme_id)
            if item:
                return item, ""
            if elapsed >= 1000 and await douyin_login_prompt_visible(page) is True:
                return {}, "logged_out"
        return {}, last_error or "no_detail_response"
    except Exception as exc:
        if "closed" in str(exc).lower():
            return {}, "browser_closed"
        return {}, f"network:{type(exc).__name__}"
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        release = getattr(mgr, "release_task_page", None)
        if callable(release):
            try:
                await release(identity, page)
            except Exception:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
        elif page is not None:
            try:
                await page.close()
            except Exception:
                pass
