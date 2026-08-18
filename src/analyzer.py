"""LLM分析模块 — 单视频分析 + 当日汇总。"""

import json
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
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(result[start:end])
                except json.JSONDecodeError:
                    pass
    return _empty_analysis()


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
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(result[start:end])
                except json.JSONDecodeError:
                    pass
    return {"summary": "", "sentiment": "neutral", "tags": []}


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
            "overall_sentiment": str,
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
    "overall_sentiment": "今日整体情绪: bullish/bearish/neutral + 中文描述",
    "consensus": ["多位博主共识观点1", "..."],
    "differences": ["博主间分歧1", "..."],
    "key_topics": ["今日核心话题1", "..."],
    "summary": "300字以内的当日财经观点综述"
}}"""

    result = await _call_llm(prompt)
    if result:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(result[start:end])
                except json.JSONDecodeError:
                    pass
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
            result = resp.json()
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"[analyzer] LLM API error: {e}")
    return None


def _empty_analysis() -> dict:
    return {
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


def _empty_digest() -> dict:
    return {
        "overall_sentiment": "neutral",
        "consensus": [],
        "differences": [],
        "key_topics": [],
        "summary": "",
    }
