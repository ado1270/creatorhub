"""填错的 Key 必须还得有退路。

设置页一贯的约定是「留空 = 不修改已保存的 Key」，
好处是不会手滑清空，坏处是填错了就再也清不掉 —— 只能去改本地数据库。
盯两件事：
  1. 留空确实不动已存的 Key（约定不能被这次改动破坏）；
  2. 明确的清除动作能把 Key 删掉，删掉后回到复用线索推送那把 Key。
"""
import asyncio

import httpx
import pytest

import app.main as main
from app import license
from app.settings import get_setting, set_setting
from test_license import FakeClient, FakeResponse
from test_project_optimizations import local_project  # noqa: F401  (fixture)


CARD = "JUFW-HDW6-TCK4-CDD4"
PUSH = "lk_c2a1cddde580d0dc9a4573934c9a1a327703b44ad151399e"
OPEN = "gdb15_-eYTrMte5DY80b8XIMdqNUSRfv4sr6t_"


def put(path, payload):
    async def run():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1") as http:
            return await http.put(path, json=payload,
                                  headers={"Content-Type": "application/json"})
    return asyncio.run(run())


@pytest.fixture
def gdb15_keys(local_project):  # noqa: F811  (local_project 建临时库)
    set_setting("lead_push_api_key", PUSH)
    set_setting("license_url", "https://www.gdb15.com/api/v1/license")
    set_setting("license_api_key", "00000")   # 客户手打的占位，形状拦不住
    set_setting("gdb15_open_api_key", OPEN)
    return local_project


def test_blank_license_key_keeps_the_saved_one(gdb15_keys):
    """留空=不修改，这条老约定不能被改掉。"""
    response = put("/api/settings", {"license_api_key": ""})
    assert response.status_code == 200
    assert get_setting("license_api_key") == "00000"
    assert response.json()["license_api_key_set"] is True


def test_clear_license_key_removes_it(gdb15_keys):
    response = put("/api/settings", {"license_api_key_clear": True})
    assert response.status_code == 200
    assert response.json()["license_api_key_set"] is False
    assert license.license_settings()["api_key"] == ""


def test_cleared_license_key_falls_back_to_push_key(gdb15_keys, monkeypatch):
    """删掉之后不该没授权：这一框本来就是「留空复用线索推送的 Key」。"""
    set_setting("license_enabled", "1")
    put("/api/settings", {"license_api_key_clear": True})
    sent = []
    monkeypatch.setattr(license.httpx, "AsyncClient", lambda *a, **k: FakeClient(
        FakeResponse(200, {"status": "active",
                           "plan": {"name": "基础版",
                                    "active_monitor_slots": 5}}),
        sink=sent))
    asyncio.run(license.verify(gdb15_keys.cfg))
    assert sent, "心跳没发出去"
    assert sent[0]["kwargs"]["headers"]["x-api-key"] == PUSH


def test_card_code_still_cannot_be_saved_into_license_key(gdb15_keys):
    response = put("/api/settings", {"license_api_key": CARD})
    assert response.status_code == 400
    assert "卡密" in response.json()["detail"]
    assert get_setting("license_api_key") == "00000"


def test_open_api_blank_keeps_key_and_clear_removes_it(gdb15_keys):
    put("/api/gdb15/open/settings", {"api_key": ""})
    assert get_setting("gdb15_open_api_key") == OPEN

    response = put("/api/gdb15/open/settings", {"clear_api_key": True})
    assert response.status_code == 200
    assert response.json()["api_key_set"] is False
    assert get_setting("gdb15_open_api_key") == ""


def test_open_api_clear_does_not_touch_the_url(gdb15_keys):
    put("/api/gdb15/open/settings", {"base_url": "https://www.gdb15.com/api/v1/open"})
    put("/api/gdb15/open/settings", {"clear_api_key": True})
    assert get_setting("gdb15_open_api_key") == ""
    assert get_setting("gdb15_open_base_url") == "https://www.gdb15.com/api/v1/open"
