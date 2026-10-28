"""Regression coverage for the quality-work ranking used to pick lead sources."""
import tempfile
import time
import unittest
from pathlib import Path

from app import db
from app.models import (CommentRecord, CommentWatch, ContentRecord,
                         KeywordCollectionComment, KeywordCollectionContent)
from app.quality import rank_works, score_work


class QualityScoreTests(unittest.TestCase):
    def _score(self, comments, **overrides):
        from app.quality import WorkCandidate
        work = WorkCandidate(platform="douyin", aweme_id="aw-1", desc="二手车",
                              like_count=100, comment_count=10, source="monitor")
        params = dict(intent_keywords=["多少钱"], exclude_keywords=["广告"],
                      regions=[], now=int(time.time()),
                      fresh_window_seconds=300)
        params.update(overrides)
        return score_work(work, comments, **params)

    def test_demand_comment_scores_above_plain_comment(self):
        now = int(time.time())
        demand = self._score([{"text": "这个多少钱", "create_time": now,
                               "ip_label": "广东"}])
        plain = self._score([{"text": "沙发真舒服", "create_time": now,
                              "ip_label": "广东"}])
        self.assertEqual(demand.demand_count, 1)
        self.assertEqual(plain.demand_count, 0)
        self.assertGreater(demand.score, plain.score)

    def test_fresh_comments_beat_stale_comments(self):
        now = int(time.time())
        fresh = self._score([{"text": "多少钱", "create_time": now - 60,
                              "ip_label": ""}])
        stale = self._score([{"text": "多少钱", "create_time": now - 6 * 3600,
                              "ip_label": ""}])
        self.assertEqual(fresh.fresh_comments, 1)
        self.assertEqual(stale.fresh_comments, 0)
        self.assertGreater(fresh.score, stale.score)

    def test_region_filter_down_weights_other_regions(self):
        now = int(time.time())
        matched = self._score([{"text": "多少钱", "create_time": now,
                                "ip_label": "广东 东莞"}], regions=["广东"])
        other = self._score([{"text": "多少钱", "create_time": now,
                              "ip_label": "上海"}], regions=["广东"])
        self.assertEqual(matched.region_count, 1)
        self.assertEqual(other.region_count, 0)
        self.assertGreater(matched.score, other.score)

    def test_excluded_words_down_weight_the_work(self):
        now = int(time.time())
        from app.quality import WorkCandidate
        normal = WorkCandidate(platform="douyin", aweme_id="a", desc="二手车",
                                like_count=100)
        ad = WorkCandidate(platform="douyin", aweme_id="b", desc="招聘二手车",
                           like_count=100)
        rows = [{"text": "多少钱", "create_time": now, "ip_label": ""}]
        for work in (normal, ad):
            score_work(work, rows, intent_keywords=["多少钱"],
                       exclude_keywords=["招聘"], regions=[],
                       now=int(time.time()), fresh_window_seconds=300)
        self.assertGreater(normal.score, ad.score)
        self.assertIn("疑似广告", " ".join(ad.reasons))


class QualityRankingTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "quality.db"))
        now = int(time.time())

        with db.get_session() as session:
            session.add(ContentRecord(platform="douyin", target_id=1,
                                      aweme_id="aw-hot", desc="二手车出售",
                                      like_count=800, comment_count=40,
                                      create_time=now - 3600))
            session.add(ContentRecord(platform="douyin", target_id=1,
                                      aweme_id="aw-cold", desc="风景",
                                      like_count=10, comment_count=2,
                                      create_time=now - 3600))
            session.add(ContentRecord(platform="douyin", target_id=1,
                                      aweme_id="aw-nocomments", desc="没有评论",
                                      like_count=500, create_time=now - 3600))
            session.commit()

        with db.get_session() as session:
            session.add(CommentRecord(platform="douyin", watch_id=0,
                                      aweme_id="aw-hot", comment_id="c1",
                                      text="这车还在吗 多少钱", create_time=now - 120,
                                      ip_label="广东 东莞"))
            session.add(CommentRecord(platform="douyin", watch_id=0,
                                      aweme_id="aw-cold", comment_id="c2",
                                      text="沙发真舒服", create_time=now - 120,
                                      ip_label="广东"))
            session.commit()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_demand_work_ranks_first_and_works_without_comments_are_skipped(self):
        ranked = rank_works(platform="douyin", limit=10)
        ids = [row["aweme_id"] for row in ranked]
        self.assertEqual(ids[0], "aw-hot")
        self.assertNotIn("aw-nocomments", ids)

    def test_already_watched_works_are_hidden_by_default(self):
        with db.get_session() as session:
            session.add(CommentWatch(platform="douyin", kind="video",
                                     aweme_id="aw-hot", title="已监控"))
            session.commit()
        ids = [row["aweme_id"] for row in rank_works(platform="douyin", limit=10)]
        self.assertNotIn("aw-hot", ids)

    def test_include_watched_returns_the_work_again(self):
        with db.get_session() as session:
            session.add(CommentWatch(platform="douyin", kind="video",
                                     aweme_id="aw-hot", title="已监控"))
            session.commit()
        rows = rank_works(platform="douyin", limit=10, include_watched=True)
        hot = next(row for row in rows if row["aweme_id"] == "aw-hot")
        self.assertTrue(hot["watched"])

    def test_watched_flag_is_scoped_per_platform(self):
        """抖音的同 ID 作品被监控，不该把小红书的同 ID 作品也藏起来。"""
        now = int(time.time())
        with db.get_session() as session:
            session.add(ContentRecord(platform="xhs", target_id=1,
                                      aweme_id="aw-hot", desc="二手车出售",
                                      like_count=800, comment_count=40,
                                      create_time=now - 3600))
            session.commit()
        with db.get_session() as session:
            session.add(CommentRecord(platform="xhs", watch_id=0,
                                      aweme_id="aw-hot", comment_id="x1",
                                      text="这车还在吗 多少钱",
                                      create_time=now - 120, ip_label="广东"))
            session.commit()
        with db.get_session() as session:
            session.add(CommentWatch(platform="douyin", kind="video",
                                     aweme_id="aw-hot", title="抖音已监控"))
            session.commit()
        rows = rank_works(platform="xhs", limit=10, include_watched=True)
        hot = next(row for row in rows if row["aweme_id"] == "aw-hot")
        self.assertFalse(hot["watched"])

    def test_keyword_collection_works_and_comments_are_included(self):
        now = int(time.time())
        with db.get_session() as session:
            session.add(KeywordCollectionContent(
                job_id=1, platform="douyin", keyword="二手汽车",
                aweme_id="aw-collect", desc="二手汽车低价出",
                like_count=300, comment_count=20, create_time=now - 3600))
            session.add(KeywordCollectionComment(
                job_id=1, content_id=1, platform="douyin", aweme_id="aw-collect",
                comment_id="c9", text="还能便宜点吗 留电话", create_time=now - 60,
                ip_label="广东 深圳"))
            session.commit()
        rows = rank_works(platform="douyin", limit=10)
        collected = next(row for row in rows if row["aweme_id"] == "aw-collect")
        self.assertEqual(collected["source"], "collection")
        self.assertEqual(collected["demand_count"], 1)
        self.assertGreater(collected["score"], 0)


if __name__ == "__main__":
    unittest.main()
