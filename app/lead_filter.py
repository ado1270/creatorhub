"""线索过滤规则。

一条评论能不能当线索卖钱，全系统只有一个口径：

- 年龄：只认最近 N 分钟内的新评论（0 表示不限）
- 地区：IP 属地白名单，留空表示不限
- 意向词：命中任意一个才算线索，留空表示不限
- 排除词：命中任意一个就丢弃

评论监控按「每条监控自己的设置 + 全局兜底」判定，关键词采集没有单条监控
的概念，直接用全局规则。规则同时决定三件事：评论是否算线索、是否推送到
gdb15、是否占用月线索额度。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .settings import get_setting

# 默认只认 0～5 小时内的新评论，这是获客口径。
DEFAULT_MAX_AGE_MINUTES = 300
MAX_MAX_AGE_MINUTES = 10080  # 7 天


def _terms(raw: str) -> list[str]:
    """Split a comma / newline separated term list."""
    parts: list[str] = []
    for chunk in str(raw or "").replace("，", ",").replace("、", ",").split(","):
        for line in chunk.split("\n"):
            term = line.strip()
            if term:
                parts.append(term)
    return parts


@dataclass
class LeadFilter:
    """一条评论是否算作可交付线索的判定规则。"""

    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES
    regions: list[str] = field(default_factory=list)
    intent_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)

    def matches(self, comment: dict, *, now: int | None = None) -> bool:
        """Check one comment against this filter.

        缺少时间戳不等于评论很旧：平台没给时间时按「时间未知」处理，
        不能因此把线索悄悄丢掉。
        """
        comment = comment or {}
        max_age = int(self.max_age_minutes or 0)
        if max_age:
            create_time = int(comment.get("create_time") or 0)
            if create_time:
                current = int(now if now is not None else time.time())
                if max(0, current - create_time) > max_age * 60:
                    return False

        regions = [str(r).casefold() for r in self.regions if str(r).strip()]
        if regions:
            location = str(comment.get("ip_label") or "").strip().casefold()
            if not location or not any(r in location for r in regions):
                return False

        text = str(comment.get("text") or "").casefold()
        excludes = [str(t).casefold() for t in self.exclude_keywords if str(t).strip()]
        if excludes and any(t in text for t in excludes):
            return False
        intents = [str(t).casefold() for t in self.intent_keywords if str(t).strip()]
        if intents and not any(t in text for t in intents):
            return False
        return True


def load_lead_filter() -> LeadFilter:
    """Read the global lead filter from saved settings."""
    try:
        max_age = int(get_setting(
            "lead_filter_max_age_minutes", str(DEFAULT_MAX_AGE_MINUTES)))
    except (TypeError, ValueError):
        max_age = DEFAULT_MAX_AGE_MINUTES
    return LeadFilter(
        max_age_minutes=max(0, min(MAX_MAX_AGE_MINUTES, max_age)),
        regions=_terms(get_setting("lead_filter_regions", "")),
        intent_keywords=_terms(get_setting("lead_filter_intent_keywords", "")),
        exclude_keywords=_terms(get_setting("lead_filter_exclude_keywords", "")),
    )


def merge_watch_filter(watch: dict, base: LeadFilter) -> dict:
    """Blend a watch's own filter with the global baseline.

    监控自己的设置优先（更具体），全局设置兜底（防止客户把某条监控改成
    「不限年龄」后把陈年评论当成线索推出去）。两者都留空才算不限。
    """
    watch_age = int(watch.get("comment_max_age_minutes") or 0)
    base_age = int(base.max_age_minutes or 0)
    if watch_age and base_age:
        max_age = min(watch_age, base_age)
    else:
        max_age = watch_age or base_age

    def pick(key: str, fallback: list[str]) -> list[str]:
        own = [str(t).strip() for t in (watch.get(key) or []) if str(t).strip()]
        return own or list(fallback)

    exclude = list(base.exclude_keywords)
    for term in (watch.get("exclude_keywords") or []):
        term = str(term).strip()
        if term and term not in exclude:
            exclude.append(term)

    return {
        **watch,
        "comment_max_age_minutes": max_age,
        "comment_regions": pick("comment_regions", base.regions),
        "intent_keywords": pick("intent_keywords", base.intent_keywords),
        "exclude_keywords": exclude,
    }
