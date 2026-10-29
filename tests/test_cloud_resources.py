"""云端授权的代理池 / 账号池（CreatorHub 侧）回归测试。

这一层的价值在于「不越界」，所以盯的是四件事：
  1. 云端撤销的代理，本地停用而不是删除；
  2. 本地已有的代理不被云端覆盖；
  3. 云端指派不会改掉本地已经绑好的账号出口；
  4. 账号登录态永远不上云。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from app import db, resources
from app.models import DouyinAccount, ProxyPool
from app.settings import set_setting
from sqlmodel import select


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeClient:
    """只实现 resources 用到的那部分 httpx 接口。"""

    def __init__(self, response=None, error=None, sink=None, method="get"):
        self._response = response
        self._error = error
        self._sink = sink
        self._method = method

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return await self._call(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._call(*args, **kwargs)

    async def _call(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        if self._sink is not None:
            self._sink.append({"args": args, "kwargs": kwargs})
        return self._response


class ResourceTestBase(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "resources.db"))
        set_setting("license_url", "https://www.gdb15.com/api/v1/license")
        set_setting("license_api_key", "lk_test")

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _patch_client(self, response=None, error=None, sink=None):
        original = resources.httpx.AsyncClient
        resources.httpx.AsyncClient = lambda *a, **k: FakeClient(response, error, sink)
        self.addCleanup(
            lambda: setattr(resources.httpx, "AsyncClient", original))

    def _add_proxy(self, **kwargs):
        with db.get_session() as s:
            row = ProxyPool(url=kwargs.pop("url", "http://1.2.3.4:8080"), **kwargs)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.id

    def _add_account(self, **kwargs):
        kwargs.setdefault("platform", "douyin")
        kwargs.setdefault("sec_uid", "sec-1")
        kwargs.setdefault("douyin_id", "dy-1")
        with db.get_session() as s:
            row = DouyinAccount(**kwargs)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.id


class BundleApplyTests(ResourceTestBase):
    def test_cloud_proxy_is_added_locally(self):
        result = resources.apply_bundle({
            "proxies": [{
                "id": "cloud-1", "label": "住宅-广东-01",
                "url": "http://u:p@1.2.3.4:8080", "status": "ok",
                "country": "CN", "city": "广州",
            }],
            "assignments": [],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["proxies"]["added"], 1)
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].origin, "cloud")
            self.assertEqual(rows[0].remote_id, "cloud-1")
            self.assertEqual(rows[0].label, "住宅-广东-01")
            self.assertEqual(rows[0].url, "http://u:p@1.2.3.4:8080")
            self.assertEqual(rows[0].country, "CN")

    def test_existing_local_proxy_is_adopted_not_overwritten(self):
        """本地已经有这条代理：不覆盖它的地址和备注，只登记云端也授权了。"""
        self._add_proxy(url="http://u:p@1.2.3.4:8080", label="我自己加的", note="别动")
        resources.apply_bundle({
            "proxies": [{"id": "cloud-1", "label": "云端叫这个", "url": "http://u:p@1.2.3.4:8080"}],
            "assignments": [],
        })
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)          # 没有变成两条
            self.assertEqual(rows[0].label, "我自己加的")  # 本地的备注保住了
            self.assertEqual(rows[0].note, "别动")
            self.assertEqual(rows[0].origin, "cloud")
            self.assertEqual(rows[0].remote_id, "cloud-1")

    def test_revoked_cloud_proxy_is_disabled_not_deleted(self):
        self._add_proxy(url="http://u:p@1.2.3.4:8080",
                        origin="cloud", remote_id="cloud-1", enabled=True)
        result = resources.apply_bundle({"proxies": [], "assignments": []})
        self.assertEqual(result["proxies"]["revoked"], 1)
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)       # 行还在
            self.assertFalse(rows[0].enabled)    # 只是停用

    def test_local_only_proxy_is_never_revoked(self):
        """客户自己加的代理，云端没有它，不能被当成「已撤销」停掉。"""
        self._add_proxy(url="http://9.9.9.9:1080", origin="local", enabled=True)
        resources.apply_bundle({"proxies": [], "assignments": []})
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].enabled)

    def test_unusable_cloud_proxy_is_not_added_to_pool(self):
        """地址在本地用不了（没写端口）：不进池子。

        进了池子就可能被「给账号批量分配」选中，账号就悄悄改走本机 IP ——
        出口 IP 漂移是风控最爱盯的信号，宁可少一条也不能悄悄换出口。
        """
        result = resources.apply_bundle({
            "proxies": [{"id": "cloud-1", "url": "http://proxy.example.com"}],
            "assignments": [],
        })
        self.assertEqual(result["proxies"]["invalid"], 1)
        self.assertEqual(result["proxies"]["added"], 0)
        with db.get_session() as s:
            self.assertEqual(len(s.exec(select(ProxyPool)).all()), 0)

    def test_sync_updates_url_of_known_cloud_proxy(self):
        """云端改了代理地址，本地要跟上 —— 云端对云端下发的代理是权威。"""
        self._add_proxy(url="http://old:1@1.1.1.1:8080",
                        origin="cloud", remote_id="cloud-1")
        resources.apply_bundle({
            "proxies": [{"id": "cloud-1", "url": "http://new:2@2.2.2.2:8080"}],
            "assignments": [],
        })
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].url, "http://new:2@2.2.2.2:8080")


class AssignmentTests(ResourceTestBase):
    def _bundle_with_assignment(self):
        return {
            "proxies": [{"id": "cloud-1", "url": "http://u:p@1.2.3.4:8080"}],
            "assignments": [{
                "platform": "douyin", "accountKey": "dy-1",
                "proxyId": "cloud-1", "proxyUrl": "http://u:p@1.2.3.4:8080",
                "proxyHost": "1.2.3.4:8080", "boundProxyHost": "",
            }],
        }

    def test_assignment_applies_to_account_without_proxy(self):
        self._add_account(proxy="")
        result = resources.apply_bundle(self._bundle_with_assignment())
        self.assertEqual(result["assignments"]["applied"], 1)
        with db.get_session() as s:
            rows = s.exec(select(DouyinAccount)).all()
            self.assertEqual(rows[0].proxy, "http://u:p@1.2.3.4:8080")

    def test_assignment_does_not_touch_account_that_already_has_proxy(self):
        """本地已经绑了代理：不能静默改掉。

        出口 IP 是设备指纹的一部分，中途换 IP 等于告诉平台「这个号突然换人了」。
        """
        self._add_account(proxy="http://keep:me@5.5.5.5:1080")
        result = resources.apply_bundle(self._bundle_with_assignment())
        self.assertEqual(result["assignments"]["applied"], 0)
        self.assertEqual(result["assignments"]["skipped"], 1)
        with db.get_session() as s:
            rows = s.exec(select(DouyinAccount)).all()
            self.assertEqual(rows[0].proxy, "http://keep:me@5.5.5.5:1080")

    def test_assignment_with_unusable_proxy_is_not_bound(self):
        """指派的地址本地解析不了：账号不许被绑上去。"""
        self._add_account(proxy="")
        result = resources.apply_bundle({
            "proxies": [],
            "assignments": [{
                "platform": "douyin", "accountKey": "dy-1",
                "proxyId": "cloud-1", "proxyUrl": "http://proxy.example.com",
                "proxyHost": "proxy.example.com:80", "boundProxyHost": "",
            }],
        })
        self.assertEqual(result["assignments"]["applied"], 0)
        with db.get_session() as s:
            rows = s.exec(select(DouyinAccount)).all()
            self.assertEqual(rows[0].proxy, "")

    def test_assignment_for_unknown_account_is_skipped(self):
        """云端台账里有这台机器上还没有的账号：跳过，不能凭空造账号。"""
        result = resources.apply_bundle(self._bundle_with_assignment())
        self.assertEqual(result["assignments"]["applied"], 0)
        self.assertEqual(result["assignments"]["skipped"], 1)
        with db.get_session() as s:
            self.assertEqual(len(s.exec(select(DouyinAccount)).all()), 0)

    def test_assignment_is_idempotent(self):
        """同步两次，第二次不该重复改（第一次已经绑上了）。"""
        self._add_account(proxy="")
        resources.apply_bundle(self._bundle_with_assignment())
        second = resources.apply_bundle(self._bundle_with_assignment())
        self.assertEqual(second["assignments"]["applied"], 0)
        with db.get_session() as s:
            rows = s.exec(select(DouyinAccount)).all()
            self.assertEqual(rows[0].proxy, "http://u:p@1.2.3.4:8080")


class ReportTests(ResourceTestBase):
    def test_report_contains_identity_but_never_login_state(self):
        self._add_account(
            nickname="小王", sec_uid="sec-1", douyin_id="dy-1",
            cookie="sessionid=super-secret-cookie",
            storage_state='{"cookies":[{"name":"sid","value":"secret"}]}',
            proxy="http://u:p@1.2.3.4:8080",
        )
        payload = resources.collect_local_resources()
        text = str(payload)
        self.assertIn("dy-1", text)
        self.assertNotIn("super-secret-cookie", text)
        self.assertNotIn("secret", text.replace("dy-1", ""))
        self.assertNotIn("storage_state", text)
        self.assertNotIn("cookie", text)

    def test_report_includes_bound_proxy_host_without_credentials(self):
        self._add_account(proxy="http://u:p@1.2.3.4:8080")
        payload = resources.collect_local_resources()
        account = payload["accounts"][0]
        self.assertEqual(account["boundProxyHost"], "1.2.3.4:8080")
        self.assertNotIn("u:p", str(payload))

    def test_report_only_includes_cloud_proxies(self):
        self._add_proxy(url="http://1.2.3.4:8080", origin="local")
        self._add_proxy(url="http://5.6.7.8:9090", origin="cloud", remote_id="cloud-1")
        payload = resources.collect_local_resources()
        self.assertEqual(len(payload["proxies"]), 1)
        self.assertEqual(payload["proxies"][0]["remoteId"], "cloud-1")


class AccountKeyTests(unittest.TestCase):
    def test_prefers_human_readable_douyin_id(self):
        account = DouyinAccount(sec_uid="sec-x", douyin_id="dy-123", uid="999")
        self.assertEqual(resources.account_resource_key(account), "dy-123")

    def test_falls_back_to_sec_uid(self):
        account = DouyinAccount(sec_uid="sec-x", douyin_id="", uid="")
        self.assertEqual(resources.account_resource_key(account), "sec-x")

    def test_empty_when_nothing_usable(self):
        account = DouyinAccount(sec_uid="", douyin_id="", uid="")
        self.assertEqual(resources.account_resource_key(account), "")


class SyncFailureTests(ResourceTestBase):
    def test_network_failure_leaves_local_data_untouched(self):
        self._add_proxy(url="http://1.2.3.4:8080", origin="local", enabled=True)
        self._patch_client(error=RuntimeError("连不上"))
        result = asyncio.run(resources.sync_resources())
        self.assertFalse(result["ok"])
        with db.get_session() as s:
            rows = s.exec(select(ProxyPool)).all()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].enabled)

    def test_rejected_key_is_reported_not_raised(self):
        self._patch_client(response=FakeResponse(403, {}))
        result = asyncio.run(resources.sync_resources())
        self.assertFalse(result["ok"])
        self.assertIn("403", result["reason"])


if __name__ == "__main__":
    unittest.main()
