"""评论监控的拉取深度：按时间窗拉，不把历史评论全搬一遍。

监控位上到几百条时，全量拉评论（30 页 + 每条评论的回复）一轮就是上百次请求。
这里守住两件事：翻页够用就停，时间窗外的评论不拉回复。
"""
import asyncio
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from app import db
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import CommentWatch
from app.platforms.douyin.client import DouyinClient


class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def anon_identity(self):
        return SimpleNamespace()


def _comment(cid, age_seconds, replies=0):
    return {"cid": cid, "create_time": int(time.time()) - age_seconds,
            "reply_comment_total": replies, "text": "x"}


class RecentCommentFetchTests(unittest.TestCase):
    def _client(self, pages, replies=None):
        calls = {"pages": [], "replies": []}

        async def fetch_comments_page(aweme_id, cursor=0, count=20):
            calls["pages"].append(cursor)
            return pages.pop(0) if pages else {"comments": [], "has_more": False}

        async def fetch_replies_page(aweme_id, comment_id, cursor=0, count=20):
            calls["replies"].append(comment_id)
            return (replies or {}).get(comment_id, {"comments": [], "has_more": False})

        stub = SimpleNamespace(fetch_comments_page=fetch_comments_page,
                               fetch_replies_page=fetch_replies_page)
        return stub, calls

    def test_paging_stops_once_a_page_is_older_than_the_window(self):
        pages = [
            {"comments": [_comment("c1", 60)], "has_more": True, "cursor": 1},
            {"comments": [_comment("c2", 7200)], "has_more": True, "cursor": 2},
        ]
        client, calls = self._client(pages)

        out = asyncio.run(DouyinClient.fetch_recent_comments(
            client, "aw-1", max_pages=5, max_age_seconds=3600))

        # 第二页整页过期，就不该再翻第三页。
        self.assertEqual(calls["pages"], [0, 1])
        self.assertEqual([c["cid"] for c in out], ["c1", "c2"])

    def test_replies_are_only_fetched_for_comments_inside_the_window(self):
        pages = [
            {"comments": [_comment("fresh", 60, replies=2),
                          _comment("old", 7200, replies=2)],
             "has_more": False},
        ]
        client, calls = self._client(pages)

        asyncio.run(DouyinClient.fetch_recent_comments(
            client, "aw-1", max_pages=5, max_age_seconds=3600))

        self.assertEqual(calls["replies"], ["fresh"])

    def test_page_depth_is_capped(self):
        pages = [{"comments": [_comment("c1", 10)], "has_more": True, "cursor": 1}
                 for _ in range(10)]
        client, calls = self._client(pages)

        asyncio.run(DouyinClient.fetch_recent_comments(
            client, "aw-1", max_pages=3, max_age_seconds=0))

        self.assertEqual(len(calls["pages"]), 3)


class CommentWatchFetchPathTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "comment-window.db"))
        self.cfg = Config()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_comment_watch_uses_the_windowed_fetch(self):
        with db.get_session() as session:
            watch = CommentWatch(
                platform="douyin", kind="video", aweme_id="aw-1", enabled=True,
                interval_seconds=3600, comment_max_age_minutes=300)
            session.add(watch)
            session.commit()
            session.refresh(watch)
            watch_id = watch.id

        calls = []

        class ClientStub:
            last_error = ""

            @asynccontextmanager
            async def session_scope(self):
                yield self

            async def fetch_recent_comments(self, aweme_id, **kwargs):
                calls.append((aweme_id, kwargs))
                return []

            async def fetch_all_comments(self, _aweme_id):
                raise AssertionError("监控不允许走全量拉取")

        engine = MonitorEngine(self.cfg, _BrowserStub())
        asyncio.run(engine._cw_video(watch_id, SimpleNamespace(), "aw-1", "t",
                                     True, api_client=ClientStub()))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "aw-1")
        self.assertEqual(calls[0][1]["max_age_seconds"], 300 * 60)


if __name__ == "__main__":
    unittest.main()
