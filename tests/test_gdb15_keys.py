"""gdb15 三把 Key / 一张卡密的形状识别。

盯一件事：每一框只拦「填错了那一样」，认不出来的小心放行 ——
鉴权永远是服务端说了算，这里只负责把提示说准。
"""
import unittest

from app import gdb15_keys as keys

CARD = "JUFW-HDW6-TCK4-CDD4"
PUSH = "lk_c2a1cddde580d0dc9a4573934c9a1a327703b44ad151399e"
OPEN = "gdb15_-eYTrMte5DY80b8XIMdqNUSRfv4sr6t_"


class CardCodeTest(unittest.TestCase):
    def test_recognises_card_code_shape(self):
        self.assertTrue(keys.looks_like_card_code(CARD))
        self.assertTrue(keys.looks_like_card_code("hqcj-mxhd-np4r-w4hd"))

    def test_real_keys_are_not_card_codes(self):
        self.assertFalse(keys.looks_like_card_code(PUSH))
        self.assertFalse(keys.looks_like_card_code(OPEN))


class OpenApiFieldTest(unittest.TestCase):
    def test_card_code_is_named(self):
        problem = keys.open_api_key_problem(CARD)
        self.assertIn("卡密", problem)
        self.assertIn("套餐与用量", problem)

    def test_push_key_is_named(self):
        self.assertIn("线索推送", keys.open_api_key_problem(PUSH))

    def test_real_open_key_is_allowed(self):
        self.assertEqual(keys.open_api_key_problem(OPEN), "")

    def test_empty_is_allowed(self):
        """留空是「不修改已保存的 Key」，不是错误。"""
        self.assertEqual(keys.open_api_key_problem(""), "")

    def test_unknown_shape_is_left_to_server(self):
        self.assertEqual(keys.open_api_key_problem("whatever"), "")


class PushFieldTest(unittest.TestCase):
    def test_card_code_is_named(self):
        problem = keys.push_key_problem(CARD)
        self.assertIn("卡密", problem)
        self.assertIn("lk_", problem)

    def test_open_api_key_is_named(self):
        problem = keys.push_key_problem(OPEN)
        self.assertIn("开放 API", problem)
        self.assertIn("lk_", problem)

    def test_real_push_key_is_allowed(self):
        self.assertEqual(keys.push_key_problem(PUSH), "")

    def test_digits_are_left_to_server(self):
        """客户手打的占位（例如 00000）不该被本地拦下 ——
        让服务端回 401，提示里已经有「Key 无效」这条路径。"""
        self.assertEqual(keys.push_key_problem("00000"), "")

    def test_empty_is_allowed(self):
        self.assertEqual(keys.push_key_problem(""), "")


class HintQualityTest(unittest.TestCase):
    def test_every_hint_points_at_where_to_get_the_right_one(self):
        """提示不能只说「不对」，必须给出该去哪儿拿对的那把。"""
        cases = [
            keys.open_api_key_problem(CARD),
            keys.open_api_key_problem(PUSH),
            keys.push_key_problem(CARD),
            keys.push_key_problem(OPEN),
        ]
        for hint in cases:
            self.assertTrue(hint, "不该返回空提示")
            self.assertTrue("gdb15" in hint or "开放 API" in hint or "套餐与用量" in hint,
                            hint)


if __name__ == "__main__":
    unittest.main()
