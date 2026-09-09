"""抓取调度器 — 单视频管线 + 全量抓取 + 历史补抓。"""

import asyncio
import time
from datetime import datetime, date, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional

from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.bilibili import BiliClient
from src.transcript import fetch_transcript
from src.analyzer import analyze_video, analyze_dynamic, generate_daily_digest, _validate_analysis, _validate_digest
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest, PendingDynamic, DirtyDigest,
)
from src.db import async_session
from src.config import get_config, get_env


def _utcnow() -> datetime:
    """返回当前时间（naive，MySQL CST时区）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


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
                results["total_failed"] += 1

    return results


async def run_daily_fetch() -> dict:
    """兼容旧接口：抓取 + 汇总。"""
    results = await run_fetch_only()
    bloggers = await _get_enabled_bloggers()
    await _generate_digest(bloggers, results)
    return results


async def run_backfill(mid: int, since: str, cap: int = 20) -> dict:
    since_dt = datetime.combine(date.fromisoformat(since), dt_time())
    async with async_session() as session:
        blogger = await session.get(Blogger, mid)
        if not blogger or not blogger.enabled:
            raise ValueError(f"Enabled blogger {mid} not found")
    result = {"mid": mid, "name": blogger.name, "processed": 0, "failed": 0, "attempted": 0}
    seen = set()
    async with _bili_client() as client:
        for page in range(1, 1001):
            data = await client.get_video_list(mid, page=page, page_size=30)
            batch = data.get("list", {}).get("vlist", [])
            if not batch:
                break
            fresh = [v for v in batch if v.get("bvid") and v["bvid"] not in seen]
            if not fresh:
                raise ValueError("Video pagination repeated")
            reached_date = False
            for v in fresh:
                seen.add(v["bvid"])
                if _publish_time(v.get("created", 0)) < since_dt:
                    reached_date = True
                    continue
                async with async_session() as session:
                    old = await session.get(Video, v["bvid"])
                    if old and old.fetch_status == "ok":
                        continue
                if result["attempted"] >= cap:
                    return result
                result["attempted"] += 1
                try:
                    await _process_video(client, mid, v)
                    result["processed"] += 1
                except Exception:
                    result["failed"] += 1
                await asyncio.sleep(get_config().get("limits", {}).get("video_interval_seconds", 5))
            if reached_date or result["attempted"] >= cap:
                break
        else:
            raise ValueError("Backfill pagination limit exceeded")
    return result


async def _fetch_blogger(client: BiliClient, mid: int, name: str) -> dict:
    cfg = get_config()
    retries = cfg.get("retry", {}).get("max_retries", 3)
    interval = cfg.get("limits", {}).get("video_interval_seconds", 5)
    result = {"name": name, "new_count": 0, "failed_count": 0, "retried_count": 0, "dynamics_count": 0}
    async with async_session() as session:
        failed = (await session.execute(select(Video).where(Video.mid == mid,
            Video.fetch_status.in_(["failed", "pending"]), Video.retry_count < retries))).scalars().all()
        pending = (await session.execute(select(PendingDynamic).where(PendingDynamic.mid == mid,
            PendingDynamic.retry_count < retries))).scalars().all()
        wm = await session.get(FetchWatermark, mid)
        last_bvid, last_dyn = (wm.last_bvid, wm.last_dyn_id) if wm else (None, None)
    attempted = set()
    for v in failed:
        attempted.add(v.bvid)
        try:
            await _process_video(client, mid, _video_info(v), is_retry=True)
            result["retried_count"] += 1
        except Exception:
            result["failed_count"] += 1
        await asyncio.sleep(interval)
    for d in pending:
        try:
            await _process_dynamic(client, mid, d.payload)
            result["retried_count"] += 1
        except Exception:
            result["failed_count"] += 1

    # Persist every discovered video before moving the watermark. A bounded scan
    # never advances it until it reaches the prior boundary; queued rows survive crashes.
    cap = max(1, int(cfg.get("data", {}).get("first_run_cap", 20)))
    since = cfg.get("data", {}).get("backfill_since")
    since_dt = datetime.combine(date.fromisoformat(since), dt_time()) if since else None
    seen, videos, complete = set(), [], False
    for page in range(1, 1001):
        data = await client.get_video_list(mid, page=page, page_size=30)
        batch = data.get("list", {}).get("vlist", [])
        if not batch:
            complete = True
            break
        fresh = [v for v in batch if v.get("bvid") and v["bvid"] not in seen]
        if not fresh:
            raise ValueError("Video pagination repeated; watermark retained")
        stop = False
        for v in fresh:
            seen.add(v["bvid"])
            if v["bvid"] == last_bvid:
                stop = True
                continue
            if since_dt and _publish_time(v.get("created", 0)) < since_dt:
                stop = True
                continue
            if not stop:
                videos.append(v)
        if not last_bvid and len(videos) >= cap:
            videos = videos[:cap]
            complete = True  # Explicit first-run scope; older content belongs to backfill.
            break
        if stop:
            complete = True
            break
    if not complete:
        raise ValueError("Video scan limit exceeded; watermark retained, use backfill")
    for v in videos:
        if v["bvid"] in attempted:
            continue
        async with async_session() as session:
            old = await session.get(Video, v["bvid"])
        if old:  # Failed/pending rows are handled by the retry queue above.
            continue
        try:
            await _process_video(client, mid, v)
            result["new_count"] += 1
        except Exception:
            result["failed_count"] += 1
            async with async_session() as session:
                if await session.get(Video, v["bvid"]) is None:
                    raise  # A DB failure must not move the boundary past an unqueued item.
        await asyncio.sleep(interval)
    if videos:
        async with async_session() as session:
            wm = await session.get(FetchWatermark, mid) or FetchWatermark(mid=mid)
            wm.last_bvid = videos[0]["bvid"]
            wm.last_publish_time = _publish_time(videos[0].get("created", 0))
            wm.updated_at = _utcnow()
            session.add(wm)
            await session.commit()

    offset, offsets, newest = "", set(), None
    tried_dyn = {d.dyn_id for d in pending}
    for _ in range(1000):
        data = await client.get_dynamics(mid, offset=offset)
        batch = data.get("items", [])
        stop = False
        for dyn in batch:
            dyn_id = str(dyn.get("id_str", ""))
            if not dyn_id:
                continue
            newest = newest or dyn_id
            if dyn_id == last_dyn:
                stop = True
                continue
            if dyn_id in tried_dyn:
                continue
            tried_dyn.add(dyn_id)
            async with async_session() as session:
                if await session.get(Dynamic, dyn_id) or await session.get(PendingDynamic, dyn_id):
                    continue
            try:
                await _process_dynamic(client, mid, dyn)
                result["dynamics_count"] += 1
            except Exception:
                result["failed_count"] += 1
                async with async_session() as session:
                    if await session.get(PendingDynamic, dyn_id) is None:
                        raise
            await asyncio.sleep(interval)
        if stop or not data.get("has_more"):
            break
        offset = str(data.get("offset", ""))
        if not offset or offset in offsets:
            raise ValueError("Dynamic pagination repeated; watermark retained")
        offsets.add(offset)
    else:
        raise ValueError("Dynamic scan limit exceeded; watermark retained")
    if newest:
        async with async_session() as session:
            wm = await session.get(FetchWatermark, mid) or FetchWatermark(mid=mid)
            wm.last_dyn_id = newest
            wm.updated_at = _utcnow()
            session.add(wm)
            await session.commit()
    return result


async def _process_video_core(client: BiliClient, mid: int, vinfo: dict, is_retry: bool = False):
    """Fetch and validate first; commit transcript and all analyses together."""
    bvid = vinfo["bvid"]
    title = str(vinfo.get("title") or "")[:500]
    duration = vinfo.get("length", 0)
    dur_sec = 0
    if isinstance(duration, str):
        for part in duration.split(":"):
            dur_sec = dur_sec * 60 + int(part or 0)
    else:
        dur_sec = int(duration or 0)
    pub_dt = _publish_time(vinfo.get("created", 0))
    info = await client.get_video_info(bvid)
    cid = info.get("cid") or (info.get("pages") or [{}])[0].get("cid")
    if not cid:
        raise ValueError(f"Cannot get CID for {bvid}")
    dur_sec = info.get("duration") or dur_sec
    transcript_result = await fetch_transcript(client, bvid, cid, dur_sec)
    transcript_text = transcript_result.get("text", "")
    if not transcript_text or transcript_result.get("partial"):
        raise ValueError("Transcript unavailable or incomplete")
    comment_data = await client.get_comments(oid=info.get("aid", 0), oid_type=1, count=20)
    comments = comment_data["replies"]
    danmaku_data = await client.get_danmaku(cid, max_count=2000)
    danmakus = danmaku_data["items"]
    analysis = await analyze_video(title, transcript_text, comments, danmakus, dur_sec)
    if analysis.get("analysis_failed") or not _validate_analysis(analysis):
        raise ValueError(f"LLM analysis failed for {bvid}")
    async with async_session() as session:
        await session.merge(Transcript(bvid=bvid, source=transcript_result["source"],
            full_text=transcript_text, segment_count=transcript_result["segments"]))
        fields = ("summary", "key_points", "sentiment", "sentiment_score", "risk_warnings", "data_citations", "tags")
        await session.merge(Summary(bvid=bvid, **{key: analysis[key] for key in fields}))
        cs, ds = analysis["comment_sentiment"], analysis["danmaku_sentiment"]
        await session.merge(CommentAnalysis(ref_id=bvid, ref_type="video", total_count=comment_data["total"],
            fetched_count=len(comments), sentiment_bullish=cs["bullish"], sentiment_bearish=cs["bearish"],
            sentiment_neutral=cs["neutral"], hot_comments=[c.get("content", {}).get("message", "") for c in comments[:5]],
            keywords=analysis["comment_keywords"]))
        await session.merge(DanmakuAnalysis(bvid=bvid, total_count=danmaku_data["total"], sampled_count=len(danmakus),
            sentiment_bullish=ds["bullish"], sentiment_bearish=ds["bearish"], sentiment_neutral=ds["neutral"],
            keywords=analysis["danmaku_keywords"]))
        video = await session.get(Video, bvid)
        video.title, video.duration, video.view_count = title, dur_sec, vinfo.get("play", 0)
        video.fetch_status, video.retry_count, video.error_message = "ok", 0, None
        video.fetched_at = _utcnow()
        await _mark_digest_dirty(session, pub_dt)
        await session.commit()
    print(f"[fetcher] Saved {bvid}: {analysis['sentiment']}")


async def _process_dynamic_core(client: BiliClient, mid: int, dyn_data: dict):
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
    pub_dt = _publish_time(pub_ts) if pub_ts else _utcnow().replace(tzinfo=None)

    # LLM Analysis
    analysis = await analyze_dynamic(content_text)
    if analysis.get("analysis_failed") or not isinstance(analysis.get("summary"), str) or not analysis["summary"].strip():
        raise ValueError("Dynamic analysis failed")

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
        await _mark_digest_dirty(session, pub_dt)
        await session.commit()

    print(f"[fetcher] ✅ dynamic {dyn_id} — {analysis.get('sentiment', 'neutral')}")


async def _generate_digest(bloggers: list, results: dict, target_date: date = None):
    target_date = target_date or _latest_closed_date()
    end = datetime.combine(target_date, dt_time(_cutoff_hour()))
    start = end - timedelta(days=1)
    analyses, scores = [], []
    async with async_session() as session:
        for b in bloggers:
            if not b.get("enabled", True):
                continue
            rows = (await session.execute(select(Video, Summary).join(Summary, Video.bvid == Summary.bvid)
                .where(Video.mid == b["mid"], Video.fetch_status == "ok", Video.publish_time >= start,
                    Video.publish_time < end))).all()
            videos = [{"bvid": v.bvid, "title": v.title or "", "summary": s.summary or "",
                "sentiment": s.sentiment or "neutral", "key_points": s.key_points or []} for v, s in rows]
            scores.extend(s.sentiment_score for _, s in rows if s.sentiment_score is not None)
            dyns = (await session.execute(select(Dynamic).where(Dynamic.mid == b["mid"],
                Dynamic.publish_time >= start, Dynamic.publish_time < end))).scalars().all()
            if videos or dyns:
                analyses.append({"mid": b["mid"], "name": b.get("name", str(b["mid"])), "videos": videos,
                    "dynamics": [{"summary": d.summary or "", "sentiment": d.sentiment or "neutral"} for d in dyns]})
    if analyses:
        digest = await generate_daily_digest(analyses)
        if digest.get("analysis_failed") or _validate_digest(digest).get("analysis_failed"):
            raise ValueError(f"Daily digest {target_date} analysis failed; existing content preserved")
        digest["bloggers"] = [b["name"] for b in analyses]
        digest["sentiment_score"] = round(sum(scores) / len(scores), 2) if scores else 0.0
        async with async_session() as session:
            await session.merge(DailyDigest(digest_date=target_date, content=digest))
            await session.commit()
    async with async_session() as session:
        dirty = await session.get(DirtyDigest, target_date)
        if dirty:
            await session.delete(dirty)
            await session.commit()
    return {"date": str(target_date), "generated": bool(analyses)}


def _publish_time(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) if timestamp else _utcnow()


def _video_info(video):
    return {"bvid": video.bvid, "title": video.title, "length": video.duration or 0,
        "created": video.publish_time.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() if video.publish_time else 0,
        "play": video.view_count or 0}


async def _process_video(client, mid, vinfo, is_retry=False):
    bvid = vinfo.get("bvid")
    if not bvid:
        raise ValueError("Missing bvid")
    async with async_session() as session:
        video = await session.get(Video, bvid)
        if not video:
            video = Video(bvid=bvid, mid=mid, title=vinfo.get("title", ""),
                publish_time=_publish_time(vinfo.get("created", 0)), retry_count=0)
            session.add(video)
        video.fetch_status = "pending"
        await session.commit()
    try:
        await _process_video_core(client, mid, vinfo, is_retry)
    except (Exception, asyncio.CancelledError) as exc:
        async with async_session() as session:
            video = await session.get(Video, bvid)
            video.fetch_status = "failed"
            video.error_message = (str(exc) or "Task interrupted")[:500]
            video.retry_count = (video.retry_count or 0) + 1
            await session.commit()
        raise


async def _process_dynamic(client, mid, data):
    dyn_id = str(data.get("id_str", ""))
    if not dyn_id:
        raise ValueError("Missing dynamic ID")
    async with async_session() as session:
        row = await session.get(PendingDynamic, dyn_id)
        if not row:
            session.add(PendingDynamic(dyn_id=dyn_id, mid=mid, payload=data, retry_count=0))
        await session.commit()
    try:
        await _process_dynamic_core(client, mid, data)
    except (Exception, asyncio.CancelledError) as exc:
        async with async_session() as session:
            row = await session.get(PendingDynamic, dyn_id)
            row.retry_count += 1
            row.error_message = (str(exc) or "Task interrupted")[:500]
            await session.commit()
        raise
    async with async_session() as session:
        row = await session.get(PendingDynamic, dyn_id)
        if row:
            await session.delete(row)
            await session.commit()


def _cutoff_hour():
    return int(get_config().get("scheduler", {}).get("digest_cutoff_hour", 22))


def _latest_closed_date():
    now = _utcnow()
    return now.date() if now.hour >= _cutoff_hour() else now.date() - timedelta(days=1)


async def _mark_digest_dirty(session, publish_time):
    day = publish_time.date() + timedelta(days=1 if publish_time.hour >= _cutoff_hour() else 0)
    if await session.get(DirtyDigest, day) is None:
        session.add(DirtyDigest(digest_date=day))


async def regenerate_dirty_digests(include_latest=False):
    latest = _latest_closed_date()
    async with async_session() as session:
        if include_latest and await session.get(DirtyDigest, latest) is None:
            session.add(DirtyDigest(digest_date=latest))
            await session.commit()
        dates = (await session.execute(select(DirtyDigest.digest_date).where(
            DirtyDigest.digest_date <= latest).order_by(DirtyDigest.digest_date))).scalars().all()
    bloggers = await _get_enabled_bloggers()
    result = {"generated": 0, "failed": 0}
    for day in dates:
        try:
            await _generate_digest(bloggers, {}, day)
            result["generated"] += 1
        except Exception as exc:
            result["failed"] += 1
            async with async_session() as session:
                row = await session.get(DirtyDigest, day)
                row.error_message = str(exc)[:500]
                await session.commit()
    return result
