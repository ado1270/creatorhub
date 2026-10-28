"""Coverage for the gdb15 lead push payload."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import asyncio

from app import lead_push
from app.engine.collection import KeywordCollector


class _Response:
    status_code = 200
    text = "ok"


class LeadPushPayloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 推送成功的收尾会更新本地暂存箱的送达回执，需要库已初始化。
        from app import db
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "payload.db"))

    def tearDown(self):
        from app import db
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    async def test_payload_carries_source_comment_and_public_author(self):
        sent = []

        async def fake_post(self, url, json=None, headers=None):
            sent.append({"url": url, "json": json, "headers": headers})
            return _Response()

        comments = [{
            "text": "这车还在吗 多少钱",
            "comment_id": "cid-1",
            "user_nickname": "张三",
            "user_sec_uid": "MS4wLjABAAAA",
            "create_time": 1_700_000_000,
        }]
        with patch.object(lead_push, "lead_push_settings", return_value={
            "enabled": True,
            "url": "https://www.gdb15.com/api/v1/leads/webhook",
            "api_key": "lk_test",
        }), patch("httpx.AsyncClient.post", new=fake_post):
            result = await lead_push.push_comment_leads(
                comments, platform="douyin", item_id="aweme-1")

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(sent), 1)
        payload = sent[0]["json"]
        self.assertEqual(payload["sourceUrl"],
                         "https://www.douyin.com/video/aweme-1")
        self.assertEqual(payload["commentId"], "cid-1")
        self.assertEqual(payload["commentText"], "这车还在吗 多少钱")
        self.assertEqual(payload["source"], "CREATORHUB")
        self.assertEqual(payload["externalUserId"], "MS4wLjABAAAA")
        self.assertEqual(payload["nickname"], "张三")
        self.assertEqual(sent[0]["headers"]["x-api-key"], "lk_test")

    async def test_intent_label_overrides_matched_terms(self):
        sent = []

        async def fake_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response()

        comments = [{"text": "二手汽车 有货吗", "comment_id": "cid-2",
                     "create_time": 1_700_000_000}]
        with patch.object(lead_push, "lead_push_settings", return_value={
            "enabled": True,
            "url": "https://www.gdb15.com/api/v1/leads/webhook",
            "api_key": "lk_test",
        }), patch("httpx.AsyncClient.post", new=fake_post):
            await lead_push.push_comment_leads(
                comments, platform="douyin", item_id="aweme-2",
                intent_keywords=["二手车"], intent_label="二手汽车")
        self.assertEqual(sent[0]["intentKeyword"], "二手汽车")

    async def test_source_keyword_is_sent_for_attribution(self):
        sent = []

        async def fake_post(self, url, json=None, headers=None):
            sent.append(json)
            return _Response()

        comments = [{"text": "在吗", "comment_id": "cid-9",
                     "create_time": 1_700_000_000}]
        with patch.object(lead_push, "lead_push_settings", return_value={
            "enabled": True,
            "url": "https://www.gdb15.com/api/v1/leads/webhook",
            "api_key": "lk_test",
        }), patch("httpx.AsyncClient.post", new=fake_post):
            await lead_push.push_comment_leads(
                comments, platform="douyin", item_id="aweme-9",
                source_keyword="二手汽车")
        self.assertEqual(sent[0]["sourceKeyword"], "二手汽车")

    async def test_disabled_push_sends_nothing(self):
        with patch.object(lead_push, "lead_push_settings", return_value={
            "enabled": False, "url": "", "api_key": "",
        }), patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
            result = await lead_push.push_comment_leads(
                [{"text": "在吗", "comment_id": "cid-3"}],
                platform="douyin", item_id="aweme-3")
        self.assertEqual(result["pushed"], 0)
        self.assertFalse(result["enabled"])
        post.assert_not_called()


    async def test_pushes_stay_concurrent_when_server_is_slow(self):
        """One keyword collection can yield hundreds of comments.

        推送有全局限速（别把服务商的防 CC 打爆），但限速管的是「每秒发几个」，
        不是「一次只能一个在飞」——服务端慢的时候要能并发等着。
        """
        inflight = 0
        peak = 0

        async def fake_post(self, url, json=None, headers=None):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.4)
            inflight -= 1
            return _Response()

        comments = [{"text": "多少钱", "comment_id": f"c-{i}",
                     "create_time": 1_700_000_000} for i in range(8)]
        original = lead_push._PUSH_RATE_PER_SECOND
        lead_push._PUSH_RATE_PER_SECOND = 20.0
        try:
            with patch.object(lead_push, "lead_push_settings", return_value={
                "enabled": True,
                "url": "https://www.gdb15.com/api/v1/leads/webhook",
                "api_key": "lk_test",
            }), patch("httpx.AsyncClient.post", new=fake_post):
                result = await lead_push.push_comment_leads(
                    comments, platform="douyin", item_id="aweme-1")
        finally:
            lead_push._PUSH_RATE_PER_SECOND = original
        self.assertEqual(result["pushed"], 8)
        self.assertGreater(peak, 1)


class CollectionPushIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_push_keeps_keyword_intact(self):
        """关键词采集推送的 intentKeyword 必须是完整词，不能被拆成单字。"""
        seen = {}

        async def fake_push(comments, *, platform, item_id, intent_label="",
                            **kwargs):
            seen["intent_label"] = intent_label
            return {"enabled": True, "pushed": len(comments),
                    "skipped": 0, "errors": []}

        collector = KeywordCollector(None, None, None)
        with patch("app.engine.collection.push_comment_leads", new=fake_push):
            await collector._push_leads(
                "douyin", "aweme-1",
                [{"text": "在吗", "comment_id": "c-1"}],
                "二手汽车")
        self.assertEqual(seen["intent_label"], "二手汽车")


if __name__ == "__main__":
    unittest.main()
