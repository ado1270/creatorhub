"""获客链路自检。

交付客户时最怕的不是功能没做，而是链路悄悄断了还在显示「正常」：
API Key 是占位符、推送开关没开、监控位满了、账号被风控冻住——
采到的线索堆在本机，gdb15 一条都收不到，而客户看到的是「采集正常」。
这里把交付验收 SOP 里最容易静默失败的项做成一次性检查，
每项都给出「问题」和「下一步动作」，让非开发的人也能照着收拾。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, true
from sqlmodel import select

from .db import get_session
from .lead_push import lead_push_settings, outbox_stats, probe_lead_push
from .license import effective_status
from .models import (AccountRiskState, CommentRecord, CommentWatch,
                     DanmakuWatch, DouyinAccount, KeywordCollectionComment,
                     KeywordCollectionJob, MonitorTarget)
from .quota import monthly_lead_usage, utc_month_start

# 一眼就能看出不是真 Key 的写法。客户手填时很容易留下这种占位内容。
_PLACEHOLDER_PATTERNS = (
    re.compile(r"^(lk_)?[x*_.-]{0,20}$", re.I),
    re.compile(r"(你的|示例|占位|待填|替换|placeholder|changeme|todo|fixme|xxxx)", re.I),
)


def _check(cid, group, label, status, detail, fix=""):
    return {"id": cid, "group": group, "label": label, "status": status,
            "detail": detail, "fix": fix}


def _looks_placeholder_key(key: str) -> bool:
    value = (key or "").strip()
    if len(value) < 8:
        return True
    return any(p.search(value) for p in _PLACEHOLDER_PATTERNS)


def _age_text(when: datetime | None) -> str:
    if not when:
        return "从未"
    delta = datetime.utcnow() - when
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 24 * 60:
        return f"{minutes // 60} 小时前"
    return f"{minutes // (60 * 24)} 天前"


async def run_selfcheck(cfg, *, probe: bool = False) -> dict:
    """跑一轮链路自检。probe=True 时才会真的向 gdb15 发一次探测。"""
    checks: list[dict] = []
    plan = getattr(cfg, "plan", None)
    now = datetime.utcnow()

    with get_session() as session:
        accounts = session.exec(select(DouyinAccount)).all()
        active_accounts = [a for a in accounts if (a.status or "active") == "active"]
        risk_rows = session.exec(select(AccountRiskState)).all()
        works = int(session.exec(select(func.count(MonitorTarget.id))
                                 .where(MonitorTarget.enabled == True)).one())  # noqa: E712
        comment_watches = session.exec(
            select(CommentWatch).where(CommentWatch.enabled == True)).all()  # noqa: E712
        danmaku = int(session.exec(select(func.count(DanmakuWatch.id))
                                   .where(DanmakuWatch.enabled == True)).one())  # noqa: E712
        last_job = session.exec(select(KeywordCollectionJob)
                                .order_by(KeywordCollectionJob.id.desc())
                                .limit(1)).first()
        # 最近 5 小时的新线索：评论监控入库 + 关键词采集判定为线索的评论，
        # 按 (平台, 评论 ID) 去重。这是「今天能不能收到客户」的直接指标。
        fresh_since = now - timedelta(hours=5)
        day_since = now - timedelta(hours=24)
        fresh_leads = int(session.exec(
            select(func.count()).select_from(
                select(CommentRecord.platform, CommentRecord.comment_id)
                .where(CommentRecord.created_at >= fresh_since,
                       CommentRecord.watch_id.is_distinct_from(0))
                .union(
                    select(KeywordCollectionComment.platform,
                           KeywordCollectionComment.comment_id)
                    .where(KeywordCollectionComment.is_lead == true(),
                           KeywordCollectionComment.created_at >= fresh_since)
                ).subquery())).one())
        day_leads = int(session.exec(
            select(func.count()).select_from(
                select(CommentRecord.platform, CommentRecord.comment_id)
                .where(CommentRecord.created_at >= day_since,
                       CommentRecord.watch_id.is_distinct_from(0))
                .union(
                    select(KeywordCollectionComment.platform,
                           KeywordCollectionComment.comment_id)
                    .where(KeywordCollectionComment.is_lead == true(),
                           KeywordCollectionComment.created_at >= day_since)
                ).subquery())).one())
        month_usage = monthly_lead_usage(session)

    # ── 采集侧 ──
    if not accounts:
        checks.append(_check(
            "account", "采集", "采集账号", "fail",
            "还没有登录任何平台账号，关键词采集和评论监控都跑不起来。",
            "先到「账号」里登录一个抖音或小红书账号。"))
    elif not active_accounts:
        checks.append(_check(
            "account", "采集", "采集账号", "fail",
            f"{len(accounts)} 个账号全部失效，需要重新登录。",
            "在「账号」里对失效账号重新登录。"))
    else:
        checks.append(_check(
            "account", "采集", "采集账号", "pass",
            f"可用账号 {len(active_accounts)} 个（共 {len(accounts)} 个）。"))

    frozen = []
    for row in risk_rows:
        if row.manual_review_required:
            frozen.append((row.account_id, "需要人工复核"))
        elif row.cooldown_until and row.cooldown_until > now:
            left = int((row.cooldown_until - now).total_seconds() // 60)
            frozen.append((row.account_id, f"冷却中，还剩 {left} 分钟"))
    if frozen:
        names = "、".join(f"账号{aid}（{why}）" for aid, why in frozen[:4])
        checks.append(_check(
            "risk", "采集", "账号风控状态", "warn",
            f"{len(frozen)} 个账号被风控限制：{names}。这段时间它们采不到东西。",
            "等冷却结束，或在风控面板里确认原因后手动恢复。"))
    elif accounts:
        checks.append(_check(
            "risk", "采集", "账号风控状态", "pass", "没有账号处于冷却或熔断。"))

    slots = works + len(comment_watches) + danmaku
    limit = getattr(plan, "active_monitor_slots", None) if plan else None
    if limit is not None and slots >= int(limit):
        checks.append(_check(
            "slots", "采集", "监控位用量", "warn",
            f"监控位已用满 {slots}/{limit}"
            f"（作品 {works}、评论 {len(comment_watches)}、弹幕 {danmaku}），"
            "再新建或启用会被拒绝。",
            "暂停不需要的监控，或升级套餐。"))
    else:
        detail = (f"监控位 {slots}/{limit}" if limit is not None
                  else f"监控位 {slots}（本地未设上限）")
        checks.append(_check(
            "slots", "采集", "监控位用量", "pass",
            f"{detail}：作品 {works}、评论 {len(comment_watches)}、弹幕 {danmaku}。"))

    if not comment_watches:
        checks.append(_check(
            "watch", "采集", "评论监控", "warn",
            "没有启用中的评论监控。评论监控才是持续产出新线索的那条路，"
            "只靠一次性关键词采集，线索会很快过时。",
            "到「优质作品候选」挑几个作品一键加入评论监控。"))
    else:
        stale = []
        errored = []
        for watch in comment_watches:
            interval = int(watch.interval_seconds or 0) or 600
            # 允许三个周期的宽限，避免正好卡在扫描间隙被误报。
            if (watch.last_scan_at is None
                    or watch.last_scan_at < now - timedelta(seconds=interval * 3 + 120)):
                stale.append(watch)
            if (watch.last_error or "").strip():
                errored.append(watch)
        if errored:
            names = "、".join((w.alias or w.title or str(w.aweme_id))[:20]
                              for w in errored[:3])
            checks.append(_check(
                "watch", "采集", "评论监控", "warn",
                f"{len(errored)} 个评论监控最近一次扫描报错：{names}。"
                f"最近一条错误：{(errored[0].last_error or '')[:160]}",
                "看错误原因处理：登录失效就重新登录，触发验证码就换时段或降频率，"
                "被风控拦住就等冷却结束。"))
        elif stale:
            names = "、".join((w.alias or w.title or str(w.aweme_id))[:20]
                              for w in stale[:3])
            checks.append(_check(
                "watch", "采集", "评论监控", "warn",
                f"{len(comment_watches)} 个启用中的评论监控里，"
                f"{len(stale)} 个超出预期时间没扫过：{names}。"
                "可能是没有可用账号、被风控挡住，或调度没轮到。",
                "看采集账号是否在线，再等一个检查周期复跑自检。"))
        else:
            scanned = [w.last_scan_at for w in comment_watches if w.last_scan_at]
            checks.append(_check(
                "watch", "采集", "评论监控", "pass",
                f"{len(comment_watches)} 个评论监控都在按周期扫描，"
                + (f"最近一次扫描 {_age_text(max(scanned))}。" if scanned
                   else "尚未有扫描记录。")))

    if last_job:
        if last_job.status == "failed":
            checks.append(_check(
                "collection", "采集", "最近一次关键词采集", "warn",
                f"最近一次采集任务失败（{_age_text(last_job.created_at)}）："
                f"{(last_job.error or last_job.blocked_reason or '')[:140]}",
                "换关键词或降低抓取量再跑一次；被风控拦截时等冷却结束。"))
        else:
            checks.append(_check(
                "collection", "采集", "最近一次关键词采集", "pass",
                f"状态 {last_job.status}（{_age_text(last_job.created_at)}），"
                f"作品 {last_job.content_count} 个、评论 {last_job.comment_count} 条。"))
    else:
        checks.append(_check(
            "collection", "采集", "最近一次关键词采集", "info",
            "还没跑过关键词采集。", "先手动跑一轮，确认能采到评论。"))

    # ── 推送侧（最关键）──
    settings = lead_push_settings()
    stats = outbox_stats()
    if not settings["enabled"]:
        checks.append(_check(
            "push", "推送", "gdb15 自动推送", "fail",
            "推送开关没打开。采到的线索只留在本机，gdb15 永远看不到。",
            "到「设置」里启用 gdb15 线索自动推送。"))
    elif _looks_placeholder_key(settings["api_key"]) or not settings["url"]:
        checks.append(_check(
            "push", "推送", "gdb15 自动推送", "fail",
            "推送开关已打开，但 API Key 没填或填的是占位内容。"
            "这种情况下每一条推送都会被拒，线索会一直堆在本地。",
            "到 gdb15 后台复制真实 API Key 填进来，再点「测试推送」。"))
    else:
        checks.append(_check(
            "push", "推送", "gdb15 自动推送", "pass",
            f"已启用，推送地址 {settings['url']}，API Key 已配置。"))

    if stats["pending"]:
        oldest = stats["items"][-1] if stats["items"] else None
        oldest_error = str((oldest or {}).get("last_error") or "")[:120]
        checks.append(_check(
            "outbox", "推送", "待补推线索", "fail",
            f"{stats['pending']} 条线索采到了但还没送进 gdb15"
            + f"。排队最久的一条失败原因：{oldest_error or '未记录'}",
            "修好 API Key 或网络后点「立即补推」；自动补推最长每 6 小时一次。"))
    else:
        checks.append(_check(
            "outbox", "推送", "待补推线索", "pass",
            "没有积压，采到的线索都送出去了。"))

    if stats["failed"]:
        checks.append(_check(
            "failed", "推送", "需人工处理的线索", "warn",
            f"{stats['failed']} 条线索自动重试到上限仍未送达，"
            "不会再自动补推，但记录还在本机。",
            "确认 Key 与额度正常后点「立即补推」，会把这批一起捡回来。"))

    if probe:
        result = await probe_lead_push()
        if result["ok"]:
            checks.append(_check(
                "probe", "推送", "gdb15 连通探测", "pass",
                f"gdb15 接受连接（HTTP {result['status']}），"
                "探测不产生线索、不扣额度。"))
        else:
            why = {
                "invalid_key": "API Key 无效或已被删除",
                "package_expired": "gdb15 套餐已到期",
                "credit_exhausted": "gdb15 线索额度已用完",
                "unconfigured": "URL 或 API Key 未配置",
                "disabled": "推送开关未启用",
                "network": "网络不通",
                "old_server": "gdb15 服务端版本过旧，不认识连通探测",
            }.get(result.get("reason", ""), result.get("reason", ""))
            checks.append(_check(
                "probe", "推送", "gdb15 连通探测", "fail",
                f"gdb15 拒绝了这次连接：{why}。{(result.get('error') or '')[:120]}",
                "按原因处理：重新生成 Key、续费或补额度，然后重跑自检。"))
    else:
        checks.append(_check(
            "probe", "推送", "gdb15 连通探测", "info",
            "未执行。点「含连通探测」才会真的向 gdb15 发一次请求。"))

    # ── 额度与授权 ──
    lead_limit = getattr(plan, "monthly_lead_records", None) if plan else None
    if lead_limit is None:
        checks.append(_check(
            "quota", "额度", "本月线索额度", "info",
            f"本地未设月线索上限，本月已入库 {month_usage} 条。"))
    else:
        lead_limit = int(lead_limit)
        left = max(0, lead_limit - month_usage)
        if left == 0:
            checks.append(_check(
                "quota", "额度", "本月线索额度", "fail",
                f"本月线索额度已用完：{month_usage}/{lead_limit}。"
                "新的评论不会再入库，也不会推给 gdb15。",
                "等下个月重置，或在 gdb15 里加额度。"))
        elif left <= lead_limit * 0.2:
            checks.append(_check(
                "quota", "额度", "本月线索额度", "warn",
                f"本月线索额度只剩 {left}/{lead_limit}，已用 {month_usage} 条。",
                "提前在 gdb15 里补额度，别在月底断供。"))
        else:
            checks.append(_check(
                "quota", "额度", "本月线索额度", "pass",
                f"本月已用 {month_usage}/{lead_limit}，剩 {left} 条。"))

    lic = effective_status(cfg)
    if not lic.get("enabled") and lic.get("source") != "server":
        checks.append(_check(
            "license", "额度", "服务端授权", "info",
            lic.get("message") or "未启用服务端套餐校验，套餐以本地 config.yaml 为准。",
            "交付客户时建议启用，否则客户改本地配置就能绕过套餐。"))
    elif lic.get("status") in ("expired", "inactive", "no_package"):
        checks.append(_check(
            "license", "额度", "服务端授权", "fail",
            f"gdb15 返回套餐状态 {lic['status']}，配额已被锁死。",
            "在 gdb15 里续费或重新开通套餐。"))
    elif lic.get("stale"):
        checks.append(_check(
            "license", "额度", "服务端授权", "warn",
            "已经很久没连上 gdb15 取到套餐了，现在按上次缓存限流。",
            "检查网络与 API Key，必要时点「立即校验」。"))
    else:
        checks.append(_check(
            "license", "额度", "服务端授权", "pass",
            f"套餐 {lic.get('plan_name') or '-'} 来自"
            f"{'gdb15 下发' if lic.get('enforced') else '本地配置'}。"))

    # ── 线索产出（结果指标）──
    if fresh_leads:
        checks.append(_check(
            "output", "产出", "最近 5 小时新线索", "pass",
            f"最近 5 小时入库 {fresh_leads} 条线索，24 小时 {day_leads} 条。"))
    elif day_leads:
        checks.append(_check(
            "output", "产出", "最近 5 小时新线索", "warn",
            f"最近 5 小时没有新线索，但 24 小时内有 {day_leads} 条。"
            "评论监控的周期可能太长，或这会儿评论区就是安静。",
            "把主力监控位的检查频率调到 30 分钟内。"))
    else:
        checks.append(_check(
            "output", "产出", "最近 5 小时新线索", "fail",
            "24 小时内没有任何线索入库。这条链路等于没在产出，"
            "客户买的是获客，不是软件界面。",
            "确认采集账号在线、有启用中的评论监控或定时采集任务。"))

    counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
    for item in checks:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "summary": {**counts, "total": len(checks)},
        "month_start": utc_month_start().isoformat(timespec="seconds"),
        "checks": checks,
    }
