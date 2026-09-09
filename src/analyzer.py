"""LLM分析模块 — 单视频分析 + 当日汇总。"""

import json
import math
from typing import Optional

import httpx

from src.config import get_config, get_env


async def analyze_video(
    title: str,
    transcript_text: str,
    comments: list,
    danmakus: list,
    duration: int,
) -> dict:
    """LLM合并分析：转写文本 + 评论 + 弹幕 → 摘要 + 情绪 + 观点。

    Returns:
        {
            "summary": str,
            "key_points": [str],
            "sentiment": "bullish"|"bearish"|"neutral",
            "sentiment_score": float,  # -1.0 ~ 1.0
            "risk_warnings": [str],
            "data_citations": [str],
            "tags": [str],
            "comment_sentiment": {"bullish": float, "bearish": float, "neutral": float},
            "comment_keywords": [str],
            "danmaku_sentiment": {"bullish": float, "bearish": float, "neutral": float},
            "danmaku_keywords": [str],
        }
    """
    # Prepare comment text
    comment_text = ""
    if comments:
        comment_lines = []
        for c in comments[:20]:
            msg = c.get("content", {}).get("message", "")
            likes = c.get("like", 0)
            if msg:
                comment_lines.append(f"[{likes}赞] {msg}")
        comment_text = "\n".join(comment_lines)

    # Prepare danmaku text
    danmaku_text = ""
    if danmakus:
        danmaku_text = "\n".join(danmakus[:500])  # limit for prompt size

    prompt = f"""分析以下B站财经视频，输出JSON格式结果。

## 视频信息
标题: {title}
时长: {duration}秒

## 视频转写文本
{transcript_text[:8000]}

## 热门评论 (按热度排序)
{comment_text[:3000]}

## 弹幕样本 (最多500条)
{danmaku_text[:3000]}

请严格输出以下JSON格式（不要输出其他内容）：
{{
    "summary": "200字以内的视频内容摘要",
    "key_points": ["关键观点1", "关键观点2", "..."],
    "sentiment": "bullish/bearish/neutral",
    "sentiment_score": -1.0到1.0之间的浮点数,
    "risk_warnings": ["风险提示1", "..."],
    "data_citations": ["视频中提到的具体数据，如指数点位、涨跌幅等"],
    "tags": ["标签1", "标签2"],
    "comment_sentiment": {{"bullish": 0.0-1.0, "bearish": 0.0-1.0, "neutral": 0.0-1.0}},
    "comment_keywords": ["评论关键词1", "..."],
    "danmaku_sentiment": {{"bullish": 0.0-1.0, "bearish": 0.0-1.0, "neutral": 0.0-1.0}},
    "danmaku_keywords": ["弹幕关键词1", "..."]
}}"""

    result = await _call_llm(prompt)
    if result:
        parsed = _try_parse_json(result)
        if parsed is not None:
            if _validate_analysis(parsed):
                return parsed
            # JSON合法但字段缺失 → 记录警告，返回empty标记
            print(f"[analyzer] LLM返回了不完整的JSON结构，视为分析失败")
    return _empty_analysis(analysis_failed=True)


async def analyze_dynamic(content: str) -> dict:
    """LLM分析图文动态。

    Returns: {"summary": str, "sentiment": str, "tags": [str]}
    """
    prompt = f"""分析以下B站财经博主的图文动态，输出JSON。

## 动态内容
{content[:4000]}

请输出：
{{
    "summary": "100字以内的摘要",
    "sentiment": "bullish/bearish/neutral",
    "tags": ["标签1", "标签2"]
}}"""

    result = await _call_llm(prompt)
    if result:
        parsed = _try_parse_json(result)
        if parsed is not None:
            summary = parsed.get("summary")
            sentiment = str(parsed.get("sentiment", "")).strip().lower()
            if isinstance(summary, str) and summary.strip() and sentiment in _VALID_SENTIMENTS and _string_list(parsed.get("tags", [])):
                return {
                    "summary": summary,
                    "sentiment": sentiment,
                    "tags": parsed.get("tags", []),
                }
            print(f"[analyzer] dynamic LLM返回无效结构，视为分析失败")
    return {"summary": "", "sentiment": "neutral", "tags": [], "analysis_failed": True}


async def generate_daily_digest(blogger_analyses: list) -> dict:
    """生成当日跨博主汇总分析。

    Args:
        blogger_analyses: [
            {
                "mid": int, "name": str,
                "videos": [{"title": str, "summary": str, "sentiment": str, "key_points": [str]}],
                "dynamics": [{"summary": str, "sentiment": str}]
            }
        ]

    Returns:
        {
            "overall_sentiment": "bullish"|"bearish"|"neutral",
            "overall_sentiment_desc": str,
            "consensus": [str],
            "differences": [str],
            "key_topics": [str],
            "summary": str,
        }
    """
    if not blogger_analyses:
        return _empty_digest()

    sections = []
    for b in blogger_analyses:
        section = f"### {b['name']}\n"
        for v in b.get("videos", []):
            section += f"- 视频《{v['title']}》: {v['summary'][:200]} [情绪:{v['sentiment']}]\n"
            section += f"  关键观点: {', '.join(v.get('key_points', [])[:3])}\n"
        for d in b.get("dynamics", []):
            section += f"- 动态: {d['summary'][:150]} [情绪:{d['sentiment']}]\n"
        sections.append(section)

    prompt = f"""以下是今日B站财经博主的内容分析，请生成跨博主汇总。

{chr(10).join(sections)}

请输出JSON：
{{
    "overall_sentiment": "bullish或bearish或neutral（三选一，只输出英文单词）",
    "overall_sentiment_desc": "今日整体情绪的中文描述，一句话概括",
    "consensus": ["多位博主共识观点1", "..."],
    "differences": ["博主间分歧1", "..."],
    "key_topics": ["今日核心话题1", "..."],
    "summary": "300字以内的当日财经观点综述"
}}"""

    result = await _call_llm(prompt)
    if result:
        parsed = _try_parse_json(result)
        if parsed is not None:
            digest = _validate_digest(parsed)
            # 校验必要字段
            if digest.get("overall_sentiment") and digest.get("summary"):
                return digest
            print("[analyzer] daily digest LLM返回结构不完整，视为分析失败")
    return _empty_digest()


# ------------------------------------------------------------------
# LLM API 调用
# ------------------------------------------------------------------

async def _call_llm(prompt: str) -> Optional[str]:
    """调用MiMo LLM API。"""
    cfg = get_config()
    llm_cfg = cfg.get("llm", {})
    api_url = llm_cfg.get("api_url", "https://api.xiaomimimo.com/v1/chat/completions")
    model = llm_cfg.get("model", "mimo-v2.5-pro")
    api_key = get_env("XIAOMI_API_KEY")

    if not api_key:
        print("[analyzer] XIAOMI_API_KEY not set")
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是专业的财经内容分析师。严格按照要求输出JSON，不要输出其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as http:
        try:
            resp = await http.post(api_url, json=payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"[analyzer] LLM API error: {e}")
    return None


def _empty_analysis(analysis_failed: bool = False) -> dict:
    d = {
        "summary": "",
        "key_points": [],
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "risk_warnings": [],
        "data_citations": [],
        "tags": [],
        "comment_sentiment": {"bullish": 0, "bearish": 0, "neutral": 1.0},
        "comment_keywords": [],
        "danmaku_sentiment": {"bullish": 0, "bearish": 0, "neutral": 1.0},
        "danmaku_keywords": [],
    }
    if analysis_failed:
        d["analysis_failed"] = True
    return d


def _try_parse_json(text: str) -> Optional[dict]:
    """尝试从LLM输出中提取合法JSON，容忍包裹文本。"""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


_REQUIRED_ANALYSIS_FIELDS = {"summary", "key_points", "sentiment", "sentiment_score"}


def _validate_analysis(parsed: dict) -> bool:
    if not isinstance(parsed, dict) or not _REQUIRED_ANALYSIS_FIELDS.issubset(parsed):
        return False
    if not isinstance(parsed["summary"], str) or not parsed["summary"].strip():
        return False
    if not _valid_score(parsed.get("sentiment_score")):
        return False
    sentiment = parsed.get("sentiment")
    if not isinstance(sentiment, str) or sentiment.strip().lower() not in _VALID_SENTIMENTS:
        return False
    defaults = _empty_analysis()
    for key in ("key_points", "risk_warnings", "data_citations", "tags", "comment_keywords", "danmaku_keywords"):
        value = parsed.get(key, [])
        if not _string_list(value):
            return False
    for key in ("comment_sentiment", "danmaku_sentiment"):
        value = parsed.get(key, defaults[key])
        if not isinstance(value, dict) or set(value) != _VALID_SENTIMENTS:
            return False
        if not all(_valid_score(v) and v >= 0 for v in value.values()) or abs(sum(value.values()) - 1) > 0.02:
            return False
    for key, value in defaults.items():
        parsed.setdefault(key, value)
    parsed["sentiment"] = sentiment.strip().lower()
    parsed["summary"] = parsed["summary"].strip()
    return True


def _empty_digest() -> dict:
    return {
        "analysis_failed": True,
        "overall_sentiment": "neutral",
        "overall_sentiment_desc": "",
        "consensus": [],
        "differences": [],
        "key_topics": [],
        "summary": "",
    }


_VALID_SENTIMENTS = {"bullish", "bearish", "neutral"}


def _validate_digest(digest: dict) -> dict:
    if not isinstance(digest.get("summary"), str) or not digest["summary"].strip():
        return _empty_digest()
    sentiment = digest.get("overall_sentiment")
    if not isinstance(sentiment, str) or sentiment.strip().lower() not in _VALID_SENTIMENTS:
        return _empty_digest()
    if not all(_string_list(digest.get(key)) for key in ("consensus", "differences", "key_topics")):
        return _empty_digest()
    if not isinstance(digest.get("overall_sentiment_desc", ""), str):
        return _empty_digest()
    digest["overall_sentiment"] = sentiment.strip().lower()
    return digest


def _string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_score(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and -1 <= value <= 1
