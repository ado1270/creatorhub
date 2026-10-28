"""Share-download regressions using temporary accounts and offline readers."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.db as db
import app.main as main
from app.browser.identity import Identity
from app.engine.monitor import MonitorEngine
from app.engine.share_downloader import ShareDownloadError, ShareDownloadFallback
from app.browser.douyin_detail import fetch_douyin_video_detail
from app.models import DouyinAccount
from app.platforms.douyin.client import DouyinClient
from test_project_optimizations import local_project, store


AWEME_ID = "1234567890123456789"
URL = f"https://www.douyin.com/video/{AWEME_ID}"


def detail(aweme_id=AWEME_ID):
    return {"aweme_id": aweme_id, "desc": "fixture", "author": {"nickname": "fixture"},
            "video": {"play_addr": {"url_list": ["https://media.example/fixture.mp4"]}}}


@pytest.fixture
def share(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.risk_control.enabled = False
    cfg.engine.quiet_hours_enabled = False
    account_id = store(DouyinAccount(platform="douyin", status="active", storage_state=
        '{"cookies":[{"name":"sessionid","value":"TOKEN","domain":".douyin.com"}]}'))
    monkeypatch.setattr(local_project.browser, "identity_for", lambda account: Identity.from_account(
        account, cfg.engine.profiles_dir, cfg.engine.user_agent), raising=False)
    monkeypatch.setattr(main, "engine", MonitorEngine(cfg, local_project.browser))
    client = SimpleNamespace(last_error="empty_body", fetch_video_detail=AsyncMock(return_value=None))

    @asynccontextmanager
    async def session_scope():
        yield client

    client.session_scope = session_scope
    monkeypatch.setattr(main, "DouyinClient", lambda *_a, **_kw: client)
    resolve = AsyncMock(return_value=AWEME_ID)
    monkeypatch.setattr(main, "resolve_aweme_id", resolve)
    browser_read = AsyncMock(return_value=(detail(), ""))
    monkeypatch.setattr(main, "fetch_douyin_video_detail", browser_read, raising=False)
    return SimpleNamespace(project=local_project, account_id=account_id, client=client,
                           browser_read=browser_read, resolve=resolve)


def inspect(share):
    return asyncio.run(main._douyin_native_share(
        URL, account_id=share.account_id, output_root=share.project.root,
        quality="highest", should_download=False, save_metadata=True,
        save_thumbnail=True, proxy="", user_agent=share.project.cfg.engine.user_agent))


def test_default_hybrid_reads_api_first_and_keeps_browser_closed_on_success(share):
    share.client.fetch_video_detail.return_value = detail()
    share.client.last_error = ""
    result = inspect(share)
    assert result["ok"] and result["metadata"]["id"] == AWEME_ID
    assert result["metadata"]["read_transport"] == "api"
    share.client.fetch_video_detail.assert_awaited_once_with(AWEME_ID)
    share.browser_read.assert_not_awaited()
    with db.get_session() as session:
        assert session.get(DouyinAccount, share.account_id).status == "active"


def test_browser_mode_never_uses_direct_detail_api(share):
    share.project.cfg.engine.douyin_read_mode = "browser"
    assert inspect(share)["ok"]
    share.client.fetch_video_detail.assert_not_awaited()
    share.browser_read.assert_awaited_once()


def test_api_mode_reports_empty_detail_without_claiming_expired_login(share):
    share.project.cfg.engine.douyin_read_mode = "api"
    with pytest.raises(ShareDownloadError) as caught:
        inspect(share)
    assert "登录态" not in str(caught.value) and "empty_body" in str(caught.value)
    assert not isinstance(caught.value, ShareDownloadFallback)
    share.browser_read.assert_not_awaited()


def test_api_only_failure_never_invokes_generic_or_browser_reader(share, monkeypatch):
    share.project.cfg.engine.douyin_read_mode = "api"
    generic = SimpleNamespace(inspect=AsyncMock(side_effect=AssertionError("not the selected API")))
    monkeypatch.setattr(main, "ShareDownloader", lambda *_a, **_kw: generic)
    result = asyncio.run(main.share_download(main.ShareDownloadIn(
        share_text=URL, account_id=share.account_id, download=False)))
    assert not result["ok"] and "empty_body" in result["results"][0]["error"]
    generic.inspect.assert_not_awaited()
    share.browser_read.assert_not_awaited()


@pytest.mark.parametrize("error,expected", [
    ("logged_out", "重新登录"), ("verification_required:http_403", "验证"),
    ("aweme_unavailable", "作品"),
])
@pytest.mark.parametrize("mode", ["api", "hybrid", "browser"])
def test_explicit_denials_do_not_fall_back_to_another_reader(share, error, expected, mode):
    share.project.cfg.engine.douyin_read_mode = mode
    share.client.last_error = error
    share.browser_read.return_value = ({}, error)
    with pytest.raises(ShareDownloadError) as caught:
        inspect(share)
    assert expected in str(caught.value)
    if mode in {"api", "hybrid"}:
        share.browser_read.assert_not_awaited()
    else:
        share.client.fetch_video_detail.assert_not_awaited()


def test_matching_api_detail_does_not_open_browser(share):
    share.project.cfg.engine.douyin_read_mode = "api"
    share.client.fetch_video_detail.return_value = detail()
    share.client.last_error = ""
    assert inspect(share)["metadata"]["id"] == AWEME_ID
    share.browser_read.assert_not_awaited()


def test_hybrid_falls_back_to_browser_only_after_inconclusive_api_read(share):
    order = []

    async def api_read(_aweme_id):
        order.append("api")
        return None

    async def browser_read(*_args, **_kwargs):
        order.append("browser")
        return detail(), ""

    share.client.fetch_video_detail.side_effect = api_read
    share.browser_read.side_effect = browser_read
    result = inspect(share)
    assert result["metadata"]["id"] == AWEME_ID
    assert result["metadata"]["read_transport"] == "browser"
    assert order == ["api", "browser"]
    share.browser_read.assert_awaited_once()
    share.client.fetch_video_detail.assert_awaited_once_with(AWEME_ID)


def test_hybrid_propagates_explicit_api_denial_without_browser_retry(share):
    share.client.last_error = "risk:verification_required:http_403"
    with pytest.raises(ShareDownloadError, match="验证") as caught:
        inspect(share)
    assert not isinstance(caught.value, ShareDownloadFallback)
    share.browser_read.assert_not_awaited()


@pytest.mark.parametrize("reason", ["uifid_not_found", "signature_not_found", "sign_invalid", "sign_expired"])
def test_signature_failures_are_precise_and_do_not_launch_another_reader(share, reason):
    share.client.last_error = "api_signature:" + reason
    with pytest.raises(ShareDownloadError) as caught:
        inspect(share)
    assert not isinstance(caught.value, ShareDownloadFallback)
    assert "这不代表账号登录失效" in str(caught.value)
    assert "完成验证" not in str(caught.value)
    share.browser_read.assert_not_awaited()


def test_locally_missing_device_id_allows_hybrid_fallback_but_not_api_mode(share):
    share.client.last_error = "api_signature:missing_uifid"
    assert inspect(share)["metadata"]["read_transport"] == "browser"
    share.browser_read.assert_awaited_once()
    share.browser_read.reset_mock()
    share.project.cfg.engine.douyin_read_mode = "api"
    with pytest.raises(ShareDownloadError, match="缺少 API 请求所需的 UIFID"):
        inspect(share)
    share.browser_read.assert_not_awaited()


def test_unknown_forbidden_does_not_claim_confirmed_captcha(share):
    share.client.last_error = "risk:request_denied:http_403"
    with pytest.raises(ShareDownloadError, match="尚未明确") as caught:
        inspect(share)
    assert "完成验证" not in str(caught.value)
    share.browser_read.assert_not_awaited()


def test_hybrid_stops_at_browser_verification_without_retrying_the_direct_api(share):
    share.browser_read.return_value = ({}, "risk:verification_required")
    with pytest.raises(ShareDownloadError, match="验证") as caught:
        inspect(share)
    assert not isinstance(caught.value, ShareDownloadFallback)
    share.client.fetch_video_detail.assert_awaited_once()
    share.browser_read.assert_awaited_once()


def test_share_transport_matrix_describes_the_actual_hybrid_order(share):
    from app.transport_matrix import resolve_transport
    transport = resolve_transport(share.project.cfg, "douyin", "share_download")
    assert transport["hybrid_order"] == "api_first"
    assert transport["fallback"] == "browser_on_confirmed_failure"


def test_busy_manual_window_is_reported_without_waiting_for_its_lifetime(share):
    main.open_browsers[share.account_id] = SimpleNamespace(active=True)
    with pytest.raises(ShareDownloadError, match="关闭.*浏览器"):
        inspect(share)
    share.resolve.assert_not_awaited()
    share.client.fetch_video_detail.assert_not_awaited()


@pytest.mark.parametrize("payload", [
    {"status_code": 0},
    {"status_code": 0, "aweme_detail": detail("9876543210987654321")},
    {"status_code": 8, "status_msg": "请完成安全验证", "aweme_detail": detail()},
])
def test_detail_client_keeps_error_and_rejects_non_target_or_rejected_data(payload):
    client = DouyinClient("sessionid=TOKEN", "fixture")
    client._get_json = AsyncMock(return_value=payload)
    assert asyncio.run(client.fetch_video_detail(AWEME_ID)) is None
    assert client.last_error


@pytest.mark.parametrize("download", [False, True])
def test_inconclusive_native_metadata_can_fall_back_to_generic_extractor(share, monkeypatch, download):
    monkeypatch.setattr(main, "_douyin_native_share", AsyncMock(
        side_effect=ShareDownloadFallback("原生详情为空")))
    generic = SimpleNamespace(
        inspect=AsyncMock(return_value={"ok": True, "metadata": {"id": AWEME_ID}}),
        download=AsyncMock(return_value={"ok": True, "metadata": {"id": AWEME_ID}, "files": []}))
    monkeypatch.setattr(main, "ShareDownloader", lambda *_a, **_kw: generic)
    result = asyncio.run(main.share_download(main.ShareDownloadIn(
        share_text=URL, account_id=share.account_id, download=download)))
    assert result["ok"] and "通用提取器" in result["results"][0]["warnings"][0]
    method = generic.download if download else generic.inspect
    method.assert_awaited_once()
    from pathlib import Path
    assert not Path(method.await_args.kwargs["cookie_file"]).exists()
    if download:
        assert result["results"][0]["history_id"]


def test_generic_failure_retains_both_diagnostics(share, monkeypatch):
    monkeypatch.setattr(main, "_douyin_native_share", AsyncMock(
        side_effect=ShareDownloadFallback("原生详情为空")))
    generic = SimpleNamespace(inspect=AsyncMock(side_effect=ShareDownloadError("站点要求刷新会话 Cookie")))
    monkeypatch.setattr(main, "ShareDownloader", lambda *_a, **_kw: generic)
    result = asyncio.run(main.share_download(main.ShareDownloadIn(
        share_text=URL, account_id=share.account_id, download=False)))
    assert not result["ok"]
    assert result["results"][0]["error"] == "原生详情为空；通用提取器：站点要求刷新会话 Cookie"


@pytest.mark.parametrize("error", ["明确要求登录", "请完成安全验证", "媒体文件下载失败"])
def test_denied_reads_and_failed_media_downloads_never_trigger_generic_retry(share, monkeypatch, error):
    monkeypatch.setattr(main, "_douyin_native_share", AsyncMock(side_effect=ShareDownloadError(error)))
    generic = SimpleNamespace(download=AsyncMock())
    monkeypatch.setattr(main, "ShareDownloader", lambda *_a, **_kw: generic)
    result = asyncio.run(main.share_download(main.ShareDownloadIn(
        share_text=URL, account_id=share.account_id, download=True)))
    assert not result["ok"] and result["results"][0]["error"] == error
    generic.download.assert_not_awaited()


def test_browser_only_failure_never_permits_generic_fallback(share):
    share.project.cfg.engine.douyin_read_mode = "browser"
    share.browser_read.return_value = ({}, "no_detail_response")
    with pytest.raises(ShareDownloadError) as caught:
        inspect(share)
    assert not isinstance(caught.value, ShareDownloadFallback)
    share.client.fetch_video_detail.assert_not_awaited()


def test_account_read_gate_runs_before_short_link_or_detail_requests(share, monkeypatch):
    gate = AsyncMock(return_value=({}, "risk_deferred:fixture cooldown"))
    monkeypatch.setattr(main.engine, "guarded_read_pair", gate)
    with pytest.raises(ShareDownloadError, match="fixture cooldown"):
        inspect(share)
    share.resolve.assert_not_awaited()
    share.browser_read.assert_not_awaited()
    share.client.fetch_video_detail.assert_not_awaited()
    assert gate.await_args.kwargs["interactive_read"] is True


class Page:
    def __init__(self, *, responses=(), snapshot=None, login=False, broken=False):
        self.url = "about:blank"
        self.responses = responses
        self.snapshot = snapshot or {}
        self.login, self.broken = login, broken
        self.handlers = {}
        self.closed = False
        self.waits = 0

    def on(self, name, callback):
        self.handlers[name] = callback

    def remove_listener(self, name, callback):
        assert self.handlers.pop(name) == callback

    async def goto(self, url, **_kwargs):
        if self.broken:
            raise TimeoutError("fixture navigation timeout")
        self.url = url
        for response in self.responses:
            await self.handlers["response"](response)

    async def wait_for_timeout(self, _timeout):
        self.waits += 1

    async def evaluate(self, _script, _aweme_id):
        return self.snapshot

    def get_by_text(self, *_args, **_kwargs):
        return SimpleNamespace(all=AsyncMock(return_value=[
            SimpleNamespace(is_visible=AsyncMock(return_value=self.login))]))

    async def close(self):
        self.closed = True


def response(payload, *, url=None, status=200):
    return SimpleNamespace(status=status, url=url or
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={AWEME_ID}",
        json=AsyncMock(return_value=payload))


def read_page(page):
    mgr = SimpleNamespace(new_page=AsyncMock(return_value=page))
    result = asyncio.run(fetch_douyin_video_detail(mgr, SimpleNamespace(), AWEME_ID, timeout_ms=2000))
    assert page.closed and not page.handlers
    return result


@pytest.mark.parametrize("media", ["video", "images"])
def test_browser_reader_accepts_only_the_requested_video_or_note(media):
    raw = detail()
    if media == "images":
        raw.pop("video")
        raw["images"] = [{"url_list": ["https://media.example/fixture.jpg"]}]
    page = Page(responses=[response({"status_code": 0, "aweme_detail": raw})])
    item, error = read_page(page)
    assert item == raw and not error


def test_browser_reader_accepts_first_party_routed_detail_host():
    page = Page(responses=[response({"status_code": 0, "aweme_detail": detail()},
        url=f"https://www-hj.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={AWEME_ID}")])
    item, error = read_page(page)
    assert item == detail() and not error


def test_browser_reader_uses_rendered_work_state_without_reissuing_api_requests():
    page = Page(snapshot={"item": {"loaderData": {"aweme_detail": detail()}}})
    item, error = read_page(page)
    assert item["aweme_id"] == AWEME_ID and not error


@pytest.mark.parametrize("status,expected", [(401, "logged_out"), (403, "risk:"), (404, "aweme_unavailable")])
def test_browser_explicit_denial_is_not_overridden_by_cached_page_state(status, expected):
    page = Page(responses=[response(None, status=status)], snapshot={"item": detail()})
    item, error = read_page(page)
    assert not item and error.startswith(expected)


@pytest.mark.parametrize("case", ["other_work", "foreign_host", "no_response"])
def test_browser_reader_never_downloads_recommendations_or_untrusted_responses(case):
    responses = []
    if case == "other_work":
        responses = [response({"status_code": 0, "aweme_detail": detail("9876543210987654321")})]
    elif case == "foreign_host":
        responses = [response({"status_code": 0, "aweme_detail": detail()},
            url="https://douyin.com.example.invalid/aweme/v1/web/aweme/detail/")]
    item, error = read_page(Page(responses=responses))
    assert not item and error in {"missing_aweme_detail", "no_detail_response"}


@pytest.mark.parametrize("options,expected", [
    ({"login": True}, "logged_out"),
    ({"snapshot": {"verification": True}}, "risk:verification_required"),
    ({"snapshot": {"unavailable": True}}, "aweme_unavailable"),
    ({"broken": True}, "network:TimeoutError"),
])
def test_browser_failures_have_distinct_diagnostics(options, expected):
    item, error = read_page(Page(**options))
    assert not item and error == expected


@pytest.mark.parametrize("title,text,expected", [
    ("验证码中间页", "", True),
    ("安全验证", "", True),
    ("抖音", "拖动滑块完成验证", True),
    ("登录", "验证码登录", False),
    ("视频详情", "正常作品内容", False),
])
def test_browser_snapshot_detects_empty_verification_page_without_confusing_sms_login(title, text, expected):
    import json
    import shutil
    import subprocess
    from app.browser.douyin_detail import _DETAIL_SNAPSHOT_JS

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    script = """
      const vm = require('node:vm');
      const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
      const context = {window: {}, document: {
        title: input.title, body: {innerText: input.text}, getElementById: () => null
      }};
      const snapshot = vm.runInNewContext('(' + input.source + ')', context)('TARGET');
      process.stdout.write(JSON.stringify(snapshot.verification));
    """
    result = subprocess.run([node, "-e", script],
        input=json.dumps({"source": _DETAIL_SNAPSHOT_JS, "title": title, "text": text}),
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is expected


def test_browser_risk_code_without_message_does_not_use_cached_media():
    item, error = read_page(Page(responses=[response({"status_code": 8})], snapshot={"item": detail()}))
    assert not item and error.startswith("risk:")


def test_unrelated_recommendation_auth_failure_does_not_reject_the_target():
    page = Page(responses=[
        response(None, status=401, url="https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=9876543210987654321"),
        response({"status_code": 0, "aweme_detail": detail()})])
    item, error = read_page(page)
    assert item == detail() and not error


def test_native_browser_download_writes_media_metadata_and_history(share, monkeypatch):
    from pathlib import Path
    import json
    from app.platforms.douyin.extract import safe_title

    class Downloader:
        def __init__(self, *_args, **_kwargs):
            pass

        async def download_aweme(self, aweme, *, base_dir, proxy):
            assert aweme.aweme_id == AWEME_ID and proxy == ""
            target = Path(base_dir) / safe_title(aweme.author_name)
            target.mkdir(parents=True, exist_ok=True)
            media = target / f"{AWEME_ID}_{safe_title(aweme.desc)}.mp4"
            media.write_bytes(b"offline-media-fixture")
            return True, str(media), ""

    monkeypatch.setattr(main, "Downloader", Downloader)
    result = asyncio.run(main.share_download(main.ShareDownloadIn(
        share_text=URL, account_id=share.account_id, download=True, save_thumbnail=False)))
    assert result["ok"]
    item = result["results"][0]
    assert item["history_id"] and item["metadata"]["id"] == AWEME_ID
    media = next(row for row in item["files"] if row["role"] == "media")
    metadata = next(row for row in item["files"] if row["role"] == "metadata")
    assert Path(media["path"]).read_bytes() == b"offline-media-fixture"
    assert json.loads(Path(metadata["path"]).read_text(encoding="utf-8"))["id"] == AWEME_ID
    share.browser_read.assert_awaited_once()


def test_selected_proxy_is_used_when_resolving_short_link(share):
    share.project.cfg.engine.douyin_read_mode = "api"
    share.client.fetch_video_detail.return_value = detail()
    share.client.last_error = ""
    result = asyncio.run(main._douyin_native_share(
        "https://v.douyin.com/TARGET/", account_id=share.account_id,
        output_root=share.project.root, quality="highest", should_download=False,
        save_metadata=False, save_thumbnail=False, proxy="http://proxy.example:8080",
        user_agent="fixture"))
    assert result["ok"]
    assert share.resolve.await_args.kwargs["proxy"] == "http://proxy.example:8080"


def test_browser_read_does_not_silently_switch_account_proxy(share):
    share.project.cfg.engine.douyin_read_mode = "browser"
    with pytest.raises(ShareDownloadError, match="账号已绑定的代理"):
        asyncio.run(main._douyin_native_share(
            URL, account_id=share.account_id, output_root=share.project.root,
            quality="highest", should_download=False, save_metadata=False,
            save_thumbnail=False, proxy="http://proxy.example:8080", user_agent="fixture"))
    share.browser_read.assert_not_awaited()
