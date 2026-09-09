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
            if text and text.strip():
                print(f"[transcript] {bvid}: CC字幕命中 ({len(text)} chars)")
                return {"source": "cc_subtitle", "text": text, "segments": 1}
    except Exception as e:
        print(f"[transcript] {bvid}: CC字幕降级失败: {e}")

    # Layer 2: AI中文字幕 (需SESSDATA)
    try:
        ai_url = await client.get_subtitle_url(bvid, cid)
        if ai_url:
            text = await _download_subtitle(ai_url)
            if text and text.strip():
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
    cfg = get_config().get("asr", {})
    key = get_env("XIAOMI_API_KEY")
    if not key:
        return None
    url = await client.get_audio_url(bvid, cid)
    if not url:
        return None
    audio = await client.download_audio(url)
    if not audio:
        return None
    chunks = await _split_audio(audio, int(cfg.get("chunk_seconds", 180)))
    texts = []
    for index, chunk in enumerate(chunks):
        text = await _call_asr(cfg.get("api_url", "https://api.xiaomimimo.com/v1/chat/completions"),
            key, chunk, cfg.get("model", "mimo-v2.5-asr"))
        if not text or not text.strip():
            raise ValueError(f"ASR segment {index + 1}/{len(chunks)} failed; transcript is incomplete")
        texts.append(text.strip())
    return {"source": "asr", "text": " ".join(texts), "segments": len(chunks)}


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
            resp.raise_for_status()
            result = resp.json()
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                return content.strip()
        except Exception as e:
            print(f"[asr] API error: {e}")
    return None


async def _split_audio(audio: bytes, seconds: int) -> list[bytes]:
    """Decode to fixed-rate PCM first, then independently encode each time slice.

    Splitting one MP3 encoder's stream carries encoder-delay/bit-reservoir state
    across boundaries and can produce a final empty segment. PCM has no such state.
    """
    if not 1 <= seconds <= 180:
        raise ValueError("ASR chunk_seconds must be between 1 and 180")
    max_pcm = int(get_config().get("asr", {}).get("max_pcm_bytes", 512 * 1024 * 1024))
    with tempfile.TemporaryDirectory(prefix="fin-asr-") as directory:
        root = Path(directory)
        source, pcm = root / "input.audio", root / "decoded.pcm"
        source.write_bytes(audio)
        await _ffmpeg("-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
            "-fs", str(max_pcm + 1), str(pcm))
        if not pcm.stat().st_size or pcm.stat().st_size > max_pcm:
            raise ValueError("Decoded audio is empty or exceeds configured size limit")
        chunks = []
        with pcm.open("rb") as stream:
            while data := stream.read(seconds * 16000 * 2):
                encoded = await _ffmpeg("-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                    "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1", input_data=data)
                if not encoded:
                    raise ValueError("Audio encoding returned an empty segment")
                chunks.append(encoded)
        return chunks


async def _ffmpeg(*args, input_data=None):
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(proc.communicate(input_data), timeout=300)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError("Audio conversion failed: " + error.decode(errors="replace")[:200])
    return output
