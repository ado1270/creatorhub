"""AI 意向判定：把「这条评论像不像潜在客户」交给大模型判一次。

为什么要这一层：意向词是硬闸门（命中才留），说法稍微变一点就漏——
「这车还在不」「能到英德吗」里没有「多少 / 价格」，却是实打实的买家。

判定口径（AI 开启时与意向词并联，取并集，谁都不单独否决对方）：

    高意向 = AI 分 >= high，或命中意向词
    待人工 = review <= AI 分 < high 且未命中意向词
    丢弃   = AI 分 < review 且未命中意向词（模型明确判定不是线索）
    模型没给分（缺条目 / 分数越界 / 输出坏了）-> 记 0 分但按待人工留下，
            绝不静默丢线索；整批调用失败由引擎回退意向词口径。

时间窗、地区、排除词仍是硬条件（见 monitor._comment_watch_hard_match），
AI 只负责「意向」这一个维度。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .settings import get_setting

TIER_LEAD = "lead"        # 高意向，直接给人工跟进
TIER_REVIEW = "review"    # 灰区，入库并标注，等人来看
TIER_DROP = "drop"        # AI 明确判定不是线索

DEFAULT_HIGH = 70
DEFAULT_REVIEW = 40
DEFAULT_BATCH = 12        # 一次请求带多少条评论（本地小模型建议调小到 4~6）
MAX_TEXT = 160            # 单条评论进提示词的长度上限
MAX_REASON = 60

LABELS = ("询价", "求购", "求联系方式", "约看车", "售后", "同行", "广告", "闲聊", "无关")

# 类别 -> 分数。让模型只选类别、分数由这里查表：实测本地小模型自由打分时
# 会把「抖音骗子多」也打成 90 分（分数不可信），而选类别的准确率明显更高。
# 模型同时给了数字分数时以数字为准（见 decide），这里只兜住「只回类别」的情况。
CATEGORY_SCORES = {"询价": 95, "求购": 92, "求联系方式": 90, "约看车": 88,
                   "售后": 25, "同行": 10, "广告": 5, "闲聊": 8, "无关": 3}

_CATEGORY_GUIDE = """类别定义：
- 询价：问多少钱、什么价、能不能便宜。例：多少钱能卖？ 800行不行
- 求购：想买、要、还有货吗、还有车吗。例：这车还在不 我要一台
- 求联系方式：要电话微信地址、怎么联系。例：怎么联系你 你在哪里
- 约看车：约时间地点来看货。例：明天能去看吗
- 售后：已经买过之后的维修退货等问题。例：刹车坏了怎么办
- 同行：自己也收车卖车、谈合作。例：我也收车 长期合作呗
- 广告：留自己号引流、骗术提醒、无关推销。例：加我微信123 抖音骗子多别信
- 闲聊：夸视频、玩梗、无关感慨。例：老板发大财 这车真好看
- 无关：与生意完全无关"""


@dataclass
class IntentAIConfig:
    """一次判定需要的模型配置。"""
    base_url: str
    api_key: str
    model: str
    business: str = ""
    high: int = DEFAULT_HIGH
    review: int = DEFAULT_REVIEW
    timeout: float = 20.0
    batch: int = DEFAULT_BATCH
    push_review: bool = False   # 灰区（待人工）是否也推给 gdb15


class IntentAIError(RuntimeError):
    """AI 判定整批不可用（配置缺失、网络失败、输出无法解析）。"""


def _number(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def config() -> IntentAIConfig | None:
    """读全局设置；未启用或配置不全时返回 None（引擎走意向词口径）。"""
    if get_setting("intent_ai_enabled", "0") != "1":
        return None
    base = get_setting("intent_ai_base_url", "").strip()
    model = get_setting("intent_ai_model", "").strip()
    key = get_setting("intent_ai_api_key", "").strip()
    if not (base and model):
        return None
    return IntentAIConfig(
        base_url=base.rstrip("/"),
        api_key=key,
        model=model,
        business=get_setting("intent_ai_business", "").strip()[:400],
        high=_number(get_setting("intent_ai_high", ""), DEFAULT_HIGH, 1, 100),
        review=_number(get_setting("intent_ai_review", ""), DEFAULT_REVIEW, 0, 100),
        # 本地模型（Ollama）第一次调用要把权重load进内存，20 秒根本不够；
        # 默认给 60 秒，批量小 + 云端模型时可以往下调。
        timeout=float(_number(get_setting("intent_ai_timeout", ""), 60, 3, 300)),
        batch=_number(get_setting("intent_ai_batch", ""), DEFAULT_BATCH, 1, 30),
        push_review=get_setting("intent_ai_push_review", "0") == "1",
    )


def build_messages(comments: list[dict], business: str = "") -> list[dict]:
    """把一批评论拼成一次 chat 请求的 messages。

    只让模型选类别，分数在 CATEGORY_SCORES 里查表：本地小模型自由打分时
    几乎全线给高分（实测把「抖音骗子多」也打成 90 分），选类别却靠谱得多。
    输出用逐行「编号|类别」——要它吐整段 JSON 时 1.5B 会照抄示例然后只回
    一条；云端模型两种写法都能过，解析侧两种都收。
    """
    rows = []
    for idx, c in enumerate(comments):
        text = str((c or {}).get("text") or "").strip().replace("\n", " ")[:MAX_TEXT]
        nick = str((c or {}).get("user_nickname") or "")[:32]
        region = str((c or {}).get("ip_label") or "")[:16]
        rows.append(f"{idx}. {text}"
                    + (f"（{region}）" if region else "")
                    + (f" [{nick}]" if nick else ""))
    biz = business or "短视频评论区里找值得人工跟进的潜在客户"
    system = (
        "你是获客线索审核员。每条评论回一行，格式是 编号|类别。"
        "不要解释、不要代码块、不要只回第一条、不要自己编新编号。"
    )
    user = (
        f"生意：{biz}\n"
        "任务：给下面每条评论选一个类别，只能从这些类别里选：\n"
        + _CATEGORY_GUIDE + "\n"
        "判断底线：问价、要货、要联系方式、约来看 = 潜在客户；"
        "夸你、评论视频本身、留他自己的微信号引流、自己也收车的同行 = 不是客户。\n"
        f"共 {len(rows)} 条，必须回 {len(rows)} 行。\n"
        "评论：\n" + "\n".join(rows) + "\n"
        "输出（从 0 编号开始，每行一条）："
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


_ARRAY_RE = re.compile(r"\[.*\]", re.S)
# 逐行格式「编号|分数|类别|依据」，分隔符允许 | , ; : 或空格。
_SEP_RE = re.compile(r"[|,;:]+|\s+")


def _coerce_score(raw) -> int:
    try:
        score = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return -1
    return score if -1 <= score <= 100 else -1


def _split_line(line: str) -> list[str]:
    return [part for part in _SEP_RE.split(line.strip()) if part]


def _from_lines(text: str, count: int) -> list[dict | None]:
    """解析逐行输出，两种写法都收：`编号|类别` 与 `编号|分数|类别|依据`。

    只认已登记的类别名——模型把「0. 这车还在不」这类评论原文连编号抄回来时，
    不能被误读成判定。缺条目的位置留 None，由 parse_verdicts 占位成未判定。
    """
    out: list[dict | None] = [None] * max(0, int(count))
    for line in text.splitlines():
        parts = _split_line(line)
        if len(parts) < 2:
            continue
        pos_raw = parts[0].rstrip(".．、)")
        if not pos_raw.isdigit():
            continue
        pos = int(pos_raw)
        if not 0 <= pos < len(out):
            continue
        head = parts[1]
        if head.isdigit():
            score = _coerce_score(head)
            label = parts[2] if len(parts) > 2 else ""
            reason = " ".join(parts[3:]) if len(parts) > 3 else ""
        else:
            if head not in CATEGORY_SCORES:
                continue
            score, label = -1, head
            reason = " ".join(parts[2:]) if len(parts) > 2 else ""
        out[pos] = {"score": score, "label": label[:16],
                    "reason": reason[:MAX_REASON]}
    return out


def _from_json_array(text: str, count: int) -> list[dict | None]:
    """解析云端模型常见的 JSON 数组输出。"""
    out: list[dict | None] = [None] * max(0, int(count))
    match = _ARRAY_RE.search(text)
    if not match:
        return out
    try:
        data = json.loads(match.group(0))
    except Exception:
        return out
    if not isinstance(data, list):
        return out
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        pos = item.get("i", item.get("index", idx))
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            continue
        if not 0 <= pos < len(out):
            continue
        out[pos] = {"score": _coerce_score(item.get("s", item.get("score"))),
                    "label": str(item.get("t") or item.get("label") or "").strip()[:16],
                    "reason": str(item.get("r") or item.get("reason") or "").strip()[:MAX_REASON]}
    return out


def parse_verdicts(content: str, count: int) -> list[dict]:
    """把模型输出解析成与输入等长的判定列表。

    缺条目的位置占位 score=-1（引擎按「待人工」处理），完全解析不出来才返回
    空列表，由调用方判定为整批失败。
    """
    text = str(content or "").strip()
    if not text:
        return []
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.S).strip()
    for parser in (_from_json_array, _from_lines):
        rows = parser(text, count)
        if any(row is not None for row in rows):
            return [row or {"score": -1, "label": "", "reason": ""} for row in rows]
    return []


async def chat(messages: list[dict], cfg: IntentAIConfig) -> str:
    """OpenAI 兼容 /chat/completions 调用，与文案生成同一条通路。"""
    body = {"model": cfg.model, "messages": messages,
            "temperature": 0, "max_tokens": 700}
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            resp = await client.post(f"{cfg.base_url}/chat/completions",
                                     json=body, headers=headers)
    except Exception as exc:
        raise IntentAIError(f"请求失败: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise IntentAIError(f"HTTP {resp.status_code}: {resp.text[:160]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise IntentAIError(f"返回不是 JSON: {exc}") from exc
    choice = ((data.get("choices") or [{}])[0].get("message") or {})
    return str(choice.get("content") or "")


async def score(comments: list[dict], cfg: IntentAIConfig) -> list[dict]:
    """给一批评论打分，返回与输入等长的判定列表（缺条目记未判定）。"""
    if not comments:
        return []
    batch = max(1, int(cfg.batch or DEFAULT_BATCH))
    verdicts: list[dict] = []
    for start in range(0, len(comments), batch):
        chunk = comments[start:start + batch]
        raw = await chat(build_messages(chunk, cfg.business), cfg)
        parsed = parse_verdicts(raw, len(chunk))
        if not parsed:     # 整批解析不出来 = 模型没干活，交给引擎回退
            raise IntentAIError("模型输出无法解析为判定列表")
        verdicts.extend(parsed)
    return verdicts[:len(comments)]


def decide(comment: dict, verdict: dict | None, cfg: IntentAIConfig,
           intent_keywords: list[str] | None = None) -> dict:
    """合并 AI 判定与意向词命中，给出层级。

    意向词命中是人工验证过的高信号，直接算高意向；AI 负责把说法不同、
    没撞上词的客户捞回来。两条通道取并集，只会多捞，不会互相漏。

    分数优先级：模型给的数字 > 类别查表 > 未判定（走待人工）。
    """
    verdict = verdict or {}
    raw_score = verdict.get("score")
    scored = isinstance(raw_score, int) and 0 <= raw_score <= 100
    score_value = int(raw_score) if scored else 0
    label = str(verdict.get("label") or "").strip()
    if not scored and label in CATEGORY_SCORES:
        # 提示词只要求选类别，分数在这里查表，避免小模型全员满分。
        score_value = CATEGORY_SCORES[label]
        scored = True
    text = str((comment or {}).get("text") or "").casefold()
    hits = [str(t) for t in (intent_keywords or [])
            if str(t).strip() and str(t).casefold() in text]
    high = max(1, int(cfg.high or DEFAULT_HIGH))
    review = max(0, min(int(cfg.review or DEFAULT_REVIEW), high))
    if hits or score_value >= high:
        tier = TIER_LEAD
    elif not scored or score_value >= review:
        tier = TIER_REVIEW
    else:
        tier = TIER_DROP
    if hits and label:
        label = f"{label}·命中词"
    elif hits:
        label = "命中意向词"
    elif not scored and not label:
        label = "AI 未判定"
    return {"tier": tier, "score": score_value, "label": label[:24],
            "reason": str(verdict.get("reason") or "")[:MAX_REASON],
            "matched": hits, "scored": scored}
