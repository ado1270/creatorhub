"""补推历史线索到 gdb15。

用途：换了 API Key、网络故障或者升级之前的版本，导致有一批评论没推出去。
gdb15 按 commentId 去重，重复推送不会产生重复线索，所以可以放心重推。

用法：
    python scripts/backfill_lead_push.py --days 7
    python scripts/backfill_lead_push.py --days 30 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.lead_push import (build_payloads, enqueue_payloads, send_payloads)  # noqa: E402
from app.models import (CommentRecord, CommentWatch, KeywordCollectionComment,  # noqa: E402
                         KeywordCollectionContent)
from sqlmodel import select  # noqa: E402


def collect_keyword_comments(since: datetime) -> list[tuple]:
    """关键词采集入库的评论，带上命中它的搜索词作为来源归因。"""
    out = []
    with db.get_session() as session:
        rows = session.exec(
            select(KeywordCollectionComment, KeywordCollectionContent)
            .join(KeywordCollectionContent,
                  KeywordCollectionContent.id == KeywordCollectionComment.content_id)
            .where(KeywordCollectionComment.created_at >= since)).all()
        for comment, content in rows:
            out.append((
                comment.platform or "douyin",
                comment.aweme_id,
                [{
                    "text": comment.text,
                    "comment_id": comment.comment_id,
                    "user_nickname": comment.user_nickname,
                    "user_sec_uid": comment.user_sec_uid,
                    "create_time": comment.create_time,
                }],
                content.keyword or "",
            ))
    return out


def collect_watch_comments(since: datetime) -> list[tuple]:
    """评论监控入库的评论，带上监控别名作为来源归因。"""
    out = []
    with db.get_session() as session:
        rows = session.exec(
            select(CommentRecord).where(CommentRecord.created_at >= since)).all()
        # 监控被删掉后评论还在，归因就空着，别因为没了监控就把线索丢掉。
        aliases = {watch.id: watch.alias for watch in session.exec(select(CommentWatch)).all()}
        for comment in rows:
            out.append((
                comment.platform or "douyin",
                comment.aweme_id,
                [{
                    "text": comment.text,
                    "comment_id": comment.comment_id,
                    "user_nickname": comment.user_nickname,
                    "user_sec_uid": comment.user_sec_uid,
                    "create_time": comment.create_time,
                }],
                aliases.get(comment.watch_id, ""),
            ))
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7,
                        help="补推最近多少天的线索，默认 7 天")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计不推送")
    args = parser.parse_args()

    cfg = load_config()
    db.init_db(cfg.db_path)
    since = datetime.utcnow() - timedelta(days=max(1, args.days))

    groups = collect_keyword_comments(since) + collect_watch_comments(since)
    print(f"待补推：{len(groups)} 个作品分组，时间范围 {since:%Y-%m-%d} 之后")
    if not groups:
        return 0
    if args.dry_run:
        for platform, aweme_id, comments, keyword in groups[:20]:
            print(f"  {platform}/{aweme_id} {keyword} -> {len(comments)} 条")
        if len(groups) > 20:
            print(f"  …… 还有 {len(groups) - 20} 组")
        return 0

    pushed = failed = queued = 0
    for platform, aweme_id, comments, keyword in groups:
        payloads = build_payloads(
            comments, platform=platform, item_id=aweme_id,
            intent_label=keyword, source_keyword=keyword)
        if not payloads:
            continue
        bad, errors = await send_payloads(payloads)
        pushed += len(payloads) - len(bad)
        failed += len(bad)
        if bad:
            queued += enqueue_payloads(
                bad, "推送失败", reasons=errors)
        if bad:
            print(f"  {platform}/{aweme_id} {keyword}: 失败 {len(bad)} 条"
                  f"（{errors[0] if errors else ''}）")
    print(f"补推完成：成功 {pushed} 条，失败 {failed} 条，入暂存箱 {queued} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
