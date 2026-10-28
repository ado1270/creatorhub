"""Offline regressions for scheduled authentication checks (not public profiles)."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.db as db
import app.main as main
import app.engine.monitor as monitor
from app.browser.identity import Identity
from app.browser.fetcher import fetch_self_profile
from app.browser.douyin_auth import douyin_login_prompt_visible
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount
from app.platforms.douyin.client import DouyinClient
from test_project_optimizations import local_project, store


@pytest.fixture
def health(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.engine.quiet_hours_enabled = False
    cfg.engine.verify_proxy_region = False
    cfg.engine.work_health_stat_snapshots = False
    cfg.engine.douyin_profile_mode = "api"
    cfg.engine.douyin_read_mode = "api"
    cfg.risk_control.enabled = False
    monkeypatch.setattr(local_project.browser, "identity_for", lambda a: Identity.from_account(
        a, cfg.engine.profiles_dir, cfg.engine.user_agent), raising=False)
    account_id = store(DouyinAccount(
        platform="douyin", nickname="fixture", status="active", sec_uid="TARGET",
        last_active_at=datetime.utcnow(), storage_state=json.dumps({"cookies": [
            {"name": "sessionid", "value": "TOKEN", "domain": ".douyin.com"}]})))
    engine = MonitorEngine(cfg, local_project.browser)
    engine._last_acct_check = 0
    client = SimpleNamespace(
        last_error="logged_out",
        fetch_self_profile=AsyncMock(return_value=None),
        fetch_profile=AsyncMock(return_value={"sec_uid": "TARGET", "nickname": "public"}))

    @asynccontextmanager
    async def session_scope():
        yield client

    client.session_scope = session_scope
    monkeypatch.setattr(monitor, "DouyinClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(main, "DouyinClient", lambda *_a, **_kw: client)
    return SimpleNamespace(project=local_project, cfg=cfg, engine=engine,
                           account_id=account_id, client=client)


def status(account_id):
    with db.get_session() as session:
        return session.get(DouyinAccount, account_id).status


def probe(health):
    with db.get_session() as session:
        return health.engine._account_probe_tuple(session.get(DouyinAccount, health.account_id))


def test_periodic_auth_check_is_not_delayed_by_recent_public_activity(health):
    assert health.cfg.engine.idle_keepalive_hours == 6
    asyncio.run(health.engine._check_accounts())
    assert status(health.account_id) == "invalid"
    health.client.fetch_self_profile.assert_awaited_once()
    health.client.fetch_profile.assert_not_awaited()


@pytest.mark.parametrize("mode", ["api", "hybrid"])
def test_health_uses_self_endpoint_and_never_overrides_expired_auth_with_public_data(
        health, monkeypatch, mode):
    health.cfg.engine.douyin_profile_mode = mode
    health.cfg.engine.douyin_read_mode = mode
    browser_profile = AsyncMock(return_value=({"sec_uid": "TARGET"}, ""))
    monkeypatch.setattr(monitor, "fetch_self_profile", browser_profile)
    result = asyncio.run(health.engine._probe_account_health(probe(health)))
    assert not result["ok"] and status(health.account_id) == "invalid"
    health.client.fetch_self_profile.assert_awaited_once()
    health.client.fetch_profile.assert_not_awaited()
    browser_profile.assert_not_awaited()


def test_health_honors_the_account_profile_transport(health, monkeypatch):
    health.cfg.engine.douyin_read_mode = "browser"
    browser_profile = AsyncMock(side_effect=AssertionError("profile mode is API-only"))
    monkeypatch.setattr(monitor, "fetch_self_profile", browser_profile)
    asyncio.run(health.engine._probe_account_health(probe(health)))
    health.client.fetch_self_profile.assert_awaited_once()
    browser_profile.assert_not_awaited()


@pytest.mark.parametrize("error", ["empty_body", "http_403", "network:TimeoutError"])
def test_inconclusive_health_check_does_not_change_auth_status(health, error):
    health.client.last_error = error
    result = asyncio.run(health.engine._probe_account_health(probe(health)))
    assert not result["ok"] and status(health.account_id) == "active"
    health.client.fetch_profile.assert_not_awaited()


def test_self_check_does_not_require_a_saved_sec_uid(health):
    with db.get_session() as session:
        account = session.get(DouyinAccount, health.account_id)
        account.sec_uid = ""
        session.add(account)
        session.commit()
    health.client.last_error = ""
    health.client.fetch_self_profile.return_value = {"sec_uid": "TARGET", "nickname": "self"}
    result = asyncio.run(health.engine._probe_account_health(probe(health)))
    assert result["ok"] and status(health.account_id) == "active"
    health.client.fetch_self_profile.assert_awaited_once()


def test_busy_account_does_not_block_other_checks_and_retries_next_tick(health):
    second_id = store(DouyinAccount(platform="douyin", storage_state="{}"))
    calls = []
    original = health.engine._probe_account_health

    async def record(value):
        calls.append(value[0])
        return await original(value)

    health.engine._probe_account_health = record

    async def run():
        lock = health.project.browser.lock_for(f"acc:{health.account_id}")
        await lock.acquire()
        try:
            await asyncio.wait_for(health.engine._check_accounts(), timeout=1)
            assert second_id in calls
            assert status(health.account_id) == "active"
        finally:
            lock.release()
        await health.engine._check_accounts()
        assert status(health.account_id) == "invalid"

    asyncio.run(run())


def test_periodic_interval_and_disable_switch_are_preserved(health):
    health.engine._probe_account_health = AsyncMock(return_value={"ok": True})
    health.cfg.engine.account_check_interval_seconds = 0
    asyncio.run(health.engine._check_accounts())
    health.engine._probe_account_health.assert_not_awaited()
    health.cfg.engine.account_check_interval_seconds = 1800
    asyncio.run(health.engine._check_accounts())
    asyncio.run(health.engine._check_accounts())
    health.engine._probe_account_health.assert_awaited_once()


@pytest.mark.parametrize("mode", ["api", "hybrid"])
def test_manual_refresh_cannot_restore_expired_login_from_public_profile(health, monkeypatch, mode):
    health.cfg.engine.douyin_profile_mode = mode
    browser_profile = AsyncMock(return_value=({"sec_uid": "TARGET"}, ""))
    monkeypatch.setattr(main, "fetch_self_profile", browser_profile)
    result = asyncio.run(main._enrich_account_profile(
        health.account_id, '{"cookies":[{"name":"sessionid","value":"TOKEN"}]}', detailed=True))
    assert result == ("invalid", "logged_out")
    assert status(health.account_id) == "invalid"
    health.client.fetch_profile.assert_not_awaited()
    browser_profile.assert_not_awaited()


@pytest.mark.parametrize("payload", [
    {"status_code": 1, "status_msg": "未登录", "user": {"sec_uid": "TARGET"}},
    {"status_code": 0, "user": {"uid": "0", "nickname": "游客"}},
    {"status_code": 0, "user": {"sec_uid": "TARGET", "guest": True}},
])
def test_self_client_rejects_error_or_guest_profile(payload):
    client = DouyinClient("sessionid=TOKEN", "fixture")
    client._get_json = AsyncMock(return_value=payload)
    assert asyncio.run(client.fetch_self_profile()) is None


def test_self_client_normalizes_http_401_as_auth_loss():
    client = DouyinClient("sessionid=TOKEN", "fixture")
    client._get_json = AsyncMock(return_value=None)
    client.last_error = "http_401"
    assert asyncio.run(client.fetch_self_profile()) is None
    assert client.last_error == "logged_out"


class _Text:
    def __init__(self, text, visible):
        self.text, self.visible = text, visible

    async def is_visible(self, **_kwargs):
        return self.visible


class _Matches:
    def __init__(self, matches):
        self.matches = matches
        self.first = matches[0] if matches else _Text("", False)

    async def all(self):
        return self.matches


class _Page:
    def __init__(self, *, texts=(), payload=None, broken_dom=False, broken_navigation=False):
        self.url = "https://www.douyin.com/jingxuan"
        self.texts = [_Text(*value) for value in texts]
        self.payload = payload
        self.broken_dom = broken_dom
        self.broken_navigation = broken_navigation
        self.handlers = {}
        self.closed = False
        self.context = SimpleNamespace(cookies=AsyncMock(return_value=[
            {"name": "sessionid", "value": "TOKEN"}]))

    def get_by_text(self, text, **_kwargs):
        if self.broken_dom:
            raise RuntimeError("execution context destroyed")
        return _Matches([node for node in self.texts
                         if (text.search(node.text) if hasattr(text, "search") else text == node.text)])

    def on(self, event, callback):
        self.handlers[event] = callback

    async def goto(self, url, **_kwargs):
        if self.broken_navigation and "/user/TARGET" in url:
            raise TimeoutError("navigation failed")
        self.url = url
        if self.payload is not None:
            await self.handlers["response"](SimpleNamespace(
                url="https://www.douyin.com/aweme/v1/web/user/profile/self/",
                status=200, json=AsyncMock(return_value=self.payload)))

    async def wait_for_timeout(self, _timeout):
        pass

    async def evaluate(self, _script, *args):
        return self.payload if args else [{"sec_uid": "TARGET", "nickname": "cached"}]

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("visible_text", ["登录", "登 录", "扫码登录", "验证码登录", "登录后免费畅享高清视频"])
def test_visible_login_prompt_is_detected_after_hidden_duplicate(visible_text):
    page = _Page(texts=[("登录", False), (visible_text, True)])
    assert asyncio.run(douyin_login_prompt_visible(page)) is True
    assert asyncio.run(main._opened_page_login_state(page, "douyin")) == "logged_out"


@pytest.mark.parametrize("page", [_Page(), _Page(broken_dom=True)])
def test_missing_or_unreadable_login_prompt_is_not_proof_of_authentication(page):
    assert asyncio.run(main._opened_page_login_state(page, "douyin")) == "unconfirmed"


@pytest.mark.parametrize("url", ["about:blank", "https://douyin.com.example.invalid/login", "chrome-error://chromewebdata/"])
def test_non_platform_pages_are_not_classified_as_logged_in_or_out(url):
    page = _Page(texts=[("登录", True)])
    page.url = url
    assert asyncio.run(douyin_login_prompt_visible(page)) is None


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/login",
    "https://creator.douyin.com/creator-micro/login",
    "https://passport.douyin.com/",
])
def test_login_redirect_is_detected_even_without_a_rendered_dialog(url):
    page = _Page()
    page.url = url
    assert asyncio.run(douyin_login_prompt_visible(page)) is True


@pytest.mark.parametrize("case,expected_error", [
    ("visible_dialog", "logged_out"),
    ("cached_identity", "auth_unconfirmed"),
    ("broken_dom", "auth_unconfirmed"),
    ("auth_error", "logged_out"),
    ("valid_self", ""),
    ("navigation_error", "navigation failed"),
])
def test_browser_health_requires_current_user_evidence(case, expected_error):
    payload = ({"status_code": 0, "user": {"sec_uid": "TARGET", "nickname": "self"}}
               if case in {"valid_self", "visible_dialog"} else
               {"status_code": 1, "status_msg": "未登录", "user": {"sec_uid": "TARGET"}}
               if case == "auth_error" else None)
    page = _Page(
        texts=[("登录", False), ("扫码登录", case == "visible_dialog")],
        payload=payload, broken_dom=case == "broken_dom",
        broken_navigation=case == "navigation_error")
    manager = SimpleNamespace(new_page=AsyncMock(return_value=page))
    user, error = asyncio.run(fetch_self_profile(manager, SimpleNamespace()))
    assert page.closed
    if case == "valid_self":
        assert user["nickname"] == "self" and not error
    else:
        assert not user and expected_error in error


def test_deferred_checks_retry_without_rechecking_completed_accounts(health):
    health.engine._probe_account_health = AsyncMock(side_effect=[
        {"ok": False, "deferred": True, "reason": "cooldown"}, {"ok": True}])
    asyncio.run(health.engine._check_accounts())
    asyncio.run(health.engine._check_accounts())
    asyncio.run(health.engine._check_accounts())
    assert health.engine._probe_account_health.await_count == 2


def test_new_engine_checks_accounts_on_first_tick(health):
    engine = MonitorEngine(health.cfg, health.project.browser)
    engine._probe_account_health = AsyncMock(return_value={"ok": True})
    asyncio.run(engine._check_accounts())
    engine._probe_account_health.assert_awaited_once()


def test_deleted_or_invalid_accounts_are_removed_from_pending_checks(health):
    with db.get_session() as session:
        account = session.get(DouyinAccount, health.account_id)
        account.status = "invalid"
        session.add(account)
        session.commit()
    health.engine._pending_account_checks.update({health.account_id, 999})
    health.engine._probe_account_health = AsyncMock(return_value={"ok": True})
    asyncio.run(health.engine._check_accounts())
    health.engine._probe_account_health.assert_not_awaited()
    assert not health.engine._pending_account_checks


def test_health_rechecks_account_lock_after_waiting_for_network_slot(health, monkeypatch):
    lock = health.project.browser.lock_for(f"acc:{health.account_id}")

    @asynccontextmanager
    async def network_guard(_account):
        await lock.acquire()  # A manual window won the Profile while we waited.
        try:
            yield
        finally:
            lock.release()

    monkeypatch.setattr(health.engine.risk, "network_guard", network_guard)

    async def run():
        result = await asyncio.wait_for(health.engine._probe_account_health(probe(health)), timeout=1)
        assert result["deferred"] and result["reason"] == "account_busy"
        health.client.fetch_self_profile.assert_not_awaited()

    asyncio.run(run())
