"""字幕/转写模块 — 三层降级链：CC字幕 → ai-zh字幕 → MiMo ASR。"""

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from src.bilibili import BiliClient, _run_curl
from src.config import get_config, get_env


async def fetch_transcript(client: BiliClient, bvid: str, cid: int, duration: int) -> dict:
    """三层降级获取视频转写文本。

    Args:
        client: B站客户端
        bvid: 视频BV号
        cid: 视频CID
        duration: 视频时长(秒)

    Returns:
        {"source": "cc_subtitle"|"ai_subtitle"|"asr", "text": str, "segments": int}
    """

    # Layer 1: CC字幕
    try:
        cc_url = await client.get_cc_subtitle(bvid, cid)
        if cc_url:
            text = await _download_subtitle(cc_url)
            if text and len(text) > 50:
                print(f"[transcript] {bvid}: CC字幕命中 ({len(text)} chars)")
                return {"source": "cc_subtitle", "text": text, "segments": 1}
    except Exception as e:
        print(f"[transcript] {bvid}: CC字幕降级失败: {e}")

    # Layer 2: AI中文字幕 (需SESSDATA)
    try:
        ai_url = await client.get_subtitle_url(bvid, cid)
        if ai_url:
            text = await _download_subtitle(ai_url)
            if text and len(text) > 50:
                print(f"[transcript] {bvid}: ai-zh字幕命中 ({len(text)} chars)")
                return {"source": "ai_subtitle", "text": text, "segments": 1}
    except Exception as e:
        print(f"[transcript] {bvid}: AI字幕降级失败: {e}")

    # Layer 3: MiMo ASR (切片≤3min)
    print(f"[transcript] {bvid}: 无字幕，降级到ASR")
    asr_result = await _asr_transcribe(client, bvid, cid, duration)
    if asr_result:
        return asr_result

    return {"source": "asr", "text": "", "segments": 0}


async def _download_subtitle(url: str) -> str:
    """下载字幕JSON并提取纯文本。使用curl避免httpx超时中断降级链。"""
    try:
        cmd = ["curl", "-s", "--max-time", "15", "-L", url]
        body, status = await _run_curl(cmd, timeout=20)
        if status and status != 200:
            print(f"[transcript] subtitle download HTTP {status}")
            return ""
        data = json.loads(body)
        segs = data.get("body", [])
        # Each item: {"from": 0.0, "to": 2.0, "content": "text"}
        texts = [item.get("content", "") for item in segs]
        return " ".join(texts)
    except (TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[transcript] subtitle download failed: {e}")
        return ""


async def _asr_transcribe(client: BiliClient, bvid: str, cid: int, duration: int) -> Optional[dict]:
    """MiMo ASR转写，自动切片≤3分钟。"""
    cfg = get_config()
    asr_cfg = cfg.get("asr", {})
    chunk_sec = asr_cfg.get("chunk_seconds", 180)
    api_url = asr_cfg.get("api_url", "https://api.xiaomimimo.com/v1/chat/completions")
    api_key = get_env("XIAOMI_API_KEY")

    if not api_key:
        print(f"[transcript] {bvid}: XIAOMI_API_KEY not set, skip ASR")
        return None

    # 获取音频URL
    audio_url = await client.get_audio_url(bvid, cid)
    if not audio_url:
        print(f"[transcript] {bvid}: no audio stream available")
        return None

    # 下载音频
    audio_data = await client.download_audio(audio_url)
    if not audio_data:
        return None

    # 切片逻辑
    if duration <= chunk_sec:
        # 单片直接转写
        text = await _call_asr(api_url, api_key, audio_data, asr_cfg.get("model", "mimo-v2.5-asr"))
        if text:
            return {"source": "asr", "text": text, "segments": 1}
    else:
        # 多片转写再拼接
        num_chunks = (duration + chunk_sec - 1) // chunk_sec
        chunk_size = len(audio_data) // num_chunks
        texts = []
        model = asr_cfg.get("model", "mimo-v2.5-asr")

        for i in range(num_chunks):
            start = i * chunk_size
            end = (i + 1) * chunk_size if i < num_chunks - 1 else len(audio_data)
            chunk_data = audio_data[start:end]

            print(f"[transcript] {bvid}: ASR chunk {i+1}/{num_chunks}")
            chunk_text = await _call_asr(api_url, api_key, chunk_data, model)
            if chunk_text:
                texts.append(chunk_text)

        if texts:
            full_text = " ".join(texts)
            return {"source": "asr", "text": full_text, "segments": num_chunks}

    return None


async def _call_asr(api_url: str, api_key: str, audio_data: bytes, model: str) -> Optional[str]:
    """调用MiMo ASR API转写单段音频。"""
    audio_b64 = base64.b64encode(audio_data).decode("utf-8")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": "mp3",
                        },
                    },
                    {
                        "type": "text",
                        "text": "请将这段音频完整转写为文字，不要遗漏、不要总结、不要添加额外内容。",
                    },
                ],
            }
        ],
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
                content = choices[0].get("message", {}).get("content", "")
                return content.strip()
        except Exception as e:
            print(f"[asr] API error: {e}")
    return None
