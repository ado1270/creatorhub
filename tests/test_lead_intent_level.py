"""推给 gdb15 的线索必须带上本地判定出的意向等级。

不带的后果：gdb15 只好像它自己的词库重判一遍，本地大模型判成 95 分的
「车在哪」（服务端词库里没这个词）会落成中等意向，而手机端提醒只对
高意向触发 —— 抓到买家和手机响之间就断了这一环。
"""
import unittest
from unittest.mock import patch

from app import lead_push
from app.intent_ai import IntentAIConfig


def _payload(comment):
    payloads = lead_push.build_payloads(
        [comment], platform="douyin", item_id="aw-1")
    return payloads[0] if payloads else {}


class ClientIntentLevelTests(unittest.TestCase):
    AI_OFF = None
    AI_ON = IntentAIConfig(base_url="http://x/v1", api_key="", model="m",
                           high=70, review=40)

    def test_keyword_hit_is_high_even_without_ai(self):
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_OFF):
            payload = _payload({"comment_id": "c-1", "text": "多少钱",
                                 "create_time": 1, "intent_source": "keyword",
                                 "intent_score": -1})
        self.assertEqual(payload["intentLevel"], "HIGH")

    def test_ai_high_score_is_high(self):
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_ON):
            payload = _payload({"comment_id": "c-1", "text": "车在哪",
                                 "create_time": 1, "intent_source": "ai",
                                 "intent_score": 95})
        self.assertEqual(payload["intentLevel"], "HIGH")

    def test_ai_review_band_is_medium(self):
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_ON):
            payload = _payload({"comment_id": "c-1", "text": "看看",
                                 "create_time": 1, "intent_source": "ai",
                                 "intent_score": 55})
        self.assertEqual(payload["intentLevel"], "MEDIUM")

    def test_unjudged_comment_leaves_the_call_to_gdb15(self):
        """AI 没开又没撞意向词：不替服务端下结论，字段干脆不带。"""
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_OFF):
            payload = _payload({"comment_id": "c-1", "text": "路过看看",
                                 "create_time": 1, "intent_source": "",
                                 "intent_score": -1})
        self.assertNotIn("intentLevel", payload)

    def test_ai_down_is_low_medium_boundary(self):
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_ON):
            payload = _payload({"comment_id": "c-1", "text": "广告加微",
                                 "create_time": 1, "intent_source": "ai",
                                 "intent_score": 10})
        self.assertNotIn("intentLevel", payload)

    def test_broken_score_does_not_raise(self):
        with patch.object(lead_push.intent_ai, "config", return_value=self.AI_ON):
            payload = _payload({"comment_id": "c-1", "text": "多少钱",
                                 "create_time": 1, "intent_source": "ai",
                                 "intent_score": "不是数字"})
        self.assertNotIn("intentLevel", payload)


if __name__ == "__main__":
    unittest.main()
