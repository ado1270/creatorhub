"""优质作品评分。

线索来自评论，所以判断一个作品值不值得加入评论监控，看的不是点赞，而是
评论区里有没有真实需求：命中意向词的评论有几条、评论是不是刚出现、评论区
还在不在活跃。

评分满分 100，与 ROADMAP_COMMENT_LEAD_SCHEME.md 的线索评分一致：

    需求评论 40 + 评论新鲜度 25 + 区域匹配 15 + 作品热度 10 + 非广告 10

只有拿到评论的作品才能参与评分——评论数据本身就是获客价值的证据。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from sqlmodel import select

from .db import get_session
from .models import (CommentRecord, CommentWatch, ContentRecord,
                     KeywordCollectionComment, KeywordCollectionContent)

# 与 gdb15 的默认意向词保持一致，方便两边判定一致。
DEFAULT_INTENT_KEYWORDS = [
    "多少钱", "报价", "怎么买", "定制", "联系方式", "上门", "还有吗",
    "能发我吗", "求购", "留电话",
]
DEFAULT_EXCLUDE_KEYWORDS = [
    "招聘", "广告", "推广", "代理", "合作", "加V", "私信我",
]

# 默认只看最近 5 小时的新评论，和获客口径一致。
DEFAULT_FRESH_WINDOW_MINUTES = 300
# 参与排名的作品只取近期抓到的，避免老作品长期霸榜。
CANDIDATE_WINDOW_DAYS = 30
MAX_CANDIDATES = 400


def _terms(raw: str | None) -> list[str]:
    text = str(raw or "").replace("，", ",").replace("、", ",")
    return [term.strip() for term in text.split(",") if term.strip()]


def _split_terms(raw: str | None) -> list[str]:
    """Split on commas and newlines; keep multi-word phrases intact."""
    parts: list[str] = []
    for chunk in str(raw or "").replace("，", ",").split(","):
        for line in chunk.split("\n"):
            term = line.strip()
            if term:
                parts.append(term)
    return parts


@dataclass
class WorkCandidate:
    platform: str = ""
    aweme_id: str = ""
    desc: str = ""
    like_count: int = 0
    comment_count: int = 0
    source: str = ""
    keyword: str = ""
    demand_count: int = 0
    fresh_comments: int = 0
    region_count: int = 0
    last_comment_at: int = 0
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    watched: bool = False

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "aweme_id": self.aweme_id,
            "desc": self.desc,
            "like_count": self.like_count,
            "comment_count": self.comment_count,
            "source": self.source,
            "keyword": self.keyword,
            "demand_count": self.demand_count,
            "fresh_comments": self.fresh_comments,
            "region_count": self.region_count,
            "last_comment_at": self.last_comment_at,
            "matched_terms": list(self.matched_terms),
            "reasons": list(self.reasons),
            "score": self.score,
            "watched": self.watched,
        }


def score_work(
    work: WorkCandidate,
    comments: list[dict],
    *,
    intent_keywords: list[str],
    exclude_keywords: list[str],
    regions: list[str],
    now: int,
    fresh_window_seconds: int,
) -> WorkCandidate:
    """Score one work from the comments collected on it."""
    folded_intents = [t.casefold() for t in intent_keywords]
    folded_excludes = [t.casefold() for t in exclude_keywords]
    folded_regions = [r.casefold() for r in regions]

    demand = 0
    fresh = 0
    matched: list[str] = []
    region_hits = 0
    last_at = 0

    for comment in comments:
        created = int(comment.get("create_time") or 0)
        if created:
            last_at = max(last_at, created)
            if now - created <= fresh_window_seconds:
                fresh += 1
        text = str(comment.get("text") or "").casefold()
        if not text:
            continue
        hits = [term for term, folded in zip(intent_keywords, folded_intents)
                if folded and folded in text]
        if not hits:
            continue
        demand += 1
        for term in hits:
            if term not in matched:
                matched.append(term)
        location = str(comment.get("ip_label") or "").casefold()
        if not folded_regions:
            region_hits += 1
        elif location and any(region in location for region in folded_regions):
            region_hits += 1

    work.demand_count = demand
    work.fresh_comments = fresh
    work.region_count = region_hits
    work.last_comment_at = last_at
    work.matched_terms = matched

    demand_score = min(40, demand * 8)
    if last_at:
        age = now - last_at
        if age <= fresh_window_seconds:
            freshness = 25
        elif age <= 24 * 3600:
            freshness = int(25 * (24 * 3600 - age) / (24 * 3600 - fresh_window_seconds))
        else:
            freshness = 0
    else:
        freshness = 0
    freshness = min(25, freshness + min(5, fresh))
    region_score = 15 if not folded_regions else int(15 * region_hits / demand) if demand else 0
    heat = min(10, int(4 * math.log10(work.like_count + 1)))
    desc_text = f"{work.desc} {work.keyword}".casefold()
    ad = any(term and term in desc_text for term in folded_excludes)
    ad_score = 0 if ad else 10

    work.score = demand_score + freshness + region_score + heat + ad_score

    reasons: list[str] = []
    if demand:
        reasons.append(f"{demand} 条高意向评论")
    if matched:
        reasons.append("命中：" + "、".join(matched[:4]))
    if fresh:
        reasons.append(f"{fresh} 条评论在最近 5 小时内")
    if work.like_count:
        reasons.append(f"{work.like_count} 赞")
    if ad:
        reasons.append("疑似广告/同行，已降权")
    if not reasons:
        reasons.append("暂无高意向评论")
    work.reasons = reasons
    return work


def rank_works(
    *,
    platform: str | None = None,
    limit: int = 50,
    intent_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    regions: list[str] | None = None,
    fresh_window_minutes: int = DEFAULT_FRESH_WINDOW_MINUTES,
    include_watched: bool = False,
) -> list[dict]:
    """Rank collected works by how likely they are to yield leads."""
    intents = intent_keywords if intent_keywords is not None else DEFAULT_INTENT_KEYWORDS
    excludes = (exclude_keywords if exclude_keywords is not None
                else DEFAULT_EXCLUDE_KEYWORDS)
    region_terms = regions or []
    now = int(time.time())
    fresh_window = max(60, int(fresh_window_minutes) * 60)
    cutoff = now - CANDIDATE_WINDOW_DAYS * 24 * 3600

    works: dict[tuple[str, str], WorkCandidate] = {}
    with get_session() as session:
        watch_q = select(CommentWatch)
        if platform:
            watch_q = watch_q.where(CommentWatch.platform == platform)
        # 按 (平台, 作品) 归拢，和下面的作品字典保持同一口径，
        # 否则不同平台撞上同一个作品 ID 时会互相串味。
        watched_ids = {(w.platform, w.aweme_id)
                       for w in session.exec(watch_q).all() if w.aweme_id}

        monitor_q = select(ContentRecord).where(ContentRecord.create_time >= cutoff)
        if platform:
            monitor_q = monitor_q.where(ContentRecord.platform == platform)
        for row in session.exec(monitor_q.order_by(ContentRecord.id.desc())
                                .limit(MAX_CANDIDATES)).all():
            works[(row.platform, row.aweme_id)] = WorkCandidate(
                platform=row.platform, aweme_id=row.aweme_id, desc=row.desc,
                like_count=row.like_count, comment_count=row.comment_count,
                source="monitor")

        collection_q = select(KeywordCollectionContent).where(
            KeywordCollectionContent.create_time >= cutoff)
        if platform:
            collection_q = collection_q.where(KeywordCollectionContent.platform == platform)
        for row in session.exec(collection_q.order_by(KeywordCollectionContent.id.desc())
                                .limit(MAX_CANDIDATES)).all():
            key = (row.platform, row.aweme_id)
            if key in works:
                works[key].keyword = row.keyword or works[key].keyword
                continue
            works[key] = WorkCandidate(
                platform=row.platform, aweme_id=row.aweme_id, desc=row.desc,
                like_count=row.like_count, comment_count=row.comment_count,
                source="collection", keyword=row.keyword)

    if not works:
        return []

    wanted = {aweme_id for _, aweme_id in works}
    # 按 (平台, 作品) 归拢，避免不同平台的作品 id 撞车时互相串味。
    comments: dict[tuple[str, str], list[dict]] = {}
    with get_session() as session:
        for row in session.exec(
                select(CommentRecord).where(CommentRecord.aweme_id.in_(wanted))
        ).all():
            comments.setdefault((row.platform, row.aweme_id), []).append({
                "text": row.text, "create_time": row.create_time,
                "ip_label": row.ip_label})
        for row in session.exec(
                select(KeywordCollectionComment).where(
                    KeywordCollectionComment.aweme_id.in_(wanted))
        ).all():
            comments.setdefault((row.platform, row.aweme_id), []).append({
                "text": row.text, "create_time": row.create_time,
                "ip_label": row.ip_label})

    scored: list[WorkCandidate] = []
    for (work_platform, aweme_id), work in works.items():
        rows = comments.get((work_platform, aweme_id)) or []
        if not rows:
            # No comment data yet: there is no evidence of lead potential.
            continue
        work.watched = (work_platform, aweme_id) in watched_ids
        if work.watched and not include_watched:
            continue
        scored.append(score_work(
            work, rows, intent_keywords=intents, exclude_keywords=excludes,
            regions=region_terms, now=now, fresh_window_seconds=fresh_window))

    scored.sort(key=lambda item: (-item.score, -item.demand_count,
                                  -item.fresh_comments, item.aweme_id))
    return [work.as_dict() for work in scored[:max(1, int(limit))]]
