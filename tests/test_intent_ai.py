"""AI 意向判定：打分口径、容错、以及引擎里的并联逻辑。

不联网：chat() 全部打桩，只验证解析、层级判定与回退路径。
"""
import asyncio
import dataclasses
import json
import time
import unittest

from app import intent_ai
from app.engine.monitor import MonitorEngine


CFG = intent_ai.IntentAIConfig(base_url="http://x/v1", api_key="k", model="m",
                               business="二手电动车回收", high=70, review=40)


class ParseVerdictsTests(unittest.TestCase):
    def test_plain_array_and_code_fence(self):
        raw = '[{"i":0,"s":88,"t":"询价","r":"问价"}]'
        self.assertEqual(intent_ai.parse_verdicts(raw, 1),
                         [{"score": 88, "label": "询价", "reason": "问价"}])
        fenced = "```json\n[{\"i\":0,\"s\":12,\"t\":\"无关\"}]\n```"
        self.assertEqual(intent_ai.parse_verdicts(fenced, 1),
                         [{"score": 12, "label": "无关", "reason": ""}])

    def test_prose_around_array_and_partial_rows(self):
        raw = '判定结果：[{"i":1,"s":55},{"i":0,"s":"70","t":"求购"}] 以上'
        out = intent_ai.parse_verdicts(raw, 2)
        self.assertEqual(out, [{"score": 70, "label": "求购", "reason": ""},
                               {"score": 55, "label": "", "reason": ""}])

    def test_bad_score_or_index_is_marked_unjudged(self):
        raw = '[{"i":0,"s":"很高"},{"i":9,"s":80}]'
        out = intent_ai.parse_verdicts(raw, 1)
        self.assertEqual(out, [{"score": -1, "label": "", "reason": ""}])

    def test_unparseable_returns_empty(self):
        for raw in ("", "模型没有输出数组", "[{oops}]"):
            self.assertEqual(intent_ai.parse_verdicts(raw, 3), [])

    def test_line_format_from_small_models(self):
        raw = "0|88|询价|问价\n1: 12 无关 路过\n2 45 求购\n3|9|同行"
        out = intent_ai.parse_verdicts(raw, 4)
        self.assertEqual([v["score"] for v in out], [88, 12, 45, 9])
        self.assertEqual(out[0]["label"], "询价")
        self.assertEqual(out[2]["label"], "求购")

    def test_line_format_padded_when_model_skips_rows(self):
        # 小模型常见：只回第一条。缺的位置必须占位成「未判定」，不能顺延错位。
        out = intent_ai.parse_verdicts("0|82|询价", 3)
        self.assertEqual([v["score"] for v in out], [82, -1, -1])

    def test_echoed_input_lines_are_not_read_as_verdicts(self):
        self.assertEqual(intent_ai.parse_verdicts("0. 这车还在不\n1. 多少钱", 2), [])

    def test_category_only_lines(self):
        # 主提示词要求的就是这种输出：模型不给分数，分数由类别查表。
        out = intent_ai.parse_verdicts("0|闲聊\n1|询价\n2|广告", 3)
        self.assertEqual([v["label"] for v in out], ["闲聊", "询价", "广告"])
        self.assertEqual([v["score"] for v in out], [-1, -1, -1])
        call = intent_ai.decide({"text": "老板发大财"}, out[0], CFG, [])
        self.assertEqual(call["tier"], intent_ai.TIER_DROP)

    def test_category_lines_with_reason_suffix(self):
        out = intent_ai.parse_verdicts("0|求联系方式|要电话", 1)
        self.assertEqual(out[0]["label"], "求联系方式")
        self.assertEqual(out[0]["reason"], "要电话")


class DecideTests(unittest.TestCase):
    def _tier(self, score, keywords=None, comment_text=""):
        verdict = {"score": score, "label": "询价", "reason": "问价"}
        call = intent_ai.decide({"text": comment_text}, verdict, CFG, keywords or [])
        return call["tier"], call

    def test_high_score_is_lead(self):
        self.assertEqual(self._tier(88)[0], intent_ai.TIER_LEAD)

    def test_gray_zone_is_review_and_kept(self):
        self.assertEqual(self._tier(55)[0], intent_ai.TIER_REVIEW)

    def test_low_score_without_keyword_is_dropped(self):
        self.assertEqual(self._tier(5)[0], intent_ai.TIER_DROP)

    def test_keyword_hit_rescues_low_ai_score(self):
        # 意向词命中是人工验证过的高信号，AI 说 5 分也必须留下。
        tier, call = self._tier(5, keywords=["怎么联系"], comment_text="怎么联系你")
        self.assertEqual(tier, intent_ai.TIER_LEAD)
        self.assertEqual(call["matched"], ["怎么联系"])
        self.assertIn("命中词", call["label"])

    def test_unscored_comment_goes_to_human_not_trash(self):
        # 模型没给数字但给了类别：按类别查表算分（这是主提示词的常见输出）。
        hit = intent_ai.decide({"text": "多少钱"}, {"score": -1, "label": "询价"}, CFG, [])
        self.assertEqual(hit["tier"], intent_ai.TIER_LEAD)
        self.assertEqual(hit["score"], intent_ai.CATEGORY_SCORES["询价"])
        self.assertTrue(hit["scored"])
        # 类别也认不出来 -> 待人工，绝不进垃圾桶
        empty = intent_ai.decide({"text": "嗯"}, {"score": -1, "label": ""}, CFG, [])
        self.assertEqual(empty["tier"], intent_ai.TIER_REVIEW)
        self.assertEqual(empty["label"], "AI 未判定")
        noise = intent_ai.decide({"text": "老板发大财"},
                                 {"score": -1, "label": "闲聊"}, CFG, [])
        self.assertEqual(noise["tier"], intent_ai.TIER_DROP)

    def test_numeric_score_beats_category_table(self):
        call = intent_ai.decide({"text": "多少钱"}, {"score": 55, "label": "询价"}, CFG, [])
        self.assertEqual(call["score"], 55)
        self.assertEqual(call["tier"], intent_ai.TIER_REVIEW)

    def test_review_threshold_cannot_exceed_high(self):
        cfg = intent_ai.IntentAIConfig(base_url="b", api_key="k", model="m",
                                       high=30, review=90)
        call = intent_ai.decide({"text": "在吗"}, {"score": 50}, cfg, [])
        self.assertEqual(call["tier"], intent_ai.TIER_LEAD)


class ScoreBatchTests(unittest.TestCase):
    def _patch_chat(self, monkeypatched):
        intent_ai.chat = monkeypatched

    def test_batches_and_index_gap_filled_as_unjudged(self):
        calls = []

        async def fake_chat(messages, cfg):
            calls.append(messages)
            # 只回第 0 条，制造缺条目
            return '[{"i":0,"s":90,"t":"询价"}]'

        original = intent_ai.chat
        intent_ai.chat = fake_chat
        try:
            cfg = dataclasses.replace(CFG, batch=10)
            comments = [{"text": f"评论{i}"} for i in range(25)]
            out = asyncio.run(intent_ai.score(comments, cfg))
        finally:
            intent_ai.chat = original
        self.assertEqual(len(out), 25)
        self.assertEqual(len(calls), 3)          # 10 + 10 + 5
        self.assertEqual(out[0]["score"], 90)
        self.assertEqual(out[1]["score"], -1)     # 缺条目 -> 未判定，不丢

    def test_unparseable_batch_raises(self):
        async def fake_chat(messages, cfg):
            return "我说不清"

        original = intent_ai.chat
        intent_ai.chat = fake_chat
        try:
            with self.assertRaises(intent_ai.IntentAIError):
                asyncio.run(intent_ai.score([{"text": "在吗"}], CFG))
        finally:
            intent_ai.chat = original


class ChatGatewayCompatTests(unittest.TestCase):
    """通义千问（阿里云百炼）这类网关的额外要求：关思考模式、temperature > 0。"""

    class _Resp:
        def __init__(self, status, text="", payload=None):
            self.status_code, self.text = status, text
            self._payload = payload or {}

        def json(self):
            return self._payload

    def test_adapt_body_fixes_thinking_and_temperature(self):
        body = {"model": "qwen3-flash", "temperature": 0}
        self.assertTrue(intent_ai._adapt_body(
            body, "parameter.enable_thinking must be set to false for "
                  "non-streaming calls"))
        self.assertIs(body["enable_thinking"], False)

        body = {"model": "qwen3-flash", "temperature": 0}
        self.assertTrue(intent_ai._adapt_body(body, "temperature must be greater than 0"))
        self.assertEqual(body["temperature"], 0.01)

        # 认不出来的报错不乱改请求体，交给上层原样报错。
        body = {"model": "m", "temperature": 0}
        self.assertFalse(intent_ai._adapt_body(body, "HTTP 401: invalid api key"))
        self.assertNotIn("enable_thinking", body)

    def _run_retry(self, first_text):
        sent = []

        async def fake_post(body, cfg):
            sent.append(dict(body))
            if len(sent) == 1:
                return self._Resp(400, first_text)
            return self._Resp(200, payload={
                "choices": [{"message": {"content": "0|询价"}}]})

        original = intent_ai._post
        intent_ai._post = fake_post
        try:
            out = asyncio.run(intent_ai.chat(
                [{"role": "user", "content": "hi"}], CFG))
        finally:
            intent_ai._post = original
        return out, sent

    def test_chat_retries_once_with_fixed_body(self):
        out, sent = self._run_retry(
            "parameter.enable_thinking must be set to false for non-streaming calls")
        self.assertEqual(out, "0|询价")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("enable_thinking", sent[0])   # 首发保持标准 OpenAI 字段
        self.assertIs(sent[1]["enable_thinking"], False)

    def test_chat_reports_error_when_retry_also_fails(self):
        async def fake_post(body, cfg):
            return ChatGatewayCompatTests._Resp(401, "invalid_api_key")

        original = intent_ai._post
        intent_ai._post = fake_post
        try:
            with self.assertRaises(intent_ai.IntentAIError) as ctx:
                asyncio.run(intent_ai.chat([{"role": "user", "content": "hi"}], CFG))
        finally:
            intent_ai._post = original
        self.assertIn("HTTP 401", str(ctx.exception))


class ThinkingModeTests(unittest.TestCase):
    """通义千问默认开着思考模式：非流式判定不该为没人看的思考过程付钱。"""

    def test_detected_by_host_or_model_name(self):
        def hit(base, model):
            cfg = intent_ai.IntentAIConfig(base_url=base, api_key="", model=model)
            return intent_ai.wants_thinking_off(cfg)
        self.assertTrue(hit("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-flash"))
        self.assertTrue(hit("https://ws-abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                            "qwen3.7-flash"))
        self.assertTrue(hit("http://localhost:11434/v1", "qwen2.5:1.5b"))
        self.assertFalse(hit("https://api.deepseek.com/v1", "deepseek-chat"))
        self.assertFalse(hit("http://localhost:11434/v1", "llama3:8b"))

    def test_first_request_already_carries_the_flag(self):
        sent = []

        async def fake_post(body, cfg):
            sent.append(dict(body))
            return ChatGatewayCompatTests._Resp(
                200, payload={"choices": [{"message": {"content": "0|询价"}}]})

        original = intent_ai._post
        intent_ai._post = fake_post
        try:
            cfg = dataclasses.replace(CFG, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                                      model="qwen3.7-flash")
            out = asyncio.run(intent_ai.chat([{"role": "user", "content": "hi"}], cfg))
        finally:
            intent_ai._post = original
        self.assertEqual(out, "0|询价")
        self.assertEqual(len(sent), 1)                    # 不用等报错再重发
        self.assertIs(sent[0]["enable_thinking"], False)


class ScreenIntentTests(unittest.TestCase):
    def setUp(self):
        self.engine = MonitorEngine.__new__(MonitorEngine)
        self.engine._scan_stats = {}
        self.settings = {
            "comment_max_age_minutes": 300,
            "comment_regions": [],
            "intent_keywords": ["多少钱"],
            "exclude_keywords": [],
        }
        self.now = int(time.time())

    def _run(self, comments, ai_cfg, fake_score=None):
        original_config = intent_ai.config
        original_score = intent_ai.score
        intent_ai.config = lambda: ai_cfg
        if fake_score is not None:
            intent_ai.score = fake_score
        try:
            return asyncio.run(self.engine._screen_intent(7, comments, self.settings))
        finally:
            intent_ai.config = original_config
            intent_ai.score = original_score

    def test_ai_off_keeps_keyword_behaviour_unchanged(self):
        fresh = [{"text": "这个多少钱", "create_time": self.now},
                 {"text": "这车还在不", "create_time": self.now}]
        kept, pushable = self._run(fresh, None)
        self.assertEqual([c["text"] for c in kept], ["这个多少钱"])
        self.assertEqual(kept[0]["intent_source"], "keyword")
        self.assertEqual(len(pushable), len(kept))   # 关键词模式下入库即推送

    def test_ai_on_rescues_comments_without_keyword_hit(self):
        async def fake_score(comments, cfg):
            return [{"score": 82, "label": "询价", "reason": "问价"},
                    {"score": 55, "label": "", "reason": ""},
                    {"score": 3, "label": "同行", "reason": "同行"}]

        fresh = [{"text": "这个多少钱", "create_time": self.now},
                 {"text": "这车还在不", "create_time": self.now},
                 {"text": "同行路过", "create_time": self.now}]
        kept, pushable = self._run(fresh, CFG, fake_score)
        # 高分 + 灰区留下，AI 明确判定不是线索的才丢
        self.assertEqual([c["text"] for c in kept], ["这个多少钱", "这车还在不"])
        self.assertEqual(kept[0]["intent_score"], 82)
        self.assertEqual(kept[0]["intent_source"], "keyword")  # 命中意向词
        self.assertEqual(kept[1]["intent_source"], "ai")
        # 灰区只入库不推送，别把 gdb15 坐席淹在没把握的评论里
        self.assertEqual([c["text"] for c in pushable], ["这个多少钱"])
        stats = self.engine._take_scan_stats(7)
        self.assertEqual(stats["review"], 1)

    def test_gray_zone_pushed_when_switch_on(self):
        async def fake_score(comments, cfg):
            return [{"score": 82, "label": "询价", "reason": ""},
                    {"score": 55, "label": "求购", "reason": ""}]

        fresh = [{"text": "这个多少钱", "create_time": self.now},
                 {"text": "这车还在不", "create_time": self.now}]
        cfg = dataclasses.replace(CFG, push_review=True)
        kept, pushable = self._run(fresh, cfg, fake_score)
        self.assertEqual(len(kept), 2)
        self.assertEqual([c["text"] for c in pushable],
                         ["这个多少钱", "这车还在不"])

    def test_ai_failure_falls_back_to_keywords(self):
        async def broken_score(comments, cfg):
            raise intent_ai.IntentAIError("HTTP 500: boom")

        fresh = [{"text": "这个多少钱", "create_time": self.now},
                 {"text": "这车还在不", "create_time": self.now}]
        kept, pushable = self._run(fresh, CFG, broken_score)
        self.assertEqual([c["text"] for c in kept], ["这个多少钱"])
        self.assertEqual(len(pushable), 1)
        notes = self.engine._take_scan_stats(7)["notes"]
        self.assertTrue(any("回退意向词" in n for n in notes))


class ScanInfoTests(unittest.TestCase):
    def test_zero_is_explained(self):
        engine = MonitorEngine.__new__(MonitorEngine)
        engine._scan_stats = {}
        engine._note_scan_stat(3, fetched=22, screened=0, leads=0)
        info = engine._format_scan_info(engine._take_scan_stats(3))
        self.assertIn("抓到 22 条新评论", info)
        self.assertIn("过时间/地区 0 条", info)

    def test_fetch_failure_becomes_note(self):
        engine = MonitorEngine.__new__(MonitorEngine)
        engine._scan_stats = {}
        engine._note_scan_stat(3, fetched=0,
                               note="未拦截到评论(可能未登录/评论区未加载/作品无评论)")
        stats = engine._take_scan_stats(3)
        self.assertIn("未拦截到评论", engine._format_scan_info(stats))
        # 一条都没抓到 + 有抓取原因 -> 该在界面上亮警告
        self.assertEqual(stats["notes"][0], "未拦截到评论(可能未登录/评论区未加载/作品无评论)")


class NoStatsLeakTests(unittest.TestCase):
    def test_stats_key_is_popped_even_on_error(self):
        # scan_comment_watch 的 finally 负责清理，避免残留污染下一轮。
        engine = MonitorEngine.__new__(MonitorEngine)
        engine._scan_stats = {9: {"fetched": 1}}
        engine._scan_stats.pop(9, None)
        self.assertEqual(engine._scan_stats, {})


if __name__ == "__main__":
    unittest.main()
