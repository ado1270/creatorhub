"""Coverage for scheduled keyword collection: cadence, risk backoff and plan limits."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import select

from app import db, main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (DouyinAccount, KeywordCollectionJob,
                         KeywordCollectionSchedule)
from app.risk import RiskDecision


class ScheduleTestBase(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "schedule.db"))
        self.cfg = Config()
        main.cfg = self.cfg
        self.enqueued = []
        self.engine = MonitorEngine(self.cfg, None)
        self.engine.enqueue_collection_job = self.enqueued.append

    def tearDown(self):
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, account_id=1):
        with db.get_session() as session:
            session.add(DouyinAccount(
                id=account_id, platform="douyin", nickname="a",
                status="active", storage_state="{}"))
            session.commit()

    def _schedule(self, **overrides):
        payload = dict(platform="douyin", account_id=1,
                       keywords=["二手汽车"], interval_seconds=3600)
        payload.update(overrides)
        with db.get_session() as session:
            row = KeywordCollectionSchedule(
                platform=payload["platform"], account_id=payload["account_id"],
                keywords=json.dumps(payload["keywords"], ensure_ascii=False),
                interval_seconds=payload["interval_seconds"],
                enabled=payload.get("enabled", True),
                next_run_at=datetime.utcnow() - timedelta(seconds=1))
            session.add(row); session.commit(); session.refresh(row)
            return row.id


class ScheduleCreationTests(ScheduleTestBase):
    def test_api_creates_a_schedule_with_the_next_run_time(self):
        self._account()
        result = asyncio.run(main.create_collection_schedule(
            main.CollectionScheduleIn(
                platform="douyin", account_id=1, keywords=["二手汽车"],
                interval_seconds=3600, name="每小时")))
        self.assertEqual(result["keywords"], ["二手汽车"])
        self.assertEqual(result["interval_seconds"], 3600)
        self.assertTrue(result["enabled"])
        self.assertIsNotNone(result["next_run_at"])
        with db.get_session() as session:
            self.assertEqual(
                len(session.exec(select(KeywordCollectionSchedule)).all()), 1)

    def test_plan_limits_the_number_of_scheduled_tasks(self):
        self._account()
        self.cfg.plan.max_collection_tasks = 1
        body = main.CollectionScheduleIn(
            platform="douyin", account_id=1, keywords=["二手汽车"])
        asyncio.run(main.create_collection_schedule(body))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.create_collection_schedule(body))
        self.assertEqual(caught.exception.status_code, 409)

    def test_plan_limits_keywords_per_task(self):
        self._account()
        self.cfg.plan.max_keywords_per_task = 1
        body = main.CollectionScheduleIn(
            platform="douyin", account_id=1, keywords=["二手汽车", "二手车"])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.create_collection_schedule(body))
        self.assertEqual(caught.exception.status_code, 409)


class ScheduleRunTests(ScheduleTestBase):
    def test_due_schedule_creates_a_collection_job(self):
        self._account()
        schedule_id = self._schedule()
        asyncio.run(self.engine._process_collection_schedules())
        self.assertEqual(self.enqueued, [1])
        with db.get_session() as session:
            job = session.exec(select(KeywordCollectionJob)).first()
            self.assertEqual(job.schedule_id, schedule_id)
            self.assertEqual(job.status, "pending")
            self.assertEqual(json.loads(job.keywords), ["二手汽车"])
            schedule = session.get(KeywordCollectionSchedule, schedule_id)
            self.assertEqual(schedule.last_status, "queued")
            self.assertGreater(schedule.next_run_at, datetime.utcnow())

    def test_disabled_schedule_is_skipped(self):
        self._account()
        self._schedule(enabled=False)
        asyncio.run(self.engine._process_collection_schedules())
        self.assertEqual(self.enqueued, [])

    def test_risk_block_backs_off_and_pauses_after_three_blocks(self):
        self._account()
        schedule_id = self._schedule()
        self.engine.risk.preflight = lambda *a, **k: RiskDecision(
            False, "账号读取冷却中", signal="cooldown")
        for _ in range(2):
            asyncio.run(self.engine._process_collection_schedules())
            with db.get_session() as session:
                schedule = session.get(KeywordCollectionSchedule, schedule_id)
                schedule.next_run_at = datetime.utcnow() - timedelta(seconds=1)
                session.add(schedule); session.commit()
        self.assertEqual(self.enqueued, [])
        with db.get_session() as session:
            schedule = session.get(KeywordCollectionSchedule, schedule_id)
            self.assertTrue(schedule.enabled)
            self.assertEqual(schedule.consecutive_blocks, 2)
            self.assertEqual(schedule.last_status, "blocked")
        asyncio.run(self.engine._process_collection_schedules())
        with db.get_session() as session:
            schedule = session.get(KeywordCollectionSchedule, schedule_id)
            self.assertFalse(schedule.enabled)
            self.assertEqual(schedule.consecutive_blocks, 3)
            self.assertIn("自动暂停", schedule.last_error)

    def test_missing_account_records_the_error_and_keeps_the_schedule(self):
        schedule_id = self._schedule()
        asyncio.run(self.engine._process_collection_schedules())
        self.assertEqual(self.enqueued, [])
        with db.get_session() as session:
            schedule = session.get(KeywordCollectionSchedule, schedule_id)
            self.assertTrue(schedule.enabled)
            self.assertEqual(schedule.last_status, "failed")
            self.assertIn("账号", schedule.last_error)


if __name__ == "__main__":
    unittest.main()
