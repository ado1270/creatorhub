"""Coverage for adding a quality-ranked work straight into comment monitoring."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import select

from app import db, main
from app.config import Config
from app.models import CommentWatch, KeywordCollectionContent, MonitorTarget


class QualityWatchApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "quality-watch.db"))
        self.cfg = Config()
        main.cfg = self.cfg

    def tearDown(self):
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _add_content(self, aweme_id="aw-1", desc="二手车出售"):
        with db.get_session() as session:
            session.add(KeywordCollectionContent(
                job_id=1, platform="douyin", aweme_id=aweme_id, desc=desc,
                keyword="二手汽车"))
            session.commit()

    def _watch(self, **overrides):
        payload = dict(platform="douyin", aweme_id="aw-1")
        payload.update(overrides)
        return asyncio.run(
            main.watch_quality_work(main.QualityWatchIn(**payload)))

    def test_quality_work_becomes_a_comment_watch(self):
        self._add_content()
        result = self._watch(intent_keywords=["多少钱"])
        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["aweme_id"], "aw-1")
        self.assertEqual(result["title"], "二手车出售")
        self.assertEqual(result["intent_keywords"], ["多少钱"])
        self.assertTrue(result["enabled"])
        with db.get_session() as session:
            watches = session.exec(select(CommentWatch)).all()
            self.assertEqual(len(watches), 1)

    def test_duplicate_work_is_rejected(self):
        self._add_content()
        self._watch()
        with self.assertRaises(HTTPException) as caught:
            self._watch()
        self.assertEqual(caught.exception.status_code, 409)

    def test_monitor_slot_limit_applies(self):
        self._add_content()
        self.cfg.plan.active_monitor_slots = 0
        with self.assertRaises(HTTPException) as caught:
            self._watch()
        self.assertEqual(caught.exception.status_code, 409)

    def test_plan_minimum_interval_is_enforced(self):
        self._add_content()
        self.cfg.plan.min_monitor_interval_seconds = 3600
        with self.assertRaises(HTTPException) as caught:
            self._watch(interval_seconds=300)
        self.assertEqual(caught.exception.status_code, 409)

    def test_paused_monitors_do_not_consume_the_slot(self):
        self._add_content()
        self.cfg.plan.active_monitor_slots = 1
        with db.get_session() as session:
            session.add(MonitorTarget(platform="douyin", target_kind="creator",
                                      sec_uid="sec-1", enabled=False))
            session.commit()
        self._watch()


if __name__ == "__main__":
    unittest.main()
