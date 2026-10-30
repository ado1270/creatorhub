"""账号额度按租户总额度下发,客户端不能再加号。

场景：专业版可绑 2 个号，客户跑在 2 台机器上。
如果每台机器各自按 max_accounts 放号，客户能拿到 4 个号，
套餐承诺就被翻倍了。服务端按租户总额度下发余额，客户端必须遵守。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from app import db, license, main
from app.config import Config, PlanConfig
from app.models import DouyinAccount
from app.settings import set_setting


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return FakeResponse(self._payload)


class AccountBudgetTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "budget.db"))
        self.cfg = Config()
        self.cfg.plan = PlanConfig(
            name="专业版", active_monitor_slots=200, max_accounts=2,
            min_monitor_interval_seconds=3600, monthly_lead_records=99999,
            max_collection_tasks=99, max_keywords_per_task=99)
        main.cfg = self.cfg

    def tearDown(self):
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _add_account(self):
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", status="active")
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    def _patch_client(self, payload):
        original = license.httpx.AsyncClient
        license.httpx.AsyncClient = lambda *a, **k: FakeClient(payload)
        self.addCleanup(lambda: setattr(license.httpx, "AsyncClient", original))

    # ── 服务端下发 ────────────────────────────────────────────

    def test_verify_stores_accounts_allowed(self):
        """服务端下发的本机账号余额要落到本地 setting 里。"""
        set_setting("license_enabled", "1")
        set_setting("license_api_key", "lk_test")
        set_setting("license_url", "https://gdb15.example.com/api/v1/license")
        self._patch_client({
            "status": "active",
            "plan": {"name": "专业版", "max_accounts": 2},
            "instance": {"accountsAllowed": 1, "accountsUsedTotal": 1},
        })
        result = asyncio.run(license.verify(self.cfg))
        self.assertEqual(result["accounts_allowed"], 1)
        self.assertEqual(license.account_budget(), 1)

    def test_no_accounts_allowed_means_unlimited(self):
        """服务端没下发这个字段时，不额外限制，交给套餐上限管。"""
        set_setting("license_accounts_allowed", "")
        self.assertIsNone(license.account_budget())

    # ── 客户端遵守额度 ────────────────────────────────────────

    def test_account_slot_respects_server_budget(self):
        """服务端说本机只能放 2 个号，就不能放第 3 个。"""
        set_setting("license_accounts_allowed", "2")
        self._add_account()
        with db.get_session() as session:
            main._require_account_slot(session)
        self._add_account()
        with db.get_session() as session:
            with self.assertRaises(Exception) as ctx:
                main._require_account_slot(session)
        self.assertIn("账号数已满", str(ctx.exception))

    def test_budget_can_be_lower_than_plan(self):
        """服务端给的余额比套餐上限更小时，以服务端为准。"""
        set_setting("license_accounts_allowed", "1")
        with db.get_session() as session:
            main._require_account_slot(session)
        self._add_account()
        with db.get_session() as session:
            with self.assertRaises(Exception) as ctx:
                main._require_account_slot(session)
        self.assertIn("账号数已满", str(ctx.exception))

    def test_account_slot_blocked_when_budget_exhausted(self):
        """其他机器吃满了套餐额度，本机一个号都不能再加。"""
        set_setting("license_accounts_allowed", "0")
        with db.get_session() as session:
            with self.assertRaises(Exception) as ctx:
                main._require_account_slot(session)
        self.assertIn("账号数已满", str(ctx.exception))

    def test_plan_limit_still_applies_without_server_budget(self):
        """服务端没额度时，仍然按本地套餐的 max_accounts 卡。"""
        set_setting("license_accounts_allowed", "")
        self._add_account()
        self._add_account()
        with db.get_session() as session:
            with self.assertRaises(Exception) as ctx:
                main._require_account_slot(session)
        self.assertIn("账号数已满", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
