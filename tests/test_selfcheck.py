"""获客链路自检：必须能在没人盯日志的情况下，把静默故障查出来。"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlmodel import select

from app import db, selfcheck
from app.config import Config, PlanConfig
from app.models import CommentRecord, CommentWatch, DouyinAccount
from app.settings import set_setting


class _Checks(dict):
    def by_id(self, cid):
        for item in self["checks"]:
            if item["id"] == cid:
                return item
        raise AssertionError(f"自检项 {cid} 不存在，实际：{[c['id'] for c in self['checks']]}")

    def status(self, cid):
        return self.by_id(cid)["status"]


class SelfCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "selfcheck.db"))
        self.cfg = Config()
        self.push = {"enabled": True,
                     "url": "https://www.gdb15.com/api/v1/leads/webhook",
                     "api_key": "lk_realkey_abcdef123456"}
        self.now = datetime.utcnow()
        with db.get_session() as s:
            s.add(DouyinAccount(platform="douyin", nickname="测试号",
                                status="active"))
            s.add(CommentWatch(platform="douyin", aweme_id="a-1", alias="主监控",
                               enabled=True, interval_seconds=600,
                               last_scan_at=self.now - timedelta(minutes=2)))
            s.add(CommentRecord(platform="douyin", watch_id=1, aweme_id="a-1",
                                comment_id="c-new", text="这车多少钱",
                                create_time=1_700_000_000,
                                created_at=self.now - timedelta(minutes=10)))
            s.commit()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    async def _run(self, *, probe=False, push=None, stats=None):
        with patch.object(selfcheck, "lead_push_settings",
                          return_value=push or self.push), \
                patch.object(selfcheck, "outbox_stats",
                             return_value=stats or {"pending": 0, "sent": 3,
                                                     "failed": 0, "items": []}), \
                patch.object(selfcheck, "probe_lead_push",
                             new=AsyncMock(return_value={"ok": True, "status": 200})):
            raw = await selfcheck.run_selfcheck(self.cfg, probe=probe)
        return _Checks(raw)

    async def test_healthy_chain_passes(self):
        result = await self._run()
        self.assertEqual(result.status("account"), "pass")
        self.assertEqual(result.status("risk"), "pass")
        self.assertEqual(result.status("watch"), "pass")
        self.assertEqual(result.status("push"), "pass")
        self.assertEqual(result.status("outbox"), "pass")
        self.assertEqual(result.status("output"), "pass")
        self.assertEqual(result["summary"]["fail"], 0)

    async def test_placeholder_key_is_reported_as_failure(self):
        """就是这次丢线索的原因：开关开着、Key 是占位符，页面显示一切正常。"""
        for bad in ("", "lk_xxxxxxxxxxxx", "你的API Key", "changeme", "lk_"):
            with self.subTest(key=bad):
                result = await self._run(push=dict(self.push, api_key=bad))
                self.assertEqual(result.status("push"), "fail")

    async def test_disabled_push_is_reported_as_failure(self):
        result = await self._run(push=dict(self.push, enabled=False))
        self.assertEqual(result.status("push"), "fail")
        self.assertIn("启用", result.by_id("push")["fix"])

    async def test_backlog_in_outbox_is_a_failure_not_a_pass(self):
        """采到了但没送进 gdb15，就是获客失败，不能报「正常」。"""
        result = await self._run(stats={
            "pending": 64, "sent": 0, "failed": 0,
            "items": [{"platform": "douyin", "comment_id": "c-1", "attempts": 3,
                       "last_error": "HTTP 401: Invalid API key", "status": "pending",
                       "created_at": ""}]})
        self.assertEqual(result.status("outbox"), "fail")
        self.assertIn("64", result.by_id("outbox")["detail"])
        self.assertIn("401", result.by_id("outbox")["detail"])

    async def test_given_up_leads_stay_visible(self):
        result = await self._run(stats={"pending": 0, "sent": 0, "failed": 5,
                                         "items": []})
        self.assertEqual(result.status("failed"), "warn")

    async def test_no_comment_watch_is_flagged(self):
        with db.get_session() as s:
            for row in s.exec(select(CommentWatch)).all():
                row.enabled = False
                s.add(row)
            s.commit()
        result = await self._run()
        self.assertEqual(result.status("watch"), "warn")

    async def test_stalled_watch_is_flagged(self):
        """监控位开着但早已不在扫，客户看到的数据会是陈的。"""
        with db.get_session() as s:
            row = s.exec(select(CommentWatch)).first()
            row.last_scan_at = self.now - timedelta(days=2)
            s.add(row)
            s.commit()
        result = await self._run()
        self.assertEqual(result.status("watch"), "warn")

    async def test_watch_error_is_surfaced(self):
        with db.get_session() as s:
            row = s.exec(select(CommentWatch)).first()
            row.last_error = "触发验证码，已暂停"
            s.add(row)
            s.commit()
        result = await self._run()
        item = result.by_id("watch")
        self.assertEqual(item["status"], "warn")
        self.assertIn("验证码", item["detail"])

    async def test_no_fresh_leads_is_a_failure(self):
        """一天没产出线索，等于没在获客。"""
        with db.get_session() as s:
            for row in s.exec(select(CommentRecord)).all():
                row.created_at = self.now - timedelta(days=3)
                s.add(row)
            s.commit()
        result = await self._run()
        self.assertEqual(result.status("output"), "fail")

    async def test_own_account_comments_do_not_count_as_leads(self):
        """本账号作品的评论留档不算获客产出，否则自检会把断掉的链路报成正常。"""
        self.cfg.plan = PlanConfig(monthly_lead_records=5)
        with db.get_session() as s:
            for row in s.exec(select(CommentRecord)).all():
                s.delete(row)
            for i in range(3):
                s.add(CommentRecord(platform="douyin", watch_id=0,
                                    aweme_id="my-own-work",
                                    comment_id=f"own-{i}", text="收到",
                                    create_time=1_700_000_000,
                                    created_at=self.now - timedelta(minutes=5)))
            s.commit()
        result = await self._run()
        self.assertEqual(result.status("output"), "fail")
        # 额度一条没动：3 条留档既没算进产出，也没算进月线索额度。
        self.assertIn("已用 0/5", result.by_id("quota")["detail"])

    async def test_exhausted_monthly_quota_is_reported(self):
        self.cfg.plan = PlanConfig(monthly_lead_records=1)
        result = await self._run()
        self.assertEqual(result.status("quota"), "fail")

    async def test_monitor_slots_at_limit_is_reported(self):
        self.cfg.plan = PlanConfig(active_monitor_slots=1)
        result = await self._run()
        self.assertEqual(result.status("slots"), "warn")

    async def test_failed_probe_is_reported_with_reason(self):
        """探测到 Key 失效时要说不通原因，不能只说「失败」。"""
        with patch.object(selfcheck, "lead_push_settings",
                          return_value=self.push), \
                patch.object(selfcheck, "outbox_stats",
                             return_value={"pending": 1, "sent": 0, "failed": 0,
                                           "items": []}), \
                patch.object(selfcheck, "probe_lead_push",
                             new=AsyncMock(return_value={
                                 "ok": False, "reason": "invalid_key", "status": 401,
                                 "error": "HTTP 401: Invalid API key"})):
            raw = await selfcheck.run_selfcheck(self.cfg, probe=True)
        result = _Checks(raw)
        self.assertEqual(result.status("probe"), "fail")
        self.assertIn("API Key 无效", result.by_id("probe")["detail"])

    async def test_probe_is_skipped_by_default(self):
        """只是打开页面，不应该往客户的服务器发请求。"""
        with patch.object(selfcheck, "probe_lead_push",
                          new=AsyncMock()) as probe_mock:
            result = await self._run(probe=False)
            self.assertEqual(result.status("probe"), "info")
            probe_mock.assert_not_called()

    async def test_invalid_account_is_a_failure(self):
        with db.get_session() as s:
            for row in s.exec(select(DouyinAccount)).all():
                row.status = "invalid"
                s.add(row)
            s.commit()
        result = await self._run()
        self.assertEqual(result.status("account"), "fail")

    async def test_every_check_has_an_actionable_fix(self):
        """不通过和需要注意的项必须告诉客户怎么做，光报问题没用。"""
        self.cfg.plan = PlanConfig(monthly_lead_records=0, active_monitor_slots=0)
        result = await self._run(stats={"pending": 2, "sent": 0, "failed": 1,
                                         "items": []},
                                 push=dict(self.push, enabled=False))
        for item in result["checks"]:
            if item["status"] in ("fail", "warn"):
                self.assertTrue(item["fix"].strip(),
                                f"{item['id']} 报了问题但没给动作")

    async def test_intent_ai_off_is_info_not_a_problem(self):
        """没开 AI 是一种选择，不是故障，但要说清代价。"""
        result = await self._run()
        self.assertEqual(result.status("intent"), "info")
        self.assertIn("高意向词", result.by_id("intent")["detail"])

    async def test_intent_ai_switch_without_config_is_a_warning(self):
        """开了开关却漏填模型，实际仍在硬筛 —— 这类静默失效必须被看见。"""
        set_setting("intent_ai_enabled", "1")
        result = await self._run()
        self.assertEqual(result.status("intent"), "warn")
        self.assertTrue(result.by_id("intent")["fix"].strip())

    async def test_intent_ai_probe_success_and_failure(self):
        set_setting("intent_ai_enabled", "1")
        set_setting("intent_ai_base_url", "http://localhost:11434/v1")
        set_setting("intent_ai_model", "qwen2.5:1.5b")
        with patch.object(selfcheck.intent_ai, "score",
                          new=AsyncMock(return_value=[{"score": 92, "label": "求购",
                                                       "reason": ""}])):
            result = await self._run(probe=True)
        self.assertEqual(result.status("intent"), "pass")

        with patch.object(selfcheck.intent_ai, "score",
                          new=AsyncMock(side_effect=selfcheck.intent_ai.IntentAIError(
                              "HTTP 401: invalid_api_key"))):
            result = await self._run(probe=True)
        self.assertEqual(result.status("intent"), "fail")
        self.assertIn("401", result.by_id("intent")["detail"])

    async def test_summary_counts_match_items(self):
        result = await self._run()
        counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
        for item in result["checks"]:
            counts[item["status"]] += 1
        for key, value in counts.items():
            self.assertEqual(result["summary"][key], value)
        self.assertEqual(result["summary"]["total"], len(result["checks"]))


if __name__ == "__main__":
    unittest.main()
