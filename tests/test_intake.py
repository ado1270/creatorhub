"""Coverage for the mobile intake path: phone submits a work, this machine builds the watch."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import select

from app import db, intake, main
from app.config import Config
from app.models import CommentWatch, DouyinAccount


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "intake.db"))
        self.cfg = Config()
        main.cfg = self.cfg

    def tearDown(self):
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _add_account(self, platform="xhs", status="active", storage_state="{}"):
        with db.get_session() as session:
            account = DouyinAccount(
                platform=platform, status=status, storage_state=storage_state)
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    # ── 地址 ──────────────────────────────────────────────────

    def test_site_base_drops_the_api_path(self):
        """授权地址带路径时，不能把路径拼到接口地址后面。"""
        from app.settings import set_setting
        set_setting("license_url", "https://www.gdb15.com/api/v1/license")
        self.assertEqual(intake._site_base(), "https://www.gdb15.com")
        self.assertEqual(intake.intake_base_url(),
                         "https://www.gdb15.com/api/v1/watch-intake")

    def test_site_base_falls_back_to_the_push_address(self):
        from app.settings import set_setting
        set_setting("license_url", "")
        set_setting("lead_push_url", "https://www.gdb15.com/api/v1/leads/webhook")
        self.assertEqual(intake.intake_base_url(),
                         "https://www.gdb15.com/api/v1/watch-intake")

    def test_intake_is_off_without_a_key(self):
        from app.settings import set_setting
        set_setting("license_url", "")
        set_setting("lead_push_url", "")
        set_setting("license_api_key", "")
        set_setting("lead_push_api_key", "")
        self.assertFalse(intake.intake_ready())

    # ── 账号挑选 ──────────────────────────────────────────────

    def test_douyin_needs_no_bound_account(self):
        self.assertIsNone(intake._pick_account_id("douyin"))

    def test_xhs_picks_a_logged_in_account(self):
        account_id = self._add_account("xhs")
        self.assertEqual(intake._pick_account_id("xhs"), account_id)

    def test_works_are_sharded_across_accounts(self):
        """作品在多个账号之间轮流分：单号出事不会带走另一批。"""
        first = self._add_account("douyin")
        second = self._add_account("douyin")
        picked = [intake._pick_account_id("douyin") for _ in range(4)]
        self.assertEqual(picked, [first, second, first, second])

    def test_xhs_without_account_reports_why(self):
        ok, error, _ = asyncio.run(intake._create_watch({
            "id": "i-1", "platform": "xhs", "rawText": "123456789012345",
        }))
        self.assertFalse(ok)
        self.assertIn("小红书", error)

    # ── 建监控 ────────────────────────────────────────────────

    def test_phone_submission_becomes_a_comment_watch(self):
        ok, error, watch = asyncio.run(intake._create_watch({
            "id": "i-1", "platform": "douyin", "rawText": "723456789012345678",
        }))
        self.assertTrue(ok, error)
        self.assertEqual(watch["platform"], "douyin")
        with db.get_session() as session:
            rows = session.exec(select(CommentWatch)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].aweme_id, "723456789012345678")
            self.assertEqual(rows[0].group_name, "手机采集")

    def test_phone_submission_respects_the_plan_interval(self):
        self.cfg.plan.min_monitor_interval_seconds = 7200
        ok, error, watch = asyncio.run(intake._create_watch({
            "id": "i-1", "platform": "douyin", "rawText": "723456789012345678",
        }))
        self.assertTrue(ok, error)
        self.assertEqual(watch["interval_seconds"], 7200)

    def test_monitor_slot_limit_applies_to_phone_submissions(self):
        self.cfg.plan.active_monitor_slots = 0
        ok, error, _ = asyncio.run(intake._create_watch({
            "id": "i-1", "platform": "douyin", "rawText": "723456789012345678",
        }))
        self.assertFalse(ok)
        self.assertIn("监控位", error)

    def test_empty_submission_is_rejected(self):
        ok, error, _ = asyncio.run(intake._create_watch({
            "id": "i-1", "platform": "douyin", "rawText": "   ",
        }))
        self.assertFalse(ok)

    # ── 取件与回报 ────────────────────────────────────────────

    def test_pending_items_are_acknowledged(self):
        item = {"id": "i-1", "platform": "douyin",
                "rawText": "723456789012345678"}
        acks = []

        async def fake_claim(take=5):
            return [item]

        async def fake_ack(item_id, payload):
            acks.append((item_id, payload))

        with mock.patch.object(intake, "_claim", fake_claim), \
                mock.patch.object(intake, "_ack", fake_ack):
            stats = asyncio.run(intake.process_pending())

        self.assertEqual(stats["claimed"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0][0], "i-1")
        self.assertEqual(acks[0][1]["status"], "done")
        self.assertGreater(acks[0][1]["watchId"], 0)

    def test_failed_items_are_reported_back(self):
        item = {"id": "i-2", "platform": "douyin", "rawText": "  "}
        acks = []

        async def fake_claim(take=5):
            return [item]

        async def fake_ack(item_id, payload):
            acks.append((item_id, payload))

        with mock.patch.object(intake, "_claim", fake_claim), \
                mock.patch.object(intake, "_ack", fake_ack):
            stats = asyncio.run(intake.process_pending())

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(acks[0][1]["status"], "failed")
        self.assertTrue(acks[0][1]["error"])


if __name__ == "__main__":
    unittest.main()
