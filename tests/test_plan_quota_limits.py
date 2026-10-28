"""Regression coverage for monitor-slot accounting and the monthly lead quota."""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app import db, main
from app.config import Config
from app.engine.collection import KeywordCollector
from app.models import (CommentRecord, KeywordCollectionComment,
                        KeywordCollectionContent, KeywordCollectionJob,
                        MonitorTarget)
from app.quota import monthly_lead_usage


class PlanLimitTestBase(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "plan.db"))
        self.cfg = Config()
        main.cfg = self.cfg

    def tearDown(self):
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()


class MonitorSlotTests(PlanLimitTestBase):
    def _add_target(self, enabled: bool = True) -> int:
        with db.get_session() as session:
            target = MonitorTarget(platform="douyin", target_kind="creator",
                                   sec_uid="sec-1", enabled=enabled)
            session.add(target)
            session.commit()
            session.refresh(target)
            return target.id

    def test_new_monitor_beyond_limit_is_rejected(self):
        """Regression: the slot check used to allow one extra monitor."""
        self.cfg.plan.active_monitor_slots = 3
        for _ in range(3):
            self._add_target()
        with db.get_session() as session:
            with self.assertRaises(HTTPException) as caught:
                main._require_monitor_slot(session, adding=True)
            self.assertEqual(caught.exception.status_code, 409)

    def test_slot_is_available_below_limit(self):
        self.cfg.plan.active_monitor_slots = 3
        for _ in range(2):
            self._add_target()
        with db.get_session() as session:
            main._require_monitor_slot(session, adding=True)
            main._require_monitor_slot(session)

    def test_disabled_monitor_does_not_occupy_a_slot(self):
        self.cfg.plan.active_monitor_slots = 1
        self._add_target(enabled=False)
        with db.get_session() as session:
            main._require_monitor_slot(session, adding=True)

    def test_unlimited_plan_never_blocks(self):
        self.cfg.plan.active_monitor_slots = None
        for _ in range(5):
            self._add_target()
        with db.get_session() as session:
            main._require_monitor_slot(session, adding=True)


class PlanIntervalTests(PlanLimitTestBase):
    def test_follow_global_interval_cannot_bypass_the_plan_minimum(self):
        """弹幕监控「跟随全局」曾绕过套餐的最低检查频率。"""
        self.cfg.plan.min_monitor_interval_seconds = 7200
        self.cfg.engine.scan_interval_seconds = 300
        self.assertTrue(
            main._monitor_interval_below_plan(0, allow_follow_global=True))
        error = main._plan_interval_error(follow_global=True)
        self.assertEqual(error.status_code, 409)
        self.assertIn("跟随全局", error.detail)

    def test_follow_global_is_allowed_when_global_meets_the_plan(self):
        self.cfg.plan.min_monitor_interval_seconds = 300
        self.cfg.engine.scan_interval_seconds = 300
        self.assertFalse(
            main._monitor_interval_below_plan(0, allow_follow_global=True))

    def test_explicit_interval_below_the_plan_is_rejected(self):
        self.cfg.plan.min_monitor_interval_seconds = 7200
        self.assertTrue(main._monitor_interval_below_plan(300))


class LeadQuotaTests(PlanLimitTestBase):
    def _add_comment(self, comment_id: str, source: str) -> None:
        with db.get_session() as session:
            if source == "watch":
                session.add(CommentRecord(platform="douyin", aweme_id="aw-1",
                                          comment_id=comment_id, text="多少钱"))
            else:
                session.add(KeywordCollectionComment(
                    job_id=1, content_id=1, platform="douyin", aweme_id="aw-1",
                    comment_id=comment_id, text="多少钱"))
            session.commit()

    def test_quota_counts_keyword_collection_comments(self):
        """Keyword collection used to bypass the monthly lead quota entirely."""
        self._add_comment("c-1", "watch")
        self._add_comment("c-2", "collection")
        with db.get_session() as session:
            self.assertEqual(monthly_lead_usage(session), 2)

    def test_quota_counts_the_same_lead_once(self):
        """Re-running a keyword must not charge the same comment twice."""
        self._add_comment("c-1", "watch")
        self._add_comment("c-1", "collection")
        self._add_comment("c-2", "collection")
        with db.get_session() as session:
            self.assertEqual(monthly_lead_usage(session), 2)

    def test_collection_is_trimmed_to_the_remaining_quota(self):
        self.cfg.plan.monthly_lead_records = 2
        self._add_comment("c-1", "watch")
        collector = KeywordCollector(self.cfg, None, None)
        with db.get_session() as session:
            job = KeywordCollectionJob(platform="douyin", account_id=1)
            content = KeywordCollectionContent(job_id=1, platform="douyin",
                                                aweme_id="aw-2")
            session.add(job)
            session.commit()
            session.add(content)
            session.commit()
            session.refresh(content)
            content_id = content.id
        comments = [{"comment_id": "c-2", "text": "还在吗"},
                    {"comment_id": "c-3", "text": "能便宜点吗"}]
        added = collector._persist_comments(1, content_id, "douyin", "aw-2",
                                            comments)
        self.assertEqual([c["comment_id"] for c in added], ["c-2"])
        with db.get_session() as session:
            self.assertEqual(monthly_lead_usage(session), 2)

    def test_collection_stops_when_quota_is_exhausted(self):
        self.cfg.plan.monthly_lead_records = 1
        self._add_comment("c-1", "watch")
        collector = KeywordCollector(self.cfg, None, None)
        with db.get_session() as session:
            job = KeywordCollectionJob(platform="douyin", account_id=1)
            session.add(job)
            session.commit()
        self.assertEqual(collector._persist_comments(
            1, 1, "douyin", "aw-3", [{"comment_id": "c-9", "text": "在吗"}]), [])


if __name__ == "__main__":
    unittest.main()
