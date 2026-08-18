"""抓取调度器 — 单视频管线 + 全量抓取 + 历史补抓。"""

import asyncio
import time
from datetime import datetime, date, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.bilibili import BiliClient
from src.transcript import fetch_transcript
from src.analyzer import analyze_video, analyze_dynamic, generate_daily_digest
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest,
)
from src.db import async_session
from src.config import get_config, get_env


def _utcnow() -> datetime:
    """返回当前时间（naive，MySQL CST时区）。"""
    return datetime.now().replace(tzinfo=None)


def _bili_client() -> BiliClient:
    return BiliClient(sessdata=get_env("SESSDATA"))


async def _get_enabled_bloggers() -> list:
    """从数据库读取启用的博主列表。"""
    async with async_session() as db_session:
        stmt = select(Blogger).where(Blogger.enabled == True)
        db_bloggers = (await db_session.execute(stmt)).scalars().all()
        return [{"mid": b.mid, "name": b.name, "tags": b.tags, "enabled": True} for b in db_bloggers]


async def run_fetch_only() -> dict:
    """只抓取，不生成汇总。用于12:00和21:00的定时任务。"""
    bloggers = await _get_enabled_bloggers()

    if not bloggers:
        print("[fetcher] 没有启用的博主，跳过抓取")
        return {"total_new": 0, "total_failed": 0, "bloggers": {}}

    results = {"total_new": 0, "total_failed": 0, "bloggers": {}}

    async with _bili_client() as client:
        # Validate cookie
        cookie_status = await client.validate_sessdata()
        if not cookie_status["valid"]:
            print("[fetcher] SESSDATA无效，仅使用不需要cookie的接口")

        for blogger in bloggers:
            if not blogger.get("enabled", True):
                continue

            mid = blogger["mid"]
            name = blogger.get("name", str(mid))
            print(f"[fetcher] 处理博主: {name} (mid={mid})")

            try:
                b_result = await _fetch_blogger(client, mid, name)
                results["bloggers"][mid] = b_result
                results["total_new"] += b_result.get("new_count", 0)
                results["total_failed"] += b_result.get("failed_count", 0)
            except Exception as e:
                print(f"[fetcher] 博主 {name} 抓取失败: {e}")
                results["bloggers"][mid] = {"error": str(e)}

    return results


async def run_daily_fetch() -> dict:
    """兼容旧接口：抓取 + 汇总。"""
    results = await run_fetch_only()
    bloggers = await _get_enabled_bloggers()
    await _generate_digest(bloggers, results)
    return results


async def run_backfill(mid: int, since: str, cap: int = 20) -> dict:
    """历史补抓：从since日期开始，最多抓cap条。"""
    cfg = get_config()
    bloggers = cfg.get("bloggers", [])
    blogger = next((b for b in bloggers if b["mid"] == mid), None)

    if not blogger:
        return {"error": f"blogger mid={mid} not found in config"}

    name = blogger.get("name", str(mid))
    results = {"mid": mid, "name": name, "processed": 0, "failed": 0}

    async with _bili_client() as client:
        # Fetch video list
        page = 1
        processed = 0
        since_dt = datetime.strptime(since, "%Y-%m-%d")

        while processed < cap:
            vlist = await client.get_video_list(mid, page=page, page_size=30)
            videos = vlist.get("list", {}).get("vlist", [])

            if not videos:
                break

            for v in videos:
                if processed >= cap:
                    break

                pub_ts = v.get("created", 0)
                pub_dt = datetime.fromtimestamp(pub_ts)

                if pub_dt < since_dt:
                    continue  # Skip old videos

                bvid = v.get("bvid", "")
                if not bvid:
                    continue

                # Check if already processed
                async with async_session() as session:
                    existing = await session.get(Video, bvid)
                    if existing and existing.fetch_status == "ok":
                        continue

                try:
                    await _process_video(client, mid, v)
                    processed += 1
                    results["processed"] = processed
                except Exception as e:
                    print(f"[backfill] {bvid} failed: {e}")
                    results["failed"] += 1

                # Rate limit
                await asyncio.sleep(3)

            page += 1

    return results


async def _fetch_blogger(client: BiliClient, mid: int, name: str) -> dict:
    """抓取单个博主的新内容。优先重试失败的视频。"""
    result = {"name": name, "new_count": 0, "skipped_count": 0, "failed_count": 0, "retried_count": 0, "videos": [], "dynamics": []}

    # Step 1: 优先重试失败的视频（retry_count < 3）
    max_retries = get_config().get("retry", {}).get("max_retries", 3)
    async with async_session() as session:
        stmt = select(Video).where(
            Video.mid == mid,
            Video.fetch_status == "failed",
            Video.retry_count < max_retries,
        ).order_by(Video.retry_count.asc())
        failed_videos = (await session.execute(stmt)).scalars().all()

    if failed_videos:
        print(f"[fetcher] {name}: 找到{len(failed_videos)}个失败视频，优先重试")
        for video in failed_videos:
            try:
                # 构造vinfo字典用于_process_video
                vinfo = {
                    "bvid": video.bvid,
                    "title": video.title,
                    "created": int(video.publish_time.timestamp()) if video.publish_time else 0,
                    "play": video.view_count or 0,
                    "length": video.duration or 0,
                }
                await _process_video(client, mid, vinfo, is_retry=True)
                result["retried_count"] += 1
                result["videos"].append(video.bvid)
            except Exception as e:
                print(f"[fetcher] ❌ 重试 {video.bvid} 失败: {e}")
                result["failed_count"] += 1
            await asyncio.sleep(3)

    # Get watermark
    async with async_session() as session:
        wm = await session.get(FetchWatermark, mid)
        last_bvid = wm.last_bvid if wm else None
        last_dyn_id = wm.last_dyn_id if wm else None

    # Fetch new videos
    vlist = await client.get_video_list(mid, page=1, page_size=30)
    videos = vlist.get("list", {}).get("vlist", [])
    cfg = get_config()
    first_run_cap = cfg.get("data", {}).get("first_run_cap", 20)
    video_interval = cfg.get("limits", {}).get("video_interval_seconds", 5)

    new_videos = []
    backfill_since = cfg.get("data", {}).get("backfill_since", "2026-07-01")
    since_dt = datetime.strptime(backfill_since, "%Y-%m-%d") if backfill_since else None

    # Batch check: which bvids already exist with fetch_status='ok' OR have a Summary
    # (Summary existence is a more reliable dedup signal than fetch_status alone)
    bvids_in_list = [v.get("bvid", "") for v in videos if v.get("bvid")]
    already_done = set()
    if bvids_in_list:
        async with async_session() as session:
            stmt = select(Video.bvid).where(
                Video.bvid.in_(bvids_in_list),
                Video.fetch_status == "ok",
            )
            already_done = set((await session.execute(stmt)).scalars().all())

            # Also check summaries — if a summary exists, the video was fully processed
            # even if fetch_status wasn't updated (e.g., previous container crashed)
            sum_stmt = select(Summary.bvid).where(Summary.bvid.in_(bvids_in_list))
            sum_done = set((await session.execute(sum_stmt)).scalars().all())
            already_done |= sum_done

    for v in videos:
        bvid = v.get("bvid", "")
        if bvid == last_bvid:
            break  # Reached watermark
        if bvid in already_done:
            result["skipped_count"] += 1
            continue  # Already processed — skip
        # 日期过滤：只处理 backfill_since 之后的视频
        pub_ts = v.get("created", 0)
        if since_dt and pub_ts and datetime.fromtimestamp(pub_ts) < since_dt:
            continue  # 跳过太旧的视频
        new_videos.append(v)
        if not last_bvid and len(new_videos) >= first_run_cap:
            break  # First run: limit

    if result["skipped_count"] > 0:
        print(f"[fetcher] {name}: 跳过{result['skipped_count']}条已有视频，待处理{len(new_videos)}条")

    for v in new_videos:
        try:
            await _process_video(client, mid, v)
            result["new_count"] += 1
            result["videos"].append(v.get("bvid", ""))
        except Exception as e:
            print(f"[fetcher] ❌ {v.get('bvid')} failed: {e}")
            result["failed_count"] += 1
        await asyncio.sleep(video_interval)

    # Update watermark — always update to the newest video in the list
    # (even if we didn't process it, to avoid re-checking old videos next run)
    if videos:
        async with async_session() as session:
            newest_bvid = videos[0].get("bvid")
            newest_pub_ts = videos[0].get("created")

            # Re-fetch watermark in this session (previous wm is detached)
            wm = await session.get(FetchWatermark, mid)
            if wm:
                wm.last_bvid = newest_bvid
                if newest_pub_ts:
                    wm.last_publish_time = datetime.fromtimestamp(newest_pub_ts)
                wm.updated_at = _utcnow()
            else:
                session.add(FetchWatermark(
                    mid=mid,
                    last_bvid=newest_bvid,
                    last_publish_time=datetime.fromtimestamp(newest_pub_ts) if newest_pub_ts else None,
                ))
            await session.commit()

    return result


async def _process_video(client: BiliClient, mid: int, vinfo: dict, is_retry: bool = False):
    """处理单个视频：字幕→评论→弹幕→LLM分析→入库。"""
    bvid = vinfo.get("bvid", "")
    title = vinfo.get("title", "")
    duration = vinfo.get("length", "")
    pub_ts = vinfo.get("created", 0)
    view_count = vinfo.get("play", 0)

    # Parse duration string "MM:SS" to seconds
    dur_sec = 0
    if isinstance(duration, str) and ":" in duration:
        parts = duration.split(":")
        dur_sec = int(parts[0]) * 60 + int(parts[1])
    elif isinstance(duration, int):
        dur_sec = duration

    pub_dt = datetime.fromtimestamp(pub_ts) if pub_ts else _utcnow().replace(tzinfo=None)

    # Get video info for CID
    vid_info = await client.get_video_info(bvid)
    cid = vid_info.get("cid", 0) or vid_info.get("pages", [{}])[0].get("cid", 0)

    if not cid:
        raise ValueError(f"Cannot get CID for {bvid}")

    # Store video record
    async with async_session() as session:
        video = await session.get(Video, bvid)
        if not video:
            video = Video(
                bvid=bvid, mid=mid, title=title, duration=dur_sec,
                publish_time=pub_dt, view_count=view_count,
                fetch_status="pending",
                retry_count=0,
            )
            session.add(video)
        else:
            video.title = title
            video.duration = dur_sec
            video.view_count = view_count
            if is_retry:
                video.fetch_status = "pending"  # 重试时重置状态
        await session.commit()

    # Step 1: Transcript
    transcript_result = await fetch_transcript(client, bvid, cid, dur_sec)
    transcript_text = transcript_result.get("text", "")

    # Step 2: Comments + Danmaku
    comments = await client.get_comments(oid=vid_info.get("aid", 0), oid_type=1, count=20)
    danmakus = await client.get_danmaku(cid, max_count=2000)

    # Step 3: LLM Analysis
    analysis = await analyze_video(title, transcript_text, comments, danmakus, dur_sec)

    # Step 4: Save everything to DB
    try:
        async with async_session() as session:
            # Transcript
            if transcript_text:
                existing_tr = await session.get(Transcript, bvid)
                if existing_tr:
                    existing_tr.source = transcript_result["source"]
                    existing_tr.full_text = transcript_text
                    existing_tr.segment_count = transcript_result["segments"]
                else:
                    session.add(Transcript(
                        bvid=bvid,
                        source=transcript_result["source"],
                        full_text=transcript_text,
                        segment_count=transcript_result["segments"],
                    ))

            # Summary
            existing_sum = await session.get(Summary, bvid)
            if existing_sum:
                existing_sum.summary = analysis["summary"]
                existing_sum.key_points = analysis["key_points"]
                existing_sum.sentiment = analysis["sentiment"]
                existing_sum.sentiment_score = analysis["sentiment_score"]
                existing_sum.risk_warnings = analysis["risk_warnings"]
                existing_sum.data_citations = analysis["data_citations"]
                existing_sum.tags = analysis["tags"]
            else:
                session.add(Summary(
                    bvid=bvid,
                    summary=analysis["summary"],
                    key_points=analysis["key_points"],
                    sentiment=analysis["sentiment"],
                    sentiment_score=analysis["sentiment_score"],
                    risk_warnings=analysis["risk_warnings"],
                    data_citations=analysis["data_citations"],
                    tags=analysis["tags"],
                ))

            # Comment analysis
            cs = analysis.get("comment_sentiment", {})
            await session.merge(CommentAnalysis(
                ref_id=bvid,
                ref_type="video",
                total_count=len(comments),
                fetched_count=len(comments),
                sentiment_bullish=cs.get("bullish", 0),
                sentiment_bearish=cs.get("bearish", 0),
                sentiment_neutral=cs.get("neutral", 0),
                hot_comments=[c.get("content", {}).get("message", "") for c in comments[:5]],
                keywords=analysis.get("comment_keywords", []),
            ))

            # Danmaku analysis
            ds = analysis.get("danmaku_sentiment", {})
            await session.merge(DanmakuAnalysis(
                bvid=bvid,
                total_count=len(danmakus),
                sampled_count=len(danmakus),
                sentiment_bullish=ds.get("bullish", 0),
                sentiment_bearish=ds.get("bearish", 0),
                sentiment_neutral=ds.get("neutral", 0),
                keywords=analysis.get("danmaku_keywords", []),
            ))

            # Mark video as ok
            video = await session.get(Video, bvid)
            if video:
                video.fetch_status = "ok"
                video.retry_count = 0  # 成功后重置重试计数

            await session.commit()
    except Exception as e:
        # 失败时更新retry_count
        print(f"[fetcher] ❌ {bvid} DB写入失败: {e}")
        async with async_session() as session:
            video = await session.get(Video, bvid)
            if video:
                video.fetch_status = "failed"
                video.error_message = str(e)[:500]
                video.retry_count = (video.retry_count or 0) + 1
                await session.commit()
        raise

    print(f"[fetcher] ✅ {bvid} ({title[:30]}) — {analysis['sentiment']}")


async def _process_dynamic(client: BiliClient, mid: int, dyn_data: dict):
    """处理单条动态。兼容桌面端modules列表格式。"""
    dyn_id = str(dyn_data.get("id_str", ""))

    # Extract modules — desktop endpoint returns list, others return dict
    raw_modules = dyn_data.get("modules", [])

    # Normalize to a single merged dict
    modules = {}
    if isinstance(raw_modules, list):
        for m in raw_modules:
            if isinstance(m, dict):
                modules.update(m)
    elif isinstance(raw_modules, dict):
        modules = raw_modules

    # Extract text content
    content_text = ""
    mod_dynamic = modules.get("module_dynamic", {})
    desc = mod_dynamic.get("desc")
    if isinstance(desc, dict):
        content_text = desc.get("text", "")
    elif isinstance(desc, str):
        content_text = desc

    if not content_text:
        # Desktop endpoint uses dyn_archive / dyn_draw / dyn_article directly
        for key in ("dyn_archive", "dyn_article", "dyn_draw"):
            sub = mod_dynamic.get(key)
            if sub and isinstance(sub, dict):
                prefix = {"dyn_archive": "[视频]", "dyn_article": "[专栏]", "dyn_draw": "[图文]"}[key]
                content_text = prefix + " " + sub.get("title", sub.get("desc", ""))
                break

        # Fallback: standard major field
        if not content_text:
            major = mod_dynamic.get("major") or {}
            if isinstance(major, dict):
                mtype = major.get("type", "")
                if "ARCHIVE" in mtype:
                    content_text = "[视频] " + (major.get("archive") or {}).get("title", "")
                elif "ARTICLE" in mtype:
                    content_text = "[专栏] " + (major.get("article") or {}).get("title", "")
                elif "DRAW" in mtype:
                    content_text = "[图文] " + str((major.get("draw") or {}).get("desc", ""))

    if not content_text:
        return

    # Get publish time
    pub_ts = 0
    author_mod = modules.get("module_author", {})
    if isinstance(author_mod, dict):
        pub_ts = author_mod.get("pub_ts", 0)
    pub_dt = datetime.fromtimestamp(pub_ts) if pub_ts else _utcnow().replace(tzinfo=None)

    # LLM Analysis
    analysis = await analyze_dynamic(content_text)

    # Save to DB
    async with async_session() as session:
        existing = await session.get(Dynamic, dyn_id)
        if existing:
            existing.content = content_text
            existing.summary = analysis.get("summary", "")
            existing.sentiment = analysis.get("sentiment", "neutral")
            existing.tags = analysis.get("tags", [])
        else:
            session.add(Dynamic(
                dyn_id=dyn_id,
                mid=mid,
                content=content_text,
                publish_time=pub_dt,
                summary=analysis.get("summary", ""),
                sentiment=analysis.get("sentiment", "neutral"),
                tags=analysis.get("tags", []),
            ))
        await session.commit()

    print(f"[fetcher] ✅ dynamic {dyn_id} — {analysis.get('sentiment', 'neutral')}")


async def _generate_digest(bloggers: list, results: dict):
    """生成当日汇总并存入DB。时间范围：昨日22:00 → 今日22:00。"""
    from zoneinfo import ZoneInfo
    from datetime import time, timedelta
    tz_shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz_shanghai)
    today = now.date()
    blogger_analyses = []

    # 时间范围：昨日22:00 → 今日22:00
    yesterday_22 = datetime.combine(today, time(22, 0)).replace(tzinfo=tz_shanghai) - timedelta(days=1)
    today_22 = datetime.combine(today, time(22, 0)).replace(tzinfo=tz_shanghai)

    async with async_session() as session:
        for b in bloggers:
            if not b.get("enabled", True):
                continue
            mid = b["mid"]
            name = b.get("name", str(mid))

            # 查询时间范围内的视频
            stmt = (
                select(Video, Summary)
                .outerjoin(Summary, Video.bvid == Summary.bvid)
                .where(Video.mid == mid)
                .where(Video.publish_time >= yesterday_22)
                .where(Video.publish_time < today_22)
                .order_by(Video.publish_time.desc())
            )
            rows = (await session.execute(stmt)).all()

            videos = []
            for v, s in rows:
                videos.append({
                    "bvid": v.bvid,
                    "title": v.title or "",
                    "summary": s.summary if s else "",
                    "sentiment": s.sentiment if s else "neutral",
                    "key_points": s.key_points if s else [],
                })

            # Get dynamics in time range
            dyn_stmt = (
                select(Dynamic)
                .where(Dynamic.mid == mid)
                .where(Dynamic.publish_time >= yesterday_22)
                .where(Dynamic.publish_time < today_22)
            )
            dyns = (await session.execute(dyn_stmt)).scalars().all()

            dynamics = [
                {"summary": d.summary or "", "sentiment": d.sentiment or "neutral"}
                for d in dyns
            ]

            if videos or dynamics:
                blogger_analyses.append({
                    "mid": mid, "name": name,
                    "videos": videos, "dynamics": dynamics,
                })

    print(f"[fetcher] digest: 找到{len(blogger_analyses)}个博主的今日内容")
    for ba in blogger_analyses:
        print(f"  {ba['name']}: {len(ba['videos'])}个视频, {len(ba['dynamics'])}条动态")

    if blogger_analyses:
        digest = await generate_daily_digest(blogger_analyses)

        # Enrich with blogger list and sentiment_score
        digest["bloggers"] = [b["name"] for b in blogger_analyses]

        # Calculate average sentiment_score from individual video summaries
        scores = []
        async with async_session() as session:
            for b in blogger_analyses:
                for v in b.get("videos", []):
                    stmt = select(Summary.sentiment_score).where(Summary.bvid == v.get("bvid"))
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if row is not None:
                        scores.append(row)
            if scores:
                digest["sentiment_score"] = round(sum(scores) / len(scores), 2)
            else:
                digest["sentiment_score"] = 0.0

        async with async_session() as session:
            existing = await session.get(DailyDigest, today)
            if existing:
                existing.content = digest
                print(f"[fetcher] digest: 更新已有记录 {today}")
            else:
                session.add(DailyDigest(digest_date=today, content=digest))
                print(f"[fetcher] digest: 创建新记录 {today}")
            await session.commit()
    else:
        print(f"[fetcher] digest: 今日无内容，跳过生成")

    print(f"[fetcher] ✅ 每日汇总生成: {today}")
