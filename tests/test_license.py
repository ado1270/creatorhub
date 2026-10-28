"""服务端权威套餐（License）回归测试。

核心风险：客户能改本地 config.yaml，也能断网重启。凡是这两种情况下
还能绕过套餐限制的，都算 bug。
"""
import tempfile
import unittest
from pathlib import Path

from app import db, license
from app.settings import set_setting
from app.config import Config, PlanConfig


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    """只实现 license.verify 用到的那部分 httpx 接口。"""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


class LicenseTestBase(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "license.db"))
        self.cfg = Config()
        self.cfg.plan = PlanConfig(
            name="本地", active_monitor_slots=99, max_accounts=99,
            min_monitor_interval_seconds=60, monthly_lead_records=99999,
            max_collection_tasks=99, max_keywords_per_task=99)
        # 开启授权并给一把 Key
        set_setting("license_enabled", "1")
        set_setting("license_api_key", "lk_test")

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()


class LicensePlanTests(LicenseTestBase):
    def _patch_client(self, response=None, error=None):
        original = license.httpx.AsyncClient
        license.httpx.AsyncClient = lambda *a, **k: FakeClient(response, error)
        self.addCleanup(
            lambda: setattr(license.httpx, "AsyncClient", original))

    def test_server_plan_overrides_local_config(self):
        """服务端下发的套餐必须覆盖本地 config.yaml 的值。"""
        self._patch_client(FakeResponse(200, {
            "status": "active",
            "plan": {
                "name": "基础版",
                "active_monitor_slots": 5,
                "max_accounts": 3,
                "min_monitor_interval_seconds": 7200,
                "monthly_lead_records": 1500,
                "max_collection_tasks": 3,
                "max_keywords_per_task": 20,
            },
            "packageExpiresAt": "2026-12-01T00:00:00.000Z",
        }))
        import asyncio
        result = asyncio.run(license.verify(self.cfg))
        self.assertTrue(result["checked"])
        self.assertEqual(self.cfg.plan.name, "基础版")
        self.assertEqual(self.cfg.plan.active_monitor_slots, 5)
        self.assertEqual(self.cfg.plan.max_accounts, 3)
        self.assertEqual(self.cfg.plan.min_monitor_interval_seconds, 7200)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 1500)
        self.assertEqual(self.cfg.plan.max_keywords_per_task, 20)

    def test_cached_plan_applied_when_server_unreachable(self):
        """断网重启也必须继续按上次的套餐限流。"""
        set_setting("license_plan_cache", (
            '{"plan": {"name": "基础版", "active_monitor_slots": 5,'
            ' "max_accounts": 3, "min_monitor_interval_seconds": 7200,'
            ' "monthly_lead_records": 1500, "max_collection_tasks": 3,'
            ' "max_keywords_per_task": 20}, "status": "active",'
            ' "package_expires_at": "2026-12-01T00:00:00.000Z",'
            ' "fetched_at": "2026-10-28T00:00:00+00:00"}'))
        self._patch_client(error=OSError("network down"))
        import asyncio
        result = asyncio.run(license.verify(self.cfg))
        self.assertFalse(result["checked"])
        self.assertTrue(result.get("used_cache"))
        self.assertEqual(self.cfg.plan.active_monitor_slots, 5)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 1500)

    def test_rejected_authorization_locks_plan(self):
        """Key 被拒后必须锁死，不能继续用缓存套餐。"""
        set_setting("license_plan_cache", (
            '{"plan": {"name": "基础版", "active_monitor_slots": 5},'
            ' "status": "active", "fetched_at": "2026-10-28T00:00:00+00:00"}'))
        self._patch_client(FakeResponse(401, {}))
        import asyncio
        result = asyncio.run(license.verify(self.cfg))
        self.assertFalse(result["checked"])
        self.assertTrue(result.get("locked"))
        self.assertEqual(self.cfg.plan.active_monitor_slots, 0)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 0)

    def test_missing_fields_keep_local_values(self):
        """服务端没给某一项时不要把本地值抹成 0。"""
        self._patch_client(FakeResponse(200, {
            "status": "active",
            "plan": {"name": "基础版", "active_monitor_slots": 5},
        }))
        import asyncio
        asyncio.run(license.verify(self.cfg))
        self.assertEqual(self.cfg.plan.active_monitor_slots, 5)
        self.assertEqual(self.cfg.plan.max_accounts, 99)

    def test_machine_code_is_stable(self):
        first = license.machine_code()
        second = license.machine_code()
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_status_reports_server_source(self):
        set_setting("license_plan_cache", (
            '{"plan": {"name": "基础版", "active_monitor_slots": 5},'
            ' "status": "active", "package_expires_at": "2026-12-01T00:00:00.000Z",'
            ' "fetched_at": "2026-10-28T00:00:00+00:00"}'))
        status = license.effective_status(self.cfg)
        self.assertEqual(status["source"], "server")
        self.assertEqual(status["status"], "active")
        self.assertFalse(status["stale"])

    def test_disabled_license_uses_local_config(self):
        set_setting("license_enabled", "0")
        status = license.effective_status(self.cfg)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["source"], "local")


class LicenseEnforcementTests(LicenseTestBase):
    """授权一旦生效，就不能靠「关开关 / 改本地配置 / 断网重启」绕过。"""

    def _patch_client(self, response=None, error=None):
        original = license.httpx.AsyncClient
        license.httpx.AsyncClient = lambda *a, **k: FakeClient(response, error)
        self.addCleanup(
            lambda: setattr(license.httpx, "AsyncClient", original))

    def test_disabling_switch_still_uses_cached_plan(self):
        """把开关关掉，缓存里的套餐照样生效。"""
        set_setting("license_enabled", "0")
        set_setting("license_plan_cache", (
            '{"plan": {"name": "基础版", "active_monitor_slots": 5,'
            ' "max_accounts": 3, "monthly_lead_records": 1500},'
            ' "status": "active",'
            ' "fetched_at": "2026-10-28T00:00:00+00:00"}'))
        import asyncio
        result = asyncio.run(license.verify(self.cfg))
        self.assertFalse(result["checked"])
        self.assertTrue(result.get("enforced"))
        self.assertEqual(self.cfg.plan.active_monitor_slots, 5)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 1500)

    def test_offline_without_cache_fails_closed(self):
        """开了授权却连不上服务端、本机又没有缓存：宁可停工也不能白嫖。"""
        set_setting("license_enabled", "1")
        self._patch_client(error=OSError("network down"))
        import asyncio
        result = asyncio.run(license.verify(self.cfg))
        self.assertFalse(result["checked"])
        self.assertTrue(result.get("locked"))
        self.assertEqual(self.cfg.plan.active_monitor_slots, 0)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 0)
        self.assertEqual(self.cfg.plan.max_accounts, 0)

    def test_rejected_license_stays_locked_after_restart(self):
        """服务端拒绝后要把「已拒绝」写进缓存，重启后仍然是锁死状态。"""
        self._patch_client(FakeResponse(403, {}))
        import asyncio
        asyncio.run(license.verify(self.cfg))

        cache = license.cached_plan()
        self.assertEqual(cache["status"], "inactive")
        self.assertEqual(cache["plan"]["active_monitor_slots"], 0)

        # 模拟重启：本地配置又是无限的
        self.cfg.plan.active_monitor_slots = 99
        self.cfg.plan.monthly_lead_records = 99999
        license.apply_cached_plan(self.cfg)
        self.assertEqual(self.cfg.plan.active_monitor_slots, 0)
        self.assertEqual(self.cfg.plan.monthly_lead_records, 0)

    def test_status_reports_enforced(self):
        set_setting("license_enabled", "0")
        set_setting("license_plan_cache", (
            '{"plan": {"name": "基础版", "active_monitor_slots": 5},'
            ' "status": "active",'
            ' "fetched_at": "2026-10-28T00:00:00+00:00"}'))
        status = license.effective_status(self.cfg)
        self.assertTrue(status["enforced"])
        self.assertEqual(status["source"], "server")

    def test_no_cache_and_disabled_is_not_enforced(self):
        set_setting("license_enabled", "0")
        set_setting("license_plan_cache", "")
        status = license.effective_status(self.cfg)
        self.assertFalse(status["enforced"])
        self.assertEqual(status["source"], "local")
