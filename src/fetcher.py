"""抓取调度器 — 单视频管线 + 全量抓取 + 历史补抓。"""

import asyncio
import time
from datetime import datetime, date, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional

from sqlalchemy import select, update, or_, and_, case, literal_column
from sqlalchemy.ext.asyncio import AsyncSession

from src.bilibili import BiliClient
from src.transcript import fetch_transcript
from src.analyzer import analyze_video, analyze_dynamic, generate_daily_digest, _validate_analysis, _validate_digest
from src.retry_strategy import classify_error, should_retry, is_permanent, max_retries_for
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest, PendingDynamic, DirtyDigest,
)
from src.db import async_session
from src.config import get_config, get_env

# ── 硬性时间下限：绝对业务边界，不可由配置修改 ──
_HARD_SINCE = datetime(2026, 7, 1)  # 绝对常量，backfill_since 只能 >= 此值

def _is_before_hard_since(pub_dt) -> bool:
    """发布时间是否早于硬性下限（含 None/无效时间）。"""
    return pub_dt is None or pub_dt < _HARD_SINCE


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
    # #2: 任务级重试预算——所有博主共享，防止单博主耗尽全局预算
    retry_budget = 50

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
                b_result = await _fetch_blogger(client, mid, name, retry_budget=retry_budget)
                results["bloggers"][mid] = b_result
                results["total_new"] += b_result.get("new_count", 0)
                results["total_failed"] += b_result.get("failed_count", 0)
                # #2: 扣减所有尝试（成功+失败），不只是成功数
                retry_budget -= b_result.get("retried_count", 0) + b_result.get("failed_count", 0)
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
    result = {"mid": mid, "name": "unknown", "processed": 0, "failed": 0, "attempted": 0}
    try:
        since_dt = datetime.combine(date.fromisoformat(since), dt_time())
        async with async_session() as session:
            blogger = await session.get(Blogger, mid)
            if not blogger or not blogger.enabled:
                raise ValueError(f"Enabled blogger {mid} not found")
            result["name"] = blogger.name
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
                    pub = _publish_time(v.get("created", 0))
                    if pub is None:
                        # #10: 缺失时间跳过但不阻断翻页
                        continue
                    if pub < since_dt:
                        reached_date = True
                        continue
                    async with async_session() as session:
                        old = await session.get(Video, v["bvid"])
                        if old and old.fetch_status == "ok":
                            continue
                        # Issue #6: 回填也检查重试策略——永久失败和次数耗尽不重试
                        if old:
                            err_type = classify_error(old.error_message or "", old.duration or 0)
                            if is_permanent(err_type):
                                continue
                            if not should_retry(old.retry_count or 0, err_type):
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
    except Exception as exc:
        result["error"] = str(exc)[:200]
        result["failed"] = max(result["failed"], 1)  # #11: 上游异常算至少1个失败
    return result


async def run_retry_failed(batch_limit: int = 50) -> dict:
    """独立重试：只从数据库捞失败视频，按错误类型分级处理，不扫描新视频。"""
    bloggers = await _get_enabled_bloggers()
    if not bloggers:
        return {"processed": 0, "skipped": 0, "failed": 0}
    
    result = {"processed": 0, "skipped": 0, "failed": 0, "details": []}
    interval = get_config().get("limits", {}).get("video_interval_seconds", 5)
    
    async with async_session() as session:
        # #1: 精确SQL重试资格过滤——用持久化的error_type匹配各类型上限
        # permanent(0次) / incomplete_info(2次) / llm_failed(2次) / transcript(3次) / unknown(3次)
        candidates = (await session.execute(select(Video).where(
            Video.fetch_status.in_(["failed", "pending"]),
            Video.mid.in_([b["mid"] for b in bloggers]),
            Video.publish_time >= _HARD_SINCE,
            Video.publish_time.isnot(None),
            # 永久失败直接排除
            Video.error_type.isnot(None),  # 有error_type说明出过错
            Video.error_type != "permanent",
            # #1: 精确重试上限——incomplete_info/llm_failed最多2次，其他最多3次
            or_(
                and_(Video.error_type.in_(["incomplete_info", "llm_failed"]),
                     Video.retry_count < 2),
                and_(Video.error_type.in_(["transcript", "unknown"]),
                     Video.retry_count < 3),
                Video.error_type.is_(None),  # 未出过错的也保留
            ),
        ).order_by(
            case((Video.retry_count.is_(None), 1), else_=0),
            Video.retry_count.asc(),
            Video.fetched_at.asc(),
            Video.bvid.asc(),
        ).limit(200))).scalars().all()
    # #2: Python二次筛选（精确匹配错误类型的重试上限）+ 任务级预算
    failed = []
    for v in candidates:
        if len(failed) >= batch_limit:
            break
        err_type = classify_error(v.error_message or "", v.duration or 0)
        if is_permanent(err_type) or not should_retry(v.retry_count or 0, err_type):
            continue
        failed.append(v)

    for v in failed:
        err_type = classify_error(v.error_message or "", v.duration or 0)
        # 针对性重试
        print(f"[retry] {v.bvid}: {err_type}，重试中...")
        async with _bili_client() as client:
            try:
                await _process_video(client, v.mid, _video_info(v), is_retry=True)
                result["processed"] += 1
                result["details"].append({"bvid": v.bvid, "status": "ok"})
                print(f"[retry] {v.bvid}: ✅ 成功")
            except Exception as e:
                result["failed"] += 1
                result["details"].append({"bvid": v.bvid, "status": "failed", "error": str(e)[:100]})
                print(f"[retry] {v.bvid}: ❌ {e}")
        await asyncio.sleep(interval)
    
    return result


async def _fetch_blogger(client: BiliClient, mid: int, name: str, retry_budget: int = 50) -> dict:
    cfg = get_config()
    retries = cfg.get("retry", {}).get("max_retries", 3)
    interval = cfg.get("limits", {}).get("video_interval_seconds", 5)
    result = {"name": name, "new_count": 0, "failed_count": 0, "retried_count": 0, "dynamics_count": 0}
    async with async_session() as session:
        # #1: 精确SQL重试资格过滤——用持久化的error_type匹配各类型上限
        failed = (await session.execute(select(Video).where(
            Video.mid == mid,
            Video.fetch_status.in_(["failed", "pending"]),
            Video.publish_time >= _HARD_SINCE,
            Video.publish_time.isnot(None),
            # 排除永久失败
            or_(Video.error_type.is_(None), Video.error_type != "permanent"),
            # 精确重试上限
            or_(
                and_(Video.error_type.in_(["incomplete_info", "llm_failed"]),
                     Video.retry_count < 2),
                and_(Video.error_type.in_(["transcript", "unknown"]),
                     Video.retry_count < 3),
                Video.error_type.is_(None),
            ),
        ))).scalars().all()
        # 按错误类型分级过滤重试，使用任务级预算
        retryable = []
        for v in failed:
            if len(retryable) >= retry_budget:
                break
            err_type = classify_error(v.error_message or "", v.duration or 0)
            if is_permanent(err_type):
                print(f"[fetcher] {v.bvid}: 永久失败({err_type})，跳过")
                continue
            if should_retry(v.retry_count or 0, err_type):
                retryable.append(v)
            else:
                print(f"[fetcher] {v.bvid}: {err_type} 已达重试上限({v.retry_count}/{max_retries_for(err_type)})")
        failed = retryable
        # #12: 动态抓取已停用，不查询 PendingDynamic
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
    # for d in pending:  # DISABLED: dynamics fetching paused
    #     try:
    #         await _process_dynamic(client, mid, d.payload)
    #         result["retried_count"] += 1
    #     except Exception:
    #         result["failed_count"] += 1

    # Persist every discovered video before moving the watermark. A bounded scan
    # never advances it until it reaches the prior boundary; queued rows survive crashes.
    cap = max(1, int(cfg.get("data", {}).get("first_run_cap", 20)))
    since = cfg.get("data", {}).get("backfill_since")
    since_dt = datetime.combine(date.fromisoformat(since), dt_time()) if since else None
    max_scan_pages = 50  # Issue #5: 无论水位线状态，每轮最多扫描50页
    seen, videos, complete = set(), [], False
    watermark_found = not last_bvid  # 无水位线时视为"已找到"
    for page in range(1, max_scan_pages + 1):
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
            if not watermark_found and v["bvid"] == last_bvid:
                watermark_found = True
                stop = True
                continue
            pub = _publish_time(v.get("created", 0))
            if pub is None:
                # Issue #4: 缺失时间跳过但不阻断后续视频
                continue
            if since_dt and pub < since_dt:
                stop = True
                continue
            if not stop:
                videos.append(v)
        # 首次运行（无水位线）时限制新视频数量；增量运行不受此限
        if not last_bvid and len(videos) >= cap:
            videos = videos[:cap]
            complete = True
            break
        if stop:
            complete = True
            break
    if not complete:
        # Issue #5: 水位线失效时给出明确提示
        hint = "watermark not found; use backfill" if last_bvid and not watermark_found else "use backfill"
        raise ValueError(f"Video scan limit exceeded ({max_scan_pages} pages); {hint}")
    last_processed = None
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
            # Issue #3: 记录最后成功处理的视频，用于安全推进水位线
            last_processed = v
        except Exception:
            result["failed_count"] += 1
            async with async_session() as session:
                if await session.get(Video, v["bvid"]) is None:
                    raise  # A DB failure must not move the boundary past an unqueued item.
        await asyncio.sleep(interval)
    # Issue #3: 水位线只推进到最后成功处理的位置，不跳过未处理项
    if last_processed:
        async with async_session() as session:
            wm = await session.get(FetchWatermark, mid) or FetchWatermark(mid=mid)
            wm.last_bvid = last_processed["bvid"]
            wm.last_publish_time = _publish_time(last_processed.get("created", 0))
            wm.updated_at = _utcnow()
            session.add(wm)
            await session.commit()

    newest = None  # DISABLED: dynamics fetching paused; watermark block below preserves existing last_dyn_id
    # for _ in range(1000):  # DISABLED: dynamics fetching paused
    #     data = await client.get_dynamics(mid, offset=offset)
    #     batch = data.get("items", [])
    #     stop = False
    #     for dyn in batch:
    #         dyn_id = str(dyn.get("id_str", ""))
    #         if not dyn_id:
    #             continue
    #         newest = newest or dyn_id
    #         if dyn_id == last_dyn:
    #             stop = True
    #             continue
    #         if dyn_id in tried_dyn:
    #             continue
    #         tried_dyn.add(dyn_id)
    #         async with async_session() as session:
    #             if await session.get(Dynamic, dyn_id) or await session.get(PendingDynamic, dyn_id):
    #                 continue
    #         try:
    #             await _process_dynamic(client, mid, dyn)
    #             result["dynamics_count"] += 1
    #         except Exception:
    #             result["failed_count"] += 1
    #             async with async_session() as session:
    #                 if await session.get(PendingDynamic, dyn_id) is None:
    #                     raise
    #         await asyncio.sleep(interval)
    #     if stop or not data.get("has_more"):
    #         break
    #     offset = str(data.get("offset", ""))
    #     if not offset or offset in offsets:
    #         raise ValueError("Dynamic pagination repeated; watermark retained")
    #     offsets.add(offset)
    # else:
    #     raise ValueError("Dynamic scan limit exceeded; watermark retained")
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

    # #3: 重试模式——先检查本地数据完整性，完整则跳过所有远程调用
    cid = None
    aid = vinfo.get("aid", 0) or 0
    existing_comments = existing_danmaku = None
    existing_summary = None
    use_existing_transcript = False
    existing_transcript = None
    reuse_existing_analysis = False

    if is_retry:
        async with async_session() as session:
            existing_transcript = await session.get(Transcript, bvid)
            existing_comments = (await session.execute(
                select(CommentAnalysis).where(CommentAnalysis.ref_id == bvid)
            )).scalar_one_or_none()
            existing_danmaku = (await session.execute(
                select(DanmakuAnalysis).where(DanmakuAnalysis.bvid == bvid)
            )).scalar_one_or_none()
            existing_summary = (await session.execute(
                select(Summary).where(Summary.bvid == bvid)
            )).scalar_one_or_none()
            db_video = await session.get(Video, bvid)
            if not aid:
                aid = getattr(db_video, 'aid', None) or 0

        # 已有完整转写
        if existing_transcript and existing_transcript.full_text and len(existing_transcript.full_text) > 100:
            transcript_text = existing_transcript.full_text
            transcript_result = {"source": existing_transcript.source, "text": transcript_text,
                "segments": existing_transcript.segment_count or 1}
            use_existing_transcript = True
            print(f"[fetcher] {bvid}: 使用已有转写({len(transcript_text)}字)")

        # #3: 所有数据完整 → 跳过get_video_info和所有远程调用
        has_all = (use_existing_transcript
            and existing_comments and (existing_comments.total_count or 0) > 0
            and existing_danmaku and (existing_danmaku.total_count or 0) > 0
            and existing_summary and existing_summary.summary)
        if has_all:
            reuse_existing_analysis = True
            print(f"[fetcher] {bvid}: 所有数据完整，跳过远程调用（复用已有分析）")
            # 无需cid/aid/远程字幕——直接构建分析结果
            analysis = {
                "summary": existing_summary.summary,
                "key_points": existing_summary.key_points or [],
                "sentiment": existing_summary.sentiment or "neutral",
                "sentiment_score": existing_summary.sentiment_score or 0.0,
                "risk_warnings": existing_summary.risk_warnings or [],
                "data_citations": existing_summary.data_citations or [],
                "tags": existing_summary.tags or [],
                "comment_sentiment": {
                    "bullish": existing_comments.sentiment_bullish if existing_comments else 0,
                    "bearish": existing_comments.sentiment_bearish if existing_comments else 0,
                    "neutral": existing_comments.sentiment_neutral if existing_comments else 0,
                },
                "danmaku_sentiment": {
                    "bullish": existing_danmaku.sentiment_bullish if existing_danmaku else 0,
                    "bearish": existing_danmaku.sentiment_bearish if existing_danmaku else 0,
                    "neutral": existing_danmaku.sentiment_neutral if existing_danmaku else 0,
                },
                "comment_keywords": existing_comments.keywords if existing_comments else [],
                "danmaku_keywords": existing_danmaku.keywords if existing_danmaku else [],
            }
            # 跳转到保存阶段
            async with async_session() as session:
                video = await session.get(Video, bvid)
                video.fetch_status, video.retry_count, video.error_message = "ok", 0, None
                video.error_type = None
                video.analysis_status = "completed"
                video.fetched_at = _utcnow()
                if aid:
                    video.aid = aid
                await _mark_digest_dirty(session, pub_dt)
                await session.commit()
            print(f"[fetcher] Saved {bvid}: {analysis['sentiment']} (复用)")
            return

    # 非重试或数据不完整——需要远程获取
    if not use_existing_transcript:
        # 远程获取字幕
        if not cid:
            info = await client.get_video_info(bvid)
            cid = info.get("cid") or (info.get("pages") or [{}])[0].get("cid")
            if not cid:
                raise ValueError(f"Cannot get CID for {bvid}")
            dur_sec = info.get("duration") or dur_sec
        transcript_result = await fetch_transcript(client, bvid, cid, dur_sec)
        transcript_text = transcript_result.get("text", "")

    if not transcript_text or (transcript_result.get("partial") and not use_existing_transcript):
        raise ValueError("Transcript unavailable or incomplete")

    # #5: aid仍为0时远程获取
    if not aid:
        try:
            aid = await client.get_video_aid(bvid)
        except Exception as e:
            print(f"[fetcher] {bvid}: 获取aid失败({e})")

    # 标记分析阶段开始——拿到字幕，即将调LLM
    async with async_session() as session:
        video = await session.get(Video, bvid)
        video.analysis_status = "processing"
        # #5: 持久化aid
        if aid and not video.aid:
            video.aid = aid
        await session.commit()

    comment_data = {"replies": [], "total": 0}
    danmaku_data = {"items": [], "total": 0}
    comments, danmakus = [], []

    if is_retry:
        # 部分缺失 → 正常获取缺失部分（完整的已在上面早返回）
        has_comments = existing_comments and (existing_comments.total_count or 0) > 0
        has_danmaku = existing_danmaku and (existing_danmaku.total_count or 0) > 0
        if not has_comments and aid:
            try:
                comment_data = await client.get_comments(oid=aid, oid_type=1, count=20)
            except Exception as e:
                print(f"[fetcher] {bvid}: 评论获取失败({e})，跳过")
            comments = comment_data["replies"]
        elif has_comments:
            comments = []
        if not has_danmaku:
            danmaku_data = await client.get_danmaku(cid, max_count=2000)
            danmakus = danmaku_data["items"]
        elif has_danmaku:
            danmakus = []
    else:
        if aid:
            try:
                comment_data = await client.get_comments(oid=aid, oid_type=1, count=20)
            except Exception as e:
                print(f"[fetcher] {bvid}: 评论获取失败({e})，跳过")
        comments = comment_data["replies"]
        danmaku_data = await client.get_danmaku(cid, max_count=2000)
        danmakus = danmaku_data["items"]

    analysis = await analyze_video(title, transcript_text, comments, danmakus, dur_sec)
    if analysis.get("analysis_failed") or not _validate_analysis(analysis):
        raise ValueError(f"LLM analysis failed for {bvid}")
    async with async_session() as session:
        # Issue #5: 使用已有转写时不覆盖
        if not use_existing_transcript:
            await session.merge(Transcript(bvid=bvid, source=transcript_result["source"],
                full_text=transcript_text, segment_count=transcript_result["segments"]))
        fields = ("summary", "key_points", "sentiment", "sentiment_score", "risk_warnings", "data_citations", "tags")
        await session.merge(Summary(bvid=bvid, **{key: analysis[key] for key in fields}))
        # Issue #4: 仅在有新抓取数据时覆盖 CommentAnalysis/DanmakuAnalysis
        # retry 模式且已有完整数据时保留原值不覆盖
        cs, ds = analysis["comment_sentiment"], analysis["danmaku_sentiment"]
        if not (is_retry and existing_comments and (existing_comments.total_count or 0) > 0):
            await session.merge(CommentAnalysis(ref_id=bvid, ref_type="video", total_count=comment_data["total"],
                fetched_count=len(comments), sentiment_bullish=cs["bullish"], sentiment_bearish=cs["bearish"],
                sentiment_neutral=cs["neutral"], hot_comments=[c.get("content", {}).get("message", "") for c in comments[:5]],
                keywords=analysis["comment_keywords"]))
        if not (is_retry and existing_danmaku and (existing_danmaku.total_count or 0) > 0):
            await session.merge(DanmakuAnalysis(bvid=bvid, total_count=danmaku_data["total"], sampled_count=len(danmakus),
                sentiment_bullish=ds["bullish"], sentiment_bearish=ds["bearish"], sentiment_neutral=ds["neutral"],
                keywords=analysis["danmaku_keywords"]))
        video = await session.get(Video, bvid)
        video.title, video.duration, video.view_count = title, dur_sec, vinfo.get("play", 0)
        video.fetch_status, video.retry_count, video.error_message = "ok", 0, None
        video.error_type = None  # #1: 成功后清除错误类型
        video.analysis_status = "completed"
        video.fetched_at = _utcnow()
        # #5: 持久化aid
        if aid:
            video.aid = aid
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
                    Video.publish_time < end,
                    Video.publish_time >= _HARD_SINCE))).all()  # Issue #2: 绝对日期下限
            videos = [{"bvid": v.bvid, "title": v.title or "", "summary": s.summary or "",
                "sentiment": s.sentiment or "neutral", "key_points": s.key_points or []} for v, s in rows]
            scores.extend(s.sentiment_score for _, s in rows if s.sentiment_score is not None)
            dyns = (await session.execute(select(Dynamic).where(Dynamic.mid == b["mid"],
                Dynamic.publish_time >= start, Dynamic.publish_time < end,
                Dynamic.publish_time >= _HARD_SINCE))).scalars().all()  # Issue #2: 绝对日期下限
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
    """将时间戳转为 naive datetime；0/None 返回 None（不伪装为当前时间）。"""
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _video_info(video):
    return {"bvid": video.bvid, "title": video.title, "length": video.duration or 0,
        "created": video.publish_time.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() if video.publish_time else 0,
        "play": video.view_count or 0, "aid": getattr(video, 'aid', None) or 0}


async def _process_video(client, mid, vinfo, is_retry=False):
    bvid = vinfo.get("bvid")
    if not bvid:
        raise ValueError("Missing bvid")
    # ── Issue #1 兜底：所有入口（扫描/回填/重试/补偿）最终都走这里 ──
    pub_dt = _publish_time(vinfo.get("created", 0))
    if _is_before_hard_since(pub_dt):
        raise ValueError(
            f"Rejected: publish_time {pub_dt} is before hard limit {_HARD_SINCE.date()}"
        )
    async with async_session() as session:
        video = await session.get(Video, bvid)
        if not video:
            video = Video(bvid=bvid, mid=mid, title=vinfo.get("title", ""),
                publish_time=_publish_time(vinfo.get("created", 0)), retry_count=0)
            session.add(video)
        video.fetch_status = "pending"
        video.analysis_status = "pending"
        await session.commit()
    try:
        await _process_video_core(client, mid, vinfo, is_retry)
    except (Exception, asyncio.CancelledError) as exc:
        async with async_session() as session:
            video = await session.get(Video, bvid)
            video.fetch_status = "failed"
            video.error_message = (str(exc) or "Task interrupted")[:500]
            # #1: 持久化错误类型——SQL精确过滤重试资格
            video.error_type = classify_error(video.error_message, vinfo.get("length", 0) or 0)
            # 区分：LLM分析失败 vs 字幕/抓取失败
            if "LLM analysis failed" in str(exc) or video.analysis_status == "processing":
                video.analysis_status = "failed"
            else:
                video.analysis_status = "pending"
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
    if publish_time is None:
        return
    day = publish_time.date() + timedelta(days=1 if publish_time.hour >= _cutoff_hour() else 0)
    existing = await session.get(DirtyDigest, day)
    if existing is None:
        session.add(DirtyDigest(digest_date=day))
    elif (existing.retry_count or 0) > 0:
        # #9: 新内容到达时重置重试计数，允许重新生成日报
        existing.retry_count = 0
        existing.error_message = None


async def regenerate_dirty_digests(include_latest=False):
    latest = _latest_closed_date()
    hard_since_date = _HARD_SINCE.date()
    max_per_round = 5  # Issue #10: 每轮最多处理5个dirty日期
    async with async_session() as session:
        if include_latest and latest >= hard_since_date and await session.get(DirtyDigest, latest) is None:
            session.add(DirtyDigest(digest_date=latest))
            await session.commit()
        dates = (await session.execute(select(DirtyDigest.digest_date).where(
            DirtyDigest.digest_date <= latest,
            DirtyDigest.digest_date >= hard_since_date,  # Issue #2: 绝对日期下限
            DirtyDigest.retry_count < 5,  # Issue #10: 失败5次后终止
        ).order_by(DirtyDigest.digest_date).limit(max_per_round))).scalars().all()
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
                row.retry_count = (row.retry_count or 0) + 1
                await session.commit()
    return result
