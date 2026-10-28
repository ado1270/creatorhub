"""推送失败不能丢线索：失败必须落库，修好后能补推。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlmodel import select

from app import db, lead_push


class _Response:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class LeadPushOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "outbox.db"))
        self.settings = {
            "enabled": True,
            "url": "https://www.gdb15.com/api/v1/leads/webhook",
            "api_key": "lk_test",
        }

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _comments(self):
        return [{"text": "这车多少钱", "comment_id": "cid-1",
                 "create_time": 1_700_000_000}]

    async def test_failed_push_is_stored_in_outbox(self):
        async def fake_post(self, url, json=None, headers=None):
            return _Response(401, '{"message":"Invalid API key"}')

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=fake_post):
            result = await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        self.assertEqual(result["pushed"], 0)
        self.assertEqual(result["queued"], 1)
        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 1)
        self.assertIn("401", stats["items"][0]["last_error"])

    async def test_retry_after_key_fixed_delivers_and_clears(self):
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        sent = []

        async def good_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response(200)

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=good_post):
            result = await lead_push.retry_outbox(force=True)

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["commentId"], "cid-1")
        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["sent"], 1)

    async def test_retry_respects_backoff_unless_forced(self):
        """后台自动补推要按退避来，别把服务端打爆。"""
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")
            deferred = await lead_push.retry_outbox()

        self.assertEqual(deferred["pushed"], 0)
        self.assertEqual(lead_push.outbox_stats()["pending"], 1)

    async def test_same_comment_is_not_stored_twice(self):
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["items"][0]["attempts"], 2)

    async def test_successful_push_does_not_touch_outbox(self):
        async def good_post(self, url, json=None, headers=None):
            return _Response(200)

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=good_post):
            result = await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(lead_push.outbox_stats()["pending"], 0)

    async def test_push_is_rate_limited(self):
        """一次推太多会把客户服务器的防CC打爆，线索全丢，所以必须限速。"""
        sent = []

        async def good_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response(200)

        comments = [{"text": f"评论{i}", "comment_id": f"cid-rate-{i}",
                     "create_time": 1_700_000_000} for i in range(5)]
        original = lead_push._PUSH_RATE_PER_SECOND
        lead_push._PUSH_RATE_PER_SECOND = 20.0
        started = time.monotonic()
        try:
            with patch.object(lead_push, "lead_push_settings",
                              return_value=self.settings), \
                    patch("httpx.AsyncClient.post", new=good_post):
                await lead_push.push_comment_leads(
                    comments, platform="douyin", item_id="aweme-rate")
        finally:
            lead_push._PUSH_RATE_PER_SECOND = original
        elapsed = time.monotonic() - started

        self.assertEqual(len(sent), 5)
        # 5 条 @20 QPS，理论上至少要 0.2 秒，留点余量断言 0.15 秒以上
        self.assertGreaterEqual(elapsed, 0.15)

    async def test_sent_records_are_cleaned_up(self):
        """已送达的记录只留最近的，别把暂存箱撑爆。"""
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")
        self.assertEqual(lead_push.outbox_stats()["pending"], 1)

        async def good_post(self, url, json=None, headers=None):
            return _Response(200)

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=good_post):
            await lead_push.retry_outbox(force=True)
        self.assertEqual(lead_push.outbox_stats()["sent"], 1)

        removed = lead_push.cleanup_outbox(keep_days=0)
        self.assertEqual(removed, 1)
        self.assertEqual(lead_push.outbox_stats()["sent"], 0)

    async def test_duplicate_at_server_counts_as_delivered(self):
        async def dup(self, url, json=None, headers=None):
            return _Response(409, "duplicate")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=dup):
            result = await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["queued"], 0)

    async def test_missing_api_key_does_not_drop_leads(self):
        """开了推送却还没填 Key，线索也得留着，不能悄悄丢。"""
        with patch.object(lead_push, "lead_push_settings",
                          return_value={"enabled": True, "url": "", "api_key": ""}), \
                patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
            result = await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        post.assert_not_called()
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(result["queued"], 1)
        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 1)

    async def test_each_failed_lead_keeps_its_own_reason(self):
        """一批线索失败在不同的原因上，暂存箱里要能分辨出是哪一条。"""
        async def fake_post(self, url, json=None, headers=None):
            if json.get("commentId") == "cid-1":
                return _Response(401, "Invalid API key")
            return _Response(500, "boom")

        comments = [
            {"text": "这车多少钱", "comment_id": "cid-1",
             "create_time": 1_700_000_000},
            {"text": "还在吗", "comment_id": "cid-2",
             "create_time": 1_700_000_001},
        ]
        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=fake_post):
            await lead_push.push_comment_leads(
                comments, platform="douyin", item_id="aweme-1")

        items = {row["comment_id"]: row
                 for row in lead_push.outbox_stats()["items"]}
        self.assertIn("401", items["cid-1"]["last_error"])
        self.assertIn("500", items["cid-2"]["last_error"])

    async def test_corrupt_outbox_row_is_not_counted_as_delivered(self):
        """暂存箱里坏掉的记录不能标成已送达——那等于假装线索送到了。"""
        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=AsyncMock()):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        with lead_push.get_session() as session:
            row = session.exec(
                select(lead_push.LeadPushOutbox)).first()
            row.payload_json = "{不是合法 JSON"
            session.add(row)
            session.commit()

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=AsyncMock()):
            await lead_push.retry_outbox(force=True)

        stats = lead_push.outbox_stats()
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["failed"], 1)

    async def test_retry_stops_when_push_is_disabled(self):
        """客户关了推送开关，后台就不该再偷偷往外发数据。"""
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        off = dict(self.settings, enabled=False)
        sent = []

        async def good_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response(200)

        with patch.object(lead_push, "lead_push_settings", return_value=off), \
                patch("httpx.AsyncClient.post", new=good_post):
            result = await lead_push.retry_outbox(force=True)

        self.assertEqual(result["pushed"], 0)
        self.assertEqual(sent, [])
        self.assertEqual(lead_push.outbox_stats()["pending"], 1)

    async def test_rate_limit_is_shared_across_push_calls(self):
        """采集推送和后台补推共用一个节奏，叠加起来不能超过限速。"""
        sent = []

        async def good_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response(200)

        original = lead_push._PUSH_RATE_PER_SECOND
        lead_push._PUSH_RATE_PER_SECOND = 10.0
        started = time.monotonic()
        try:
            with patch.object(lead_push, "lead_push_settings",
                              return_value=self.settings), \
                    patch("httpx.AsyncClient.post", new=good_post):
                # 两批各 3 条，分两次推送，模拟采集推送和后台补推同时在跑。
                await lead_push.push_comment_leads(
                    [{"text": "a", "comment_id": f"cid-a-{i}",
                      "create_time": 1_700_000_000} for i in range(3)],
                    platform="douyin", item_id="aweme-a")
                await lead_push.push_comment_leads(
                    [{"text": "b", "comment_id": f"cid-b-{i}",
                      "create_time": 1_700_000_000} for i in range(3)],
                    platform="douyin", item_id="aweme-b")
        finally:
            lead_push._PUSH_RATE_PER_SECOND = original
        elapsed = time.monotonic() - started

        self.assertEqual(len(sent), 6)
        # 6 条 @10 QPS 若限速生效，至少要 0.5 秒；留余量断言 0.4 秒以上
        self.assertGreaterEqual(elapsed, 0.4)

    async def test_later_success_clears_the_stale_outbox_row(self):
        """先失败入了箱，后来同一条评论又推成功了，箱里不能继续挂着待推。"""
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")
        self.assertEqual(lead_push.outbox_stats()["pending"], 1)

        async def good_post(self, url, json=None, headers=None):
            return _Response(200)

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=good_post):
            # 下一轮采集重新推这条评论并且成功了：箱里的旧记录要跟着结掉。
            await lead_push.push_comment_leads(
                self._comments(), platform="douyin", item_id="aweme-1")

        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["failed"], 0)

    async def test_gives_up_after_max_attempts_but_stays_visible(self):
        """反复送不出去的线索要停止自动重试，但不能假装它不存在。"""
        async def bad_post(self, url, json=None, headers=None):
            return _Response(401, "Invalid API key")

        with patch.object(lead_push, "lead_push_settings",
                          return_value=self.settings), \
                patch("httpx.AsyncClient.post", new=bad_post):
            for _ in range(lead_push._MAX_OUTBOX_ATTEMPTS + 1):
                await lead_push.push_comment_leads(
                    self._comments(), platform="douyin", item_id="aweme-1")
                await lead_push.retry_outbox(force=True)

        stats = lead_push.outbox_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["items"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
