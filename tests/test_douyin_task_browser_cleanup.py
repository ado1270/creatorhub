"""One-shot browser tasks must not leave the native startup blank window."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser.douyin_detail import fetch_douyin_video_detail
from app.browser.identity import Identity
from app.browser.manager import BrowserManager


WORK_ID = "1234567890123456789"
DETAIL = {"aweme_id": WORK_ID, "video": {"play_addr": {"url_list": ["https://media.example/fixture"]}}}


class Page:
    def __init__(self, context, *, url="about:blank", behavior="success"):
        self.context, self.url, self.behavior = context, url, behavior
        self.closed = False
        self.handlers = {}

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        self.handlers.pop(event, None)

    async def close(self):
        if self.behavior == "close_failure":
            raise RuntimeError("fixture page close failed")
        self.closed = True

    async def goto(self, url, **kwargs):
        if self.behavior == "timeout":
            raise TimeoutError("fixture")
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        self.url = url

    async def wait_for_timeout(self, _milliseconds):
        pass

    async def evaluate(self, _script, _arg):
        if self.behavior == "captcha":
            return {"verification": True}
        if self.behavior == "unavailable":
            return {"unavailable": True}
        return {"item": DETAIL}


class Context:
    def __init__(self, behavior="success"):
        self.behavior = behavior
        self.all_pages = [Page(self)]
        self.closed = False

    @property
    def pages(self):
        return [page for page in self.all_pages if not page.closed]

    async def new_page(self):
        if self.behavior == "new_page_failure":
            raise RuntimeError("fixture new page failed")
        page = Page(self, behavior=self.behavior)
        self.all_pages.append(page)
        return page

    async def close(self):
        self.closed = True
        for page in self.all_pages:
            page.closed = True


def environment(tmp_path, behavior="success"):
    manager = BrowserManager("fixture", profiles_root=str(tmp_path))
    identity = Identity(account_id=13, profile_dir=str(tmp_path / "profile"),
                        platform="douyin", ua="fixture")
    context = Context(behavior)
    manager._launch_persistent = AsyncMock(return_value=context)
    return manager, identity, context


@pytest.mark.parametrize("behavior", [
    "success", "timeout", "captcha", "unavailable", "new_page_failure", "close_failure",
])
def test_reader_reclaims_startup_blank_window_on_success_and_failure(tmp_path, behavior):
    manager, identity, context = environment(tmp_path, behavior)
    result, error = asyncio.run(fetch_douyin_video_detail(manager, identity, WORK_ID))
    assert context.closed and not context.pages
    assert identity.key not in manager._contexts
    assert identity.key not in manager._startup_pages
    assert identity.key not in manager._last_used
    if behavior in {"success", "close_failure"}:
        assert result == DETAIL and not error
    else:
        assert not result and error


def test_cancelled_read_also_reclaims_startup_window(tmp_path):
    manager, identity, context = environment(tmp_path, "cancel")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fetch_douyin_video_detail(manager, identity, WORK_ID))
    assert context.closed and identity.key not in manager._contexts


@pytest.mark.parametrize("other_url", ["https://www.douyin.com/user/self", "about:blank"])
def test_another_page_even_a_new_blank_tab_is_preserved(tmp_path, other_url):
    manager, identity, context = environment(tmp_path)

    async def run():
        await manager.context_for(identity)
        other = await context.new_page()
        other.url = other_url
        assert (await fetch_douyin_video_detail(manager, identity, WORK_ID))[0] == DETAIL
        assert not other.closed
        assert not context.closed
        assert manager._contexts[identity.key] is context

    asyncio.run(run())


def test_initial_blank_navigated_by_user_is_preserved(tmp_path):
    manager, identity, context = environment(tmp_path)

    async def run():
        await manager.context_for(identity)
        context.pages[0].url = "https://www.douyin.com/user/self"
        await fetch_douyin_video_detail(manager, identity, WORK_ID)
        assert not context.closed and not context.pages[0].closed

    asyncio.run(run())


def test_replaced_managed_context_is_never_closed_by_an_old_task(tmp_path):
    manager, identity, context = environment(tmp_path)
    replacement = Context()

    async def run():
        page = await manager.new_page(identity)
        manager._contexts[identity.key] = replacement
        assert await manager.release_task_page(identity, page) is False
        assert page.closed
        assert not replacement.closed and not context.closed

    asyncio.run(run())


def test_manual_unmanaged_context_is_not_closed(tmp_path):
    manager, identity, context = environment(tmp_path)

    async def run():
        task_page = await context.new_page()
        assert await manager.release_task_page(identity, task_page) is False
        assert task_page.closed
        assert not context.closed and not context.pages[0].closed

    asyncio.run(run())


def test_context_without_known_startup_inventory_is_preserved(tmp_path):
    manager, identity, context = environment(tmp_path)

    async def run():
        page = await manager.new_page(identity)
        manager._startup_pages.pop(identity.key)
        assert await manager.release_task_page(identity, page) is False
        assert not context.closed

    asyncio.run(run())


def test_pending_api_session_save_finishes_before_browser_is_closed(tmp_path):
    manager, identity, context = environment(tmp_path)
    order = []

    async def run():
        page = await manager.new_page(identity)

        async def drain():
            order.append(page.closed)

        manager._douyin_api_observers[identity.key] = (
            context, SimpleNamespace(drain_pending=AsyncMock(side_effect=drain)))
        assert await manager.release_task_page(identity, page)
        assert order[0] is False and context.closed
        assert identity.key not in manager._douyin_api_observers

    asyncio.run(run())
