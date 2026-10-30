import asyncio
import json
import time
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from yaml import safe_dump

from app.config import load_config
from app.engine.cadence import row_deadline
from app.engine.monitor import MonitorEngine


class CommentLeadFilterTests(unittest.TestCase):
    def setUp(self):
        self.engine = MonitorEngine.__new__(MonitorEngine)
        self.settings = {
            "comment_max_age_minutes": 300,
            "comment_regions": ["广东"],
            "intent_keywords": ["多少钱"],
            "exclude_keywords": ["广告"],
        }

    def test_fresh_local_intent_comment_is_kept(self):
        now = int(time.time())
        comment = {"create_time": now - 120, "ip_label": "广东 东莞",
                   "text": "这个多少钱"}
        self.assertTrue(
            self.engine._comment_watch_matches(comment, self.settings))

    def test_old_other_region_ad_and_non_intent_are_rejected(self):
        now = int(time.time())
        old = {"create_time": now - 301 * 60, "ip_label": "广东",
               "text": "多少钱"}
        other_region = {"create_time": now - 120, "ip_label": "上海",
                        "text": "多少钱"}
        ad = {"create_time": now - 120, "ip_label": "广东", "text": "广告"}
        non_intent = {"create_time": now - 120, "ip_label": "广东",
                      "text": "支持一下"}
        for comment in (old, other_region, ad, non_intent):
            self.assertFalse(
                self.engine._comment_watch_matches(comment, self.settings))


class PlanConfigTests(unittest.TestCase):
    def test_monitor_slots_load_and_clamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            with path.open("w", encoding="utf-8") as stream:
                safe_dump({"plan": {"name": "basic",
                                    "active_monitor_slots": -2}}, stream)
            cfg = load_config(str(path))
            self.assertEqual(cfg.plan.name, "basic")
            self.assertEqual(cfg.plan.active_monitor_slots, 0)

    def test_plan_minimum_interval_is_enforced_by_scheduler(self):
        now = datetime.utcnow()
        row = type("Row", (), {
            "id": 1,
            "enabled": True,
            "interval_seconds": 60,
            "last_scan_at": now,
            "created_at": now,
        })()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            with path.open("w", encoding="utf-8") as stream:
                safe_dump({"plan": {
                    "min_monitor_interval_seconds": 3600,
                }}, stream)
            cfg = load_config(str(path))
        deadline = row_deadline(row, cfg, kind="monitor")
        self.assertIsNotNone(deadline)
        self.assertGreaterEqual(deadline, now.replace(microsecond=0))


if __name__ == "__main__":
    unittest.main()


class CommentLeadPushTests(unittest.TestCase):
    """评论监控的推送判据：本轮真正新入库的评论就要推给 gdb15。

    两条边界都要守住：
    1. 去重只认 comment_id。以前额外要求「评论时间戳比历史最大时间戳更新」，
       而抖音评论不按时间顺序返回（热度排序、翻页错位、子评论晚一轮抓到），
       比水位线旧的新评论就静默丢了 —— 本地写着「高意向 N 条」，云端一条没有，
       而且因为已入库，之后永远不会补推。生产上真丢过。
    2. 额度用尽时不入库也不推，且同一轮里不会反复推；额度恢复后补推一次，
       因为这条线索客户从来没收到过。
    """

    def setUp(self):
        from app import db
        from app.config import Config
        from app.engine.monitor import MonitorEngine
        from app.models import CommentWatch

        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "watermark.db"))
        self.cfg = Config()
        self.engine = MonitorEngine.__new__(MonitorEngine)
        self.engine.cfg = self.cfg
        self.pushed = []
        self._watch_cls = CommentWatch

        with db.get_session() as session:
            watch = CommentWatch(platform="douyin", kind="video",
                                  aweme_id="aw-1", title="测试作品")
            session.add(watch)
            session.commit()
            session.refresh(watch)
            self.watch_id = watch.id

    def tearDown(self):
        from app import db
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _ingest(self, comments, first_scan=False):
        async def fake_push(platform, aweme_id, items, settings):
            self.pushed.extend(c["comment_id"] for c in items)

        async def fake_notify(name, work_desc, items):
            return None

        self.engine._push_comment_leads = fake_push
        self.engine._notify_comments = fake_notify
        return asyncio.run(self.engine._ingest(
            self.watch_id, "aw-1", comments, "测试作品", "测试作品", first_scan))

    def test_quota_exhausted_comments_are_not_pushed_twice(self):
        self.cfg.plan.monthly_lead_records = 0
        now = int(time.time())
        comments = [{"comment_id": "c-1", "text": "多少钱",
                     "create_time": now, "ip_label": "广东"}]
        self._ingest(comments)
        # 额度用尽，评论不入库，但水位线必须已经前进。
        self.assertEqual(self.pushed, [])
        self._ingest(comments)
        self.assertEqual(self.pushed, [])

    def test_watermark_advances_even_without_stored_comments(self):
        self.cfg.plan.monthly_lead_records = 0
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        from app import db
        from app.models import CommentWatch
        with db.get_session() as session:
            watch = session.get(CommentWatch, self.watch_id)
            marks = json.loads(watch.comment_watermark or "{}")
        self.assertEqual(marks.get("aw-1"), now)

    def test_over_quota_comment_is_pushed_once_quota_recovers(self):
        """额度用尽期间不重复推；额度恢复后补推一次。

        永久作废等于丢单：这条线索客户一条都没收到过。
        """
        self.cfg.plan.monthly_lead_records = 0
        now = int(time.time())
        one = [{"comment_id": "c-1", "text": "多少钱",
                "create_time": now, "ip_label": "广东"}]
        self._ingest(one)
        self._ingest(one)
        self.assertEqual(self.pushed, [])
        self.cfg.plan.monthly_lead_records = None
        self._ingest(one)
        self.assertEqual(self.pushed, ["c-1"])

    def test_new_comment_after_watermark_is_pushed(self):
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        self.assertEqual(self.pushed, ["c-1"])
        self.pushed.clear()
        self._ingest([{"comment_id": "c-2", "text": "还在吗",
                       "create_time": now + 60, "ip_label": "广东"}])
        self.assertEqual(self.pushed, ["c-2"])

    def test_out_of_order_new_comment_is_still_pushed(self):
        """比历史最新时间戳更早的评论，只要本轮才抓到，照样要推。

        这就是生产上丢线索的那条路径：抖音评论不是按时间顺序返回的。
        """
        now = int(time.time())
        self._ingest([{"comment_id": "c-new", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        self.pushed.clear()
        self._ingest([{"comment_id": "c-old", "text": "还在吗",
                       "create_time": now - 3600, "ip_label": "广东"}])
        self.assertEqual(self.pushed, ["c-old"])

    def test_stored_comment_is_never_pushed_twice(self):
        """去重靠 comment_id：已入库的评论再被抓到也不重复推。"""
        now = int(time.time())
        one = [{"comment_id": "c-1", "text": "多少钱",
                "create_time": now, "ip_label": "广东"}]
        self._ingest(one)
        self.pushed.clear()
        self._ingest(one)
        self.assertEqual(self.pushed, [])

    def test_same_comment_twice_in_one_round_pushes_once(self):
        """同一轮里重复的 comment_id 只推一次（调用方漏过滤时的兜底）。"""
        now = int(time.time())
        c = {"comment_id": "c-1", "text": "多少钱",
             "create_time": now, "ip_label": "广东"}
        self._ingest([c, dict(c)])
        self.assertEqual(self.pushed, ["c-1"])

    def test_first_scan_pushes_comments_inside_the_window(self):
        """新建监控位的第一轮也要推 —— 第一批线索正是客户最想要的。"""
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now - 60, "ip_label": "广东"}],
                     first_scan=True)
        self.assertEqual(self.pushed, ["c-1"])

    def test_first_scan_skips_history_outside_the_window(self):
        """首轮超出有效期的老评论只入库不推，免得一次性灌满付费线索池。

        用「有效期=不限」的监控位测：硬筛放行了，仍要由首轮的默认窗口拦住。
        浏览器抓取路径没有 API 那层时间窗，全靠这里兜住。
        """
        from app import db
        from app.models import CommentWatch
        now = int(time.time())
        with db.get_session() as session:
            w = session.get(CommentWatch, self.watch_id)
            w.comment_max_age_minutes = 0
            session.add(w)
            session.commit()
        self._ingest([{"comment_id": "c-ancient", "text": "多少钱",
                       "create_time": now - 3 * 86400, "ip_label": "广东"}],
                     first_scan=True)
        self.assertEqual(self.pushed, [])
        from app import db
        from app.models import CommentRecord
        from sqlmodel import select
        with db.get_session() as session:
            kept = session.exec(select(CommentRecord).where(
                CommentRecord.comment_id == "c-ancient")).all()
        self.assertEqual(len(kept), 1)

    def test_first_scan_honours_the_watch_age_setting(self):
        """监控位自己设了评论有效期时，首轮按设置走，不用默认窗口。"""
        from app import db
        from app.models import CommentWatch
        now = int(time.time())
        with db.get_session() as session:
            w = session.get(CommentWatch, self.watch_id)
            w.comment_max_age_minutes = 60 * 24
            session.add(w)
            session.commit()
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now - 3 * 86400, "ip_label": "广东"}],
                     first_scan=True)
        self.assertEqual(self.pushed, [])
        self._ingest([{"comment_id": "c-2", "text": "还在吗",
                       "create_time": now - 3600, "ip_label": "广东"}],
                     first_scan=True)
        self.assertEqual(self.pushed, ["c-2"])
