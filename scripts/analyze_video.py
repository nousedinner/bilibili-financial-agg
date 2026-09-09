#!/usr/bin/env python3
"""B站视频快速分析脚本 — 提取字幕+AI总结"""

import sys
import os
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bilibili import BiliClient
from src.config import get_env


async def resolve_bvid(url_or_bvid: str) -> str:
    """从URL或BV号提取bvid。"""
    # 已经是BV号
    bv_match = re.search(r'(BV[a-zA-Z0-9]{10})', url_or_bvid)
    if bv_match:
        return bv_match.group(1)
    
    # 短链接，跟踪重定向
    if 'b23.tv' in url_or_bvid:
        try:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-L', '-s', '-o', '/dev/null', '-w', '%{url_effective}',
                url_or_bvid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            final_url = stdout.decode().strip()
            bv_match = re.search(r'(BV[a-zA-Z0-9]{10})', final_url)
            if bv_match:
                return bv_match.group(1)
        except Exception as e:
            print(f"短链接解析失败: {e}", file=sys.stderr)
    
    return None


async def get_subtitles_direct(bvid: str, cid: int, sessdata: str) -> str:
    """直接用curl获取字幕（绕过bilibili.py的httpx限制）。"""
    import time
    
    # 获取字幕列表
    wts = int(time.time())
    url = f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}&wts={wts}"
    cookie = f"SESSDATA={sessdata}" if sessdata else None
    
    cmd = ["curl", "-s", 
           "-H", "Referer: https://www.bilibili.com/",
           "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    if cookie:
        cmd += ["-b", cookie]
    cmd.append(url)
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    
    try:
        data = json.loads(stdout)
        subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
        print(f"找到 {len(subtitles)} 个字幕", file=sys.stderr)
        
        for sub in subtitles:
            lang = sub.get("lan", "")
            subtitle_url = sub.get("subtitle_url", "")
            print(f"字幕语言: {lang}", file=sys.stderr)
            
            if subtitle_url and ("ai-zh" in lang or lang.startswith("zh")):
                # 下载字幕内容
                full_url = f"https:{subtitle_url}" if subtitle_url.startswith("//") else subtitle_url
                dl_cmd = ["curl", "-s", "-H", "Referer: https://www.bilibili.com/", full_url]
                dl_proc = await asyncio.create_subprocess_exec(
                    *dl_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                dl_stdout, _ = await asyncio.wait_for(dl_proc.communicate(), timeout=20)
                
                sub_data = json.loads(dl_stdout)
                body = sub_data.get("body", [])
                text = " ".join([item.get("content", "") for item in body])
                if text and len(text) > 50:
                    print(f"字幕提取成功: {len(text)} 字符", file=sys.stderr)
                    return text
    except Exception as e:
        print(f"字幕解析失败: {e}", file=sys.stderr)
    
    # 备选方案：直接尝试下载AI字幕（如果知道格式）
    # 这需要更多的信息，暂时跳过
    
    return None


async def analyze_video(url_or_bvid: str, output_file: str = None) -> dict:
    """分析B站视频，返回结构化结果。"""
    
    # 1. 解析BV号
    print(f"解析URL: {url_or_bvid}", file=sys.stderr)
    bvid = await resolve_bvid(url_or_bvid)
    if not bvid:
        return {"error": "无法解析BV号，请检查URL格式"}
    print(f"BV号: {bvid}", file=sys.stderr)
    
    # 2. 获取视频信息
    sessdata = get_env("SESSDATA")
    async with BiliClient(sessdata=sessdata) as client:
        data = await client.get_video_info(bvid)
        if not data:
            return {"error": "获取视频信息失败"}
        
        if not data.get("title"):
            return {"error": f"视频不存在或已被删除: {bvid}"}
        
        cid = data.get("cid")
        duration = data.get("duration", 0)
        aid = data.get("aid")
        title = data.get("title", "未知标题")
        up_name = data.get("owner", {}).get("name", "未知UP主")
        view_count = data.get("stat", {}).get("view", 0)
        like_count = data.get("stat", {}).get("like", 0)
        
        print(f"标题: {title}", file=sys.stderr)
        print(f"UP主: {up_name}", file=sys.stderr)
        print(f"时长: {duration//60}分{duration%60}秒", file=sys.stderr)
        
        # 3. 提取字幕
        print("提取字幕中...", file=sys.stderr)
        transcript_text = None
        
        # 方法1：直接用curl获取AI字幕
        transcript_text = await get_subtitles_direct(bvid, cid, sessdata)
        
        # 方法2：用bilibili.py的fetch_transcript（包含ASR fallback）
        if not transcript_text or len(transcript_text) < 50:
            print("尝试ASR转录...", file=sys.stderr)
            from src.transcript import fetch_transcript
            transcript_result = await fetch_transcript(client, bvid, cid, duration)
            if transcript_result and transcript_result.get("text"):
                transcript_text = transcript_result["text"]
        
        if not transcript_text:
            return {
                "error": "无法提取字幕",
                "video_info": {
                    "bvid": bvid,
                    "title": title,
                    "up_name": up_name,
                    "duration": duration
                }
            }
        
        print(f"字幕长度: {len(transcript_text)} 字符", file=sys.stderr)
        
        # 4. 获取热门评论（可选）
        comments = []
        if aid:
            try:
                comments_response = await client.get_comments(aid, count=10)
                comments = comments_response["replies"]
            except Exception:
                pass
    
    # 5. 构建输出
    result = {
        "bvid": bvid,
        "title": title,
        "up_name": up_name,
        "duration": duration,
        "duration_str": f"{duration//60}分{duration%60}秒",
        "view_count": view_count,
        "like_count": like_count,
        "transcript_length": len(transcript_text),
        "transcript": transcript_text,
        "comments_count": len(comments),
        "url": f"https://www.bilibili.com/video/{bvid}"
    }
    
    # 保存到文件
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已保存到: {output_file}", file=sys.stderr)
    
    return result


def main():
    if len(sys.argv) < 2:
        print("用法: python analyze_video.py <bilibili_url_or_bvid> [output.json]")
        print("示例: python analyze_video.py https://www.bilibili.com/video/BV1xxxxxxx")
        print("示例: python analyze_video.py BV1xxxxxxx result.json")
        sys.exit(1)
    
    url_or_bvid = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    result = asyncio.run(analyze_video(url_or_bvid, output_file))
    
    # 输出JSON到stdout
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
