"""套餐额度统计。

「月线索额度」必须覆盖所有产生线索的渠道：评论监控入库的评论、关键词采集
抓到的评论都要计入，否则商家可以绕过额度限制。集中放在这里，避免各处口径
不一致。

按 (平台, 评论 ID) 去重计数：同一个客户每小时重跑一次关键词采集时，命中的
评论会被重复入库，但它们是同一条线索，只能算一次。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, true, union
from sqlmodel import select

from .models import CommentRecord, KeywordCollectionComment


def utc_month_start() -> datetime:
    now = datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_lead_usage(session) -> int:
    """本月已占用的线索额度（评论监控 + 关键词采集，按线索去重）。"""
    since = utc_month_start()
    watched = select(CommentRecord.platform, CommentRecord.comment_id).where(
        CommentRecord.created_at >= since)
    # 关键词采集入库的评论里只有命中线索过滤规则的才算线索，
    # 其余只是采集留档，不占用月线索额度。
    collected = select(
        KeywordCollectionComment.platform,
        KeywordCollectionComment.comment_id,
    ).where(
        KeywordCollectionComment.created_at >= since,
        KeywordCollectionComment.is_lead == true(),
    )
    distinct_leads = union(watched, collected).subquery()
    return int(session.exec(
        select(func.count()).select_from(distinct_leads)).one() or 0)


def lead_quota_remaining(session, limit: int | None) -> int | None:
    """剩余线索额度；limit 为 None 表示不限量。"""
    if limit is None:
        return None
    return max(0, int(limit) - monthly_lead_usage(session))
