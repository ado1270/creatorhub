"""评论的送达回执与补推出口。

修好推送判据只能保证「以后不丢」。修好之前攒下的漏网线索不会自己回来 ——
它们已经入库，之后每一轮都按 comment_id 被当成老评论跳过。所以库里必须
记得住「这条到底推出去没有」，并且给人工一个明确的补推动作。
"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path


class CommentPushReceiptTests(unittest.TestCase):
    def setUp(self):
        from app import db
        from app.config import Config
        from app.engine.monitor import MonitorEngine
        from app.models import CommentWatch

        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "receipt.db"))
        self.cfg = Config()
        self.engine = MonitorEngine.__new__(MonitorEngine)
        self.engine.cfg = self.cfg
        self.sent = []

        with db.get_session() as session:
            watch = CommentWatch(platform="douyin", kind="video",
                                 aweme_id="aw-1", title="测试作品",
                                 comment_max_age_minutes=0)
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

    def _patch_sender(self, monkey):
        """替换最底层发送函数，保留引擎里的回执逻辑。"""
        from app.engine import monitor as M
        original = M.push_comment_leads

        async def fake(comments, *, platform, item_id, intent_keywords=None,
                       intent_label="", source_keyword=""):
            self.sent.extend(str(c.get("comment_id") or "") for c in comments)
            return {"enabled": True, "pushed": len(comments), "skipped": 0,
                    "errors": [], "queued": 0,
                    "handled": [str(c.get("comment_id") or "") for c in comments]}

        M.push_comment_leads = monkey or fake
        self.addCleanup(setattr, M, "push_comment_leads", original)

    def _ingest(self, comments, first_scan=False):
        async def fake_notify(name, work_desc, items):
            return None
        self.engine._notify_comments = fake_notify
        return asyncio.run(self.engine._ingest(
            self.watch_id, "aw-1", comments, "测试作品", "测试作品", first_scan))

    def _records(self):
        from app import db
        from app.models import CommentRecord
        from sqlmodel import select
        with db.get_session() as session:
            return list(session.exec(select(CommentRecord)).all())

    def test_push_stamps_receipt(self):
        self._patch_sender(None)
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        rows = self._records()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].pushed_at)

    def test_failed_handover_leaves_receipt_empty(self):
        """推送没接住（异常）时不能盖回执，否则补推工具会当它已经送过。"""
        async def boom(*args, **kwargs):
            raise RuntimeError("网络断了")
        self._patch_sender(boom)
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        self.assertIsNone(self._records()[0].pushed_at)

    def test_repush_sends_history_that_was_never_pushed(self):
        self._patch_sender(None)
        now = int(time.time())
        self._ingest([{"comment_id": "c-1", "text": "多少钱",
                       "create_time": now, "ip_label": "广东"}])
        self.assertEqual(self.sent, ["c-1"])
        self.sent.clear()
        # 模拟修复前留下的漏网线索：已入库、从未推出去、而且时间戳比水位线旧。
        from app import db
        from app.models import CommentRecord
        from sqlmodel import select
        with db.get_session() as session:
            session.add(CommentRecord(platform="douyin", watch_id=self.watch_id,
                                      aweme_id="aw-1", comment_id="c-lost",
                                      text="还在吗", create_time=now - 86400,
                                      ip_label="广东"))
            session.commit()
        result = asyncio.run(self.engine.repush_unpushed_leads())
        self.assertEqual(self.sent, ["c-lost"])
        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["remaining_unpushed"], 0)
        by_id = {r.comment_id: r for r in self._records()}
        self.assertIsNotNone(by_id["c-lost"].pushed_at)

    def test_repush_honours_current_filters(self):
        """补推仍按地区与排除词复核：客户改了设置，不该把不要的评论塞进 CRM。"""
        self._patch_sender(None)
        from app import db
        from app.models import CommentRecord, CommentWatch
        from sqlmodel import select
        now = int(time.time())
        with db.get_session() as session:
            w = session.get(CommentWatch, self.watch_id)
            w.comment_regions = '["广东"]'
            session.add(w)
            session.add(CommentRecord(platform="douyin", watch_id=self.watch_id,
                                      aweme_id="aw-1", comment_id="c-hunan",
                                      text="多少钱", create_time=now,
                                      ip_label="湖南"))
            session.commit()
        result = asyncio.run(self.engine.repush_unpushed_leads())
        self.assertEqual(self.sent, [])
        self.assertEqual(result["skipped"], 1)
        self.assertIsNone(self._records()[0].pushed_at)

    def test_repush_is_idempotent(self):
        self._patch_sender(None)
        from app import db
        from app.models import CommentRecord
        now = int(time.time())
        with db.get_session() as session:
            session.add(CommentRecord(platform="douyin", watch_id=self.watch_id,
                                      aweme_id="aw-1", comment_id="c-1",
                                      text="多少钱", create_time=now,
                                      ip_label="广东"))
            session.commit()
        asyncio.run(self.engine.repush_unpushed_leads())
        self.assertEqual(self.sent, ["c-1"])
        self.sent.clear()
        asyncio.run(self.engine.repush_unpushed_leads())
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
