"""字幕/转写模块 — 三层降级链：CC字幕 → ai-zh字幕 → MiMo ASR。"""

import asyncio
import base64
import json
import tempfile
import os
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
    # Issue #7: duration 缺失或为0时拒绝进入ASR（防止绕过60分钟限制）
    if not duration or duration <= 0:
        print(f"[transcript] {bvid}: duration={duration} 无效，跳过ASR")
        return {"source": "skipped", "text": "", "segments": 0}
    # 60分钟以上无字幕的视频跳过ASR——成本高且直播回放质量低
    if duration > 3600:
        print(f"[transcript] {bvid}: {duration}s (>{60}min) 无字幕，跳过ASR")
        return {"source": "skipped", "text": "", "segments": 0}
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


async def _probe_duration(audio_path: str) -> float:
    """用ffprobe获取实际音频时长（秒）。失败返回0。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return float(stdout.decode().strip())
    except Exception:
        return 0.0


async def _asr_transcribe(client: BiliClient, bvid: str, cid: int, duration: int) -> Optional[dict]:
    cfg = get_config().get("asr", {})
    key = get_env("XIAOMI_API_KEY")
    if not key:
        return None
    url = await client.get_audio_url(bvid, cid)
    if not url:
        return None
    audio_path = await client.download_audio(url)
    if not audio_path:
        return None
    # #5: 全链路文件路径化——转码、分片、上传均基于文件，不整体读入内存
    mp3_path = None
    try:
        mp3_path = await _to_mp3_path(audio_path)
        # #7: 用ffprobe验证实际时长，防止B站元数据虚报
        actual_duration = await _probe_duration(mp3_path)
        if actual_duration <= 0:
            actual_duration = duration
        # #7: 硬上限——实际时长超60分钟拒绝ASR
        if actual_duration > 3600:
            print(f"[transcript] {bvid}: 实际时长{actual_duration}s (>{60}min)，跳过ASR")
            return None
        chunk_seconds = int(cfg.get("chunk_seconds", 180))
        chunks = await _split_audio_from_file(mp3_path, chunk_seconds)
        # #7: 最大分片数限制——防止单条异常内容产生大量ASR调用
        max_chunks = int(cfg.get("max_chunks", 40))  # 40片 × 3分钟 = 120分钟理论上限
        if len(chunks) > max_chunks:
            print(f"[transcript] {bvid}: 分片数{len(chunks)}超过上限{max_chunks}，截断")
            # 清理多余分片
            for excess in chunks[max_chunks:]:
                try:
                    os.unlink(excess)
                except OSError:
                    pass
            chunks = chunks[:max_chunks]
        texts = []
        for index, chunk_path in enumerate(chunks):
            try:
                with open(chunk_path, "rb") as f:
                    chunk_data = f.read()
                text = await _call_asr(cfg.get("api_url", "https://api.xiaomimimo.com/v1/chat/completions"),
                    key, chunk_data, cfg.get("model", "mimo-v2.5-asr"))
                if not text or not text.strip():
                    raise ValueError(f"ASR segment {index + 1}/{len(chunks)} failed; transcript is incomplete")
                texts.append(text.strip())
            finally:
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass
        return {"source": "asr", "text": " ".join(texts), "segments": len(chunks)}
    finally:
        for p in (audio_path, mp3_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def _to_mp3(audio: bytes) -> bytes:
    """用 ffmpeg 把任意音频转成 mp3 bytes。"""
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as src:
        src.write(audio)
        src_path = src.name
    dst_path = src_path.replace(".m4a", ".mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode == 0 and os.path.exists(dst_path):
            return open(dst_path, "rb").read()
    finally:
        for p in (src_path, dst_path):
            if os.path.exists(p):
                os.unlink(p)
    return audio  # fallback: 原样返回


async def _to_mp3_path(src_path: str) -> str:
    """#5: 文件路径版转码——不读入bytes，直接文件到文件。"""
    dst_path = src_path.rsplit(".", 1)[0] + ".mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if proc.returncode == 0 and os.path.exists(dst_path):
        return dst_path
    return src_path  # fallback


async def _split_audio_from_file(audio_path: str, chunk_seconds: int) -> list:
    """#5: 从文件分片——每片写临时文件，返回路径列表。"""
    duration_proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    stdout, _ = await duration_proc.communicate()
    try:
        total_duration = float(stdout.decode().strip())
    except (ValueError, TypeError):
        total_duration = 0.0
    if total_duration <= 0:
        return [audio_path]
    num_chunks = max(1, int(total_duration / chunk_seconds) + 1)
    chunk_paths = []
    for i in range(num_chunks):
        start = i * chunk_seconds
        fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", audio_path, "-ss", str(start), "-t", str(chunk_seconds),
            "-vn", "-acodec", "libmp3lame", "-q:a", "4", chunk_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode == 0 and os.path.getsize(chunk_path) > 0:
            chunk_paths.append(chunk_path)
        else:
            try:
                os.unlink(chunk_path)
            except OSError:
                pass
    return chunk_paths


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
