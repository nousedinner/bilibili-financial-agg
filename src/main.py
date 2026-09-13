"""FastAPI 应用 — REST API + 定时调度。"""

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, date
from zoneinfo import ZoneInfo
from uuid import uuid4
from typing import Literal
from fastapi.responses import JSONResponse
from typing import Optional

from fastapi import FastAPI, Query, Header, HTTPException, Depends, Body
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, text, or_, and_, literal, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_config, get_env
from src.db import init_db, async_session, engine
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest, ApiUser, FetchJob,
)
from src.fetcher import run_daily_fetch, run_fetch_only, run_backfill, run_retry_failed, _get_enabled_bloggers, _generate_digest, regenerate_dirty_digests, _process_video, _video_info, _utcnow, _HARD_SINCE


# ------------------------------------------------------------------
# Task mutex flags — prevent overlapping background tasks
# ------------------------------------------------------------------
_job_lock = asyncio.Lock()
_background_tasks = set()
_scheduler = None
_cookie_cache = (0.0, {})
_cookie_lock = asyncio.Lock()
_last_job_write_failure = None  # #7: 状态提交失败标记


# ------------------------------------------------------------------
# Lifespan: init DB + start scheduler
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # One scheduler/writer per database. A second worker fails startup rather
    # than running competing jobs; the connection keeps the MySQL lock alive.
    async with engine.connect() as owner:
        is_mysql = owner.dialect.name == "mysql"
        if is_mysql:
            acquired = (await owner.execute(text("SELECT GET_LOCK('fin_agg_scheduler', 0)"))).scalar()
            if acquired != 1:
                raise RuntimeError("Another fin-agg worker is active; run uvicorn with --workers 1")
        try:
            await _recover_interrupted()
            _start_scheduler()
            yield
        finally:
            if _scheduler:
                _scheduler.shutdown(wait=False)
            tasks = list(_background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if is_mysql:
                await owner.execute(text("SELECT RELEASE_LOCK('fin_agg_scheduler')"))
    await engine.dispose()


def _start_scheduler():
    global _scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    cfg = get_config().get("scheduler", {})
    if cfg.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("Stored publication times and digest windows use Asia/Shanghai")
    if not 0 <= int(cfg.get("digest_cutoff_hour", 22)) <= 23:
        raise ValueError("digest_cutoff_hour must be 0..23")
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    schedules = [("fetch_1", cfg.get("fetch_cron_1", "0 12 * * *"), _scheduled_fetch),
        ("fetch_2", cfg.get("fetch_cron_2", "0 21 * * *"), _scheduled_fetch),
        ("digest", cfg.get("digest_cron", cfg.get("daily_cron", "10 22 * * *")), _scheduled_digest)]
    for job_id, cron, callback in schedules:
        _scheduler.add_job(callback, CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
            id=job_id, coalesce=True, max_instances=1, misfire_grace_time=3600)
    _scheduler.start()


async def _scheduled_fetch():
    return await _run_job("scheduled_fetch", _fetch_work)


async def _scheduled_digest():
    return await _run_job("scheduled_digest", _digest_work)


app = FastAPI(
    title="B站财经博主聚合服务",
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------
# Auth dependency
# ------------------------------------------------------------------

async def require_api_key(x_api_key: Optional[str] = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少X-API-Key header")
    async with async_session() as session:
        stmt = select(ApiUser).where(ApiUser.api_key == x_api_key, ApiUser.enabled == True)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=401, detail="无效的API Key")


# ------------------------------------------------------------------
# 博主管理
# ------------------------------------------------------------------

@app.get("/api/bloggers", dependencies=[Depends(require_api_key)])
async def list_bloggers():
    async with async_session() as session:
        stmt = select(Blogger).where(Blogger.enabled == True)
        result = (await session.execute(stmt)).scalars().all()
        return {
            "code": 0,
            "data": [
                {"mid": b.mid, "name": b.name, "tags": b.tags or [], "added_at": str(b.added_at)}
                for b in result
            ],
        }


@app.post("/api/bloggers", dependencies=[Depends(require_api_key)])
async def add_blogger(name: str = Query(..., min_length=1, max_length=100, pattern=r".*\S.*"), mid: Optional[int] = Query(None, gt=0, le=9223372036854775807), tags: str = Query("", max_length=1000)):
    """添加博主 - 输入B站用户名自动搜索，或直接输入mid。"""
    from src.bilibili import BiliClient
    from src.config import get_env
    
    real_mid = mid
    real_name = name
    
    # 如果没提供mid，尝试搜索
    if not real_mid:
        sessdata = get_env("SESSDATA")
        async with BiliClient(sessdata=sessdata) as client:
            user_info = await client.search_user(name)
        
        if user_info:
            real_mid = user_info["mid"]
            real_name = user_info["name"]
        else:
            raise HTTPException(404, f"搜索不到B站用户 '{name}'，请直接提供mid参数")
    
    async with async_session() as session:
        existing = await session.get(Blogger, real_mid)
        if existing:
            existing.enabled = True
            existing.name = real_name
            existing.tags = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            session.add(Blogger(
                mid=real_mid, name=real_name,
                tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else [],
            ))
        await session.commit()
    
    return {"code": 0, "data": {"mid": real_mid, "name": real_name, "message": f"博主 {real_name} 已添加"}}


@app.delete("/api/bloggers/{mid}", dependencies=[Depends(require_api_key)])
async def remove_blogger(mid: int):
    async with async_session() as session:
        blogger = await session.get(Blogger, mid)
        if not blogger:
            raise HTTPException(404, "博主不存在")
        blogger.enabled = False
        await session.commit()
    return {"code": 0, "data": {"message": f"博主 {mid} 已禁用"}, "message": f"博主 {mid} 已禁用"}


@app.post("/api/bloggers/sync", dependencies=[Depends(require_api_key)])
async def sync_followings():
    from src.bilibili import BiliClient
    sessdata, uid = get_env("SESSDATA"), get_env("BILI_UID")
    if not sessdata or not uid.isdigit():
        raise HTTPException(400, "请配置有效的 SESSDATA 和 BILI_UID")
    async with BiliClient(sessdata=sessdata) as client:
        items = await client.get_followings(int(uid), ps=50)
    async with async_session() as session:
        existing = set((await session.execute(select(Blogger.mid).where(Blogger.enabled == True))).scalars().all())
    fresh = [{"mid": f["mid"], "name": f.get("uname") or f.get("name") or str(f["mid"]),
        "sign": f.get("sign", "")} for f in items if f["mid"] not in existing]
    return {"code": 0, "data": {"total_followings": len(items), "already_tracked": len(existing),
        "new_count": len(fresh), "new": fresh}}


class BloggerInput(BaseModel):
    mid: int = Field(gt=0, le=9223372036854775807)
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")


@app.post("/api/bloggers/sync/add", dependencies=[Depends(require_api_key)])
async def sync_add_bloggers(
    mids: Optional[str] = Query(None, max_length=10000),
    names: str = Query("", max_length=50000),
    bloggers: Optional[list[BloggerInput]] = Body(None, max_length=500),
):
    if bloggers is None:
        try:
            ids = [int(m.strip()) for m in (mids or "").split(",")]
            labels = names.split(",") if names else []
            if labels and len(labels) != len(ids):
                raise ValueError("姓名和 MID 数量不一致")
            bloggers = [BloggerInput(mid=mid, name=(labels[i].strip() if labels else "") or f"用户{mid}") for i, mid in enumerate(ids)]
        except ValueError as exc:
            raise HTTPException(400, "无效的 MID 或姓名配对") from exc
    if not bloggers or len(bloggers) > 500:
        raise HTTPException(400, "每批需要 1 到 500 位博主")
    added = []
    async with async_session() as session:
        for item in {b.mid: b for b in bloggers}.values():
            old = await session.get(Blogger, item.mid)
            if old and old.enabled:
                continue
            if old:
                old.enabled = True
                old.name = item.name
            else:
                session.add(Blogger(mid=item.mid, name=item.name, tags=[]))
            added.append({"mid": item.mid, "name": item.name, "action": "re-enabled" if old else "added"})
        await session.commit()
    return {"code": 0, "data": {"added": added}}


# ------------------------------------------------------------------
# API用户管理
# ------------------------------------------------------------------

class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50, pattern=r".*\S.*")

@app.post("/api/bootstrap")
async def bootstrap_user(req: CreateUserRequest):
    """已禁用：首次部署时创建用户后关闭此端点。"""
    raise HTTPException(403, "注册接口已关闭，请联系管理员")


@app.post("/api/users", dependencies=[Depends(require_api_key)])
async def create_user(req: CreateUserRequest):
    """创建API用户，返回API Key。"""
    api_key = secrets.token_urlsafe(32)
    async with async_session() as session:
        existing = (await session.execute(
            select(ApiUser).where(ApiUser.username == req.username)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "用户名已存在")
        session.add(ApiUser(username=req.username, api_key=api_key))
        await session.commit()
    return {"code": 0, "data": {"username": req.username, "api_key": api_key}}


@app.get("/api/users", dependencies=[Depends(require_api_key)])
async def list_users():
    """列出所有API用户。"""
    async with async_session() as session:
        users = (await session.execute(select(ApiUser))).scalars().all()
        return {
            "code": 0,
            "data": [
                {"id": u.id, "username": u.username, "enabled": u.enabled, "created_at": str(u.created_at)}
                for u in users
            ],
        }


@app.delete("/api/users/{user_id}", dependencies=[Depends(require_api_key)])
async def disable_user(user_id: int):
    """禁用API用户。"""
    async with async_session() as session:
        user = await session.get(ApiUser, user_id)
        if not user:
            raise HTTPException(404, "用户不存在")
        user.enabled = False
        await session.commit()
    return {"code": 0, "message": f"用户 {user.username} 已禁用"}


# ------------------------------------------------------------------
# 视频
# ------------------------------------------------------------------

@app.get("/api/videos", dependencies=[Depends(require_api_key)])
async def list_videos(
    blogger: Optional[int] = None,
    tag: Optional[str] = Query(None, max_length=100),
    sentiment: Optional[Literal["bullish", "bearish", "neutral"]] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    async with async_session() as session:
        stmt = select(Video, Summary).outerjoin(Summary, Video.bvid == Summary.bvid)

        if blogger:
            stmt = stmt.where(Video.mid == blogger)

        stmt = stmt.where(Video.mid.in_(select(Blogger.mid).where(Blogger.enabled == True)))
        if sentiment:
            stmt = stmt.where(Summary.sentiment == sentiment)
        if tag:
            if session.bind.dialect.name == "sqlite":
                elements = func.json_each(Summary.tags).table_valued("value")
                stmt = stmt.where(select(1).select_from(elements).where(elements.c.value == tag).exists())
            else:
                import json
                stmt = stmt.where(func.json_contains(Summary.tags, json.dumps(tag, ensure_ascii=False)) == 1)
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0

        # Paginate
        stmt = stmt.order_by(desc(Video.publish_time), desc(Video.bvid)).offset((page - 1) * limit).limit(limit)
        rows = (await session.execute(stmt)).all()

        items = []
        for v, s in rows:
            item = {
                "bvid": v.bvid,
                "mid": v.mid,
                "title": v.title,
                "duration": v.duration,
                "publish_time": v.publish_time.isoformat() if v.publish_time else None,
                "view_count": v.view_count,
                "sentiment": s.sentiment if s else None,
                "sentiment_score": s.sentiment_score if s else None,
                "summary": s.summary[:200] if s and s.summary else None,
                "fetch_status": v.fetch_status,
                "analysis_status": v.analysis_status,
            }
            items.append(item)

        return {
            "code": 0,
            "data": {
                "items": items,
                "total": total,
                "page": page,
                "has_more": page * limit < total,
            },
        }


@app.get("/api/videos/{bvid}", dependencies=[Depends(require_api_key)])
async def get_video_detail(bvid: str):
    async with async_session() as session:
        video = await session.get(Video, bvid)
        if not video:
            raise HTTPException(404, "视频不存在")

        summary = await session.get(Summary, bvid)
        transcript = await session.get(Transcript, bvid)
        comment = await session.get(CommentAnalysis, (bvid, "video"))
        danmaku = await session.get(DanmakuAnalysis, bvid)

        return {
            "code": 0,
            "data": {
                "bvid": video.bvid,
                "mid": video.mid,
                "title": video.title,
                "duration": video.duration,
                "publish_time": video.publish_time.isoformat() if video.publish_time else None,
                "view_count": video.view_count,
                "fetch_status": video.fetch_status,
                "analysis_status": video.analysis_status,
                "summary": {
                    "text": summary.summary if summary else None,
                    "key_points": summary.key_points if summary else [],
                    "sentiment": summary.sentiment if summary else None,
                    "sentiment_score": summary.sentiment_score if summary else None,
                    "risk_warnings": summary.risk_warnings if summary else [],
                    "data_citations": summary.data_citations if summary else [],
                    "tags": summary.tags if summary else [],
                } if summary else None,
                "transcript": {
                    "source": transcript.source if transcript else None,
                    "text": transcript.full_text if transcript else None,
                    "segments": transcript.segment_count if transcript else None,
                } if transcript else None,
                "comments": {
                    "total": comment.total_count if comment else 0,
                    "sentiment": {
                        "bullish": comment.sentiment_bullish if comment else 0,
                        "bearish": comment.sentiment_bearish if comment else 0,
                        "neutral": comment.sentiment_neutral if comment else 0,
                    },
                    "hot_comments": comment.hot_comments if comment else [],
                    "keywords": comment.keywords if comment else [],
                } if comment else None,
                "danmaku": {
                    "total": danmaku.total_count if danmaku else 0,
                    "sampled": danmaku.sampled_count if danmaku else 0,
                    "sentiment": {
                        "bullish": danmaku.sentiment_bullish if danmaku else 0,
                        "bearish": danmaku.sentiment_bearish if danmaku else 0,
                        "neutral": danmaku.sentiment_neutral if danmaku else 0,
                    },
                    "keywords": danmaku.keywords if danmaku else [],
                } if danmaku else None,
            },
        }


# ------------------------------------------------------------------
# 动态
# ------------------------------------------------------------------

@app.get("/api/dynamics", dependencies=[Depends(require_api_key)])
async def list_dynamics(
    blogger: Optional[int] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    async with async_session() as session:
        stmt = select(Dynamic)
        if blogger:
            stmt = stmt.where(Dynamic.mid == blogger)

        count_stmt = select(func.count(Dynamic.dyn_id))
        if blogger:
            count_stmt = count_stmt.where(Dynamic.mid == blogger)
        total = (await session.execute(count_stmt)).scalar() or 0

        stmt = stmt.order_by(desc(Dynamic.publish_time)).offset((page - 1) * limit).limit(limit)
        items = (await session.execute(stmt)).scalars().all()

        return {
            "code": 0,
            "data": {
                "items": [
                    {
                        "dyn_id": d.dyn_id,
                        "mid": d.mid,
                        "content": d.content[:500] if d.content else None,
                        "publish_time": d.publish_time.isoformat() if d.publish_time else None,
                        "summary": d.summary,
                        "sentiment": d.sentiment,
                        "tags": d.tags,
                    }
                    for d in items
                ],
                "total": total,
                "page": page,
                "has_more": page * limit < total,
            },
        }


# ------------------------------------------------------------------
# 聚合
# ------------------------------------------------------------------

@app.get("/api/feed", dependencies=[Depends(require_api_key)])
async def get_feed(
    limit: int = Query(20, ge=1, le=100),
    before: Optional[float] = Query(None, ge=0, le=253402214400, allow_inf_nan=False),
    before_id: Optional[str] = Query(None, max_length=80, pattern=r"^(video|dynamic):[A-Za-z0-9]+$"),
    blogger: Optional[int] = Query(None, gt=0),
    sentiment: Optional[Literal["bullish", "bearish", "neutral"]] = None,
):
    if before_id and before is None:
        raise HTTPException(400, "before_id requires before")
    async with async_session() as session:
        enabled = select(Blogger.mid).where(Blogger.enabled == True)
        vk, dk = literal("video:") + Video.bvid, literal("dynamic:") + Dynamic.dyn_id
        if session.bind.dialect.name == "mysql":
            # Python/Kotlin use case-sensitive ID ordering; default MySQL
            # collations may be case-insensitive and would break the boundary.
            vk, dk = vk.collate("utf8mb4_bin"), dk.collate("utf8mb4_bin")
        vs = select(Video, Summary).outerjoin(Summary, Video.bvid == Summary.bvid).where(Video.mid.in_(enabled))
        ds = select(Dynamic).where(Dynamic.mid.in_(enabled))
        # Issue #8: Feed 只展示已完成的内容，过滤失败/处理中的记录
        vs = vs.where(Video.fetch_status == "ok", Video.analysis_status == "completed")
        if blogger:
            vs, ds = vs.where(Video.mid == blogger), ds.where(Dynamic.mid == blogger)
        if sentiment:
            vs, ds = vs.where(Summary.sentiment == sentiment), ds.where(Dynamic.sentiment == sentiment)
        total = (await session.execute(select(func.count()).select_from(vs.subquery()))).scalar()
        total += (await session.execute(select(func.count()).select_from(ds.subquery()))).scalar()
        if before is not None:
            boundary = datetime.fromtimestamp(before, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            def earlier(column, key):
                return or_(column < boundary, and_(column == boundary, key < before_id)) if before_id else column < boundary
            vs, ds = vs.where(earlier(Video.publish_time, vk)), ds.where(earlier(Dynamic.publish_time, dk))
        videos = (await session.execute(vs.order_by(Video.publish_time.desc(), vk.desc()).limit(limit + 1))).all()
        dynamics = (await session.execute(ds.order_by(Dynamic.publish_time.desc(), dk.desc()).limit(limit + 1))).scalars().all()
        feed = [{"type": "video", "bvid": v.bvid, "mid": v.mid, "title": v.title,
            "publish_time": v.publish_time.isoformat() if v.publish_time else None,
            "sentiment": s.sentiment if s else None, "sentiment_score": s.sentiment_score if s else None,
            "summary": s.summary[:200] if s and s.summary else None, "duration": v.duration, "view_count": v.view_count,
            "fetch_status": v.fetch_status, "analysis_status": v.analysis_status}
            for v, s in videos]
        feed.extend({"type": "dynamic", "dyn_id": d.dyn_id, "mid": d.mid,
            "publish_time": d.publish_time.isoformat() if d.publish_time else None,
            "sentiment": d.sentiment, "summary": d.summary} for d in dynamics)
        def key(item):
            return item["type"] + ":" + (item.get("bvid") or item.get("dyn_id"))
        feed.sort(key=lambda item: (item["publish_time"] or "", key(item)), reverse=True)
        has_more = len(feed) > limit
        items = feed[:limit]
        cursor = None
        if has_more and items and items[-1]["publish_time"]:
            last = items[-1]
            cursor = {"before": datetime.fromisoformat(last["publish_time"]).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
                "before_id": key(last)}
        return {"code": 0, "data": {"items": items, "total": total,
            "has_more": has_more and cursor is not None, "next_cursor": cursor, "cursor": cursor}}


@app.get("/api/daily", dependencies=[Depends(require_api_key)])
async def list_daily_dates():
    """可用日期列表（附摘要预览）。"""
    async with async_session() as session:
        stmt = select(
            DailyDigest.digest_date,
            func.json_unquote(func.json_extract(DailyDigest.content, "$.overall_sentiment")).label("overall_sentiment"),
            func.json_extract(DailyDigest.content, "$.sentiment_score").label("sentiment_score"),
            func.left(func.json_unquote(func.json_extract(DailyDigest.content, "$.summary")), 100).label("summary"),
        ).order_by(desc(DailyDigest.digest_date))
        rows = (await session.execute(stmt)).all()
        # Issue #9: dates 返回字符串列表（对齐 Android DailyDatesResponse: List<String>）
        date_strings = []
        date_details = {}  # 扩展数据供需要详情的客户端使用
        for r in rows:
            ds = r.digest_date.isoformat()
            date_strings.append(ds)
            detail = {}
            if r.overall_sentiment:
                detail["overall_sentiment"] = r.overall_sentiment
            if r.sentiment_score is not None:
                detail["sentiment_score"] = float(r.sentiment_score)
            if r.summary:
                detail["summary"] = r.summary
            if detail:
                date_details[ds] = detail
        return {"code": 0, "data": {"dates": date_strings, "details": date_details}}


@app.get("/api/daily/{date_str}", dependencies=[Depends(require_api_key)])
async def get_daily_digest(date_str: str):
    """某日汇总。"""
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "日期格式错误，应为 YYYY-MM-DD")

    async with async_session() as session:
        digest = await session.get(DailyDigest, target_date)
        if not digest:
            raise HTTPException(404, "该日期无汇总数据")

        return {"code": 0, "data": {"date": date_str, **(digest.content or {})}}


# ------------------------------------------------------------------
# 系统
# ------------------------------------------------------------------

@app.get("/api/health")
async def health():
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "db": "disconnected"})
    if _scheduler is not None and not _scheduler.running:
        return JSONResponse(status_code=503, content={"status": "degraded", "scheduler": "stopped"})
    if _last_job_write_failure is not None:
        return JSONResponse(status_code=503, content={"status": "degraded",
            "reason": "job status write failed", "job_id": _last_job_write_failure["job_id"]})
    return {"status": "ok", "db": "connected"}


@app.get("/api/status", dependencies=[Depends(require_api_key)])
async def status():
    async with async_session() as session:
        bloggers = (await session.execute(select(func.count()).select_from(Blogger).where(Blogger.enabled == True))).scalar()
        videos = (await session.execute(select(func.count()).select_from(Video))).scalar()
        failed = (await session.execute(select(func.count()).select_from(Video).where(Video.fetch_status == "failed"))).scalar()
        # #6: last_job包含所有状态（含skipped），last_fetch只取实际完成的
        job = (await session.execute(select(FetchJob).order_by(
            FetchJob.started_at.desc()).limit(1))).scalar_one_or_none()
        last_fetch_job = (await session.execute(select(FetchJob).where(
            FetchJob.status.in_(["completed", "partial"]),
            FetchJob.finished_at.isnot(None)
        ).order_by(FetchJob.finished_at.desc()).limit(1))).scalar_one_or_none()
        last_job = _job_json(job) if job else None
        last_fetch_time = last_fetch_job.finished_at.isoformat() if last_fetch_job and last_fetch_job.finished_at else None
    cookie = await _cached_cookie_status()
    return {"code": 0, "data": {"bloggers": bloggers, "videos": videos, "failed": failed,
        "last_fetch": last_fetch_time, "last_job": last_job,
        "task_running": _job_lock.locked(), "cookie_valid": cookie.get("valid", False),
        "cookie_expire": cookie.get("expire_date", "")}}


@app.post("/api/fetch/trigger", dependencies=[Depends(require_api_key)])
async def trigger_fetch():
    return await _submit_job("fetch", _fetch_work)


async def _run_fetch_task():
    return await _run_job("fetch", _fetch_work)


class BackfillRequest(BaseModel):
    since: date
    mid: Optional[int] = Field(None, gt=0, le=9223372036854775807)


@app.post("/api/fetch/backfill", dependencies=[Depends(require_api_key)])
async def trigger_backfill(req: BackfillRequest):
    if req.since > _utcnow().date():
        raise HTTPException(400, "since 不能晚于今天")
    # Issue #1: API 层拒绝早于硬性下限的日期
    if req.since < _HARD_SINCE.date():
        raise HTTPException(400, f"since 不能早于 {_HARD_SINCE.date()}")
    if req.mid:
        async with async_session() as session:
            b = await session.get(Blogger, req.mid)
            if not b or not b.enabled:
                raise HTTPException(404, "博主不存在或已禁用")
    return await _submit_job("backfill", lambda: _backfill_work(req.mid, req.since.isoformat()))


async def _run_backfill_task(mid: int = None, since: str = "2026-07-01"):
    return await _run_job("backfill", lambda: _backfill_work(mid, since))


@app.post("/api/transcripts/retry", dependencies=[Depends(require_api_key)])
async def retry_transcripts():
    return await _submit_job("retry_transcripts", _retry_work)


@app.post("/api/retry/failed", dependencies=[Depends(require_api_key)])
async def retry_failed():
    """针对性重试：只处理失败视频，按错误类型分级，不扫描新视频。"""
    return await _submit_job("retry_failed", _retry_failed_work)


async def _retry_failed_work():
    return await run_retry_failed()


async def _retry_transcripts_task():
    return await _run_job("retry_transcripts", _retry_work)


async def _recover_interrupted():
    async with async_session() as session:
        # 标记残留的running/queued记录为interrupted
        await session.execute(update(FetchJob).where(
            FetchJob.status.in_(["running", "queued"])).values(
            status="interrupted", finished_at=_utcnow(), error_message="Service restarted"))
        # #3: 重试计数和错误信息先于状态修改，确保MySQL单语句语义正确
        # #1: 同步写入error_type——恢复记录也必须有分类
        await session.execute(text("""
            UPDATE videos SET
                retry_count = CASE
                    WHEN fetch_status = 'pending' OR analysis_status = 'processing'
                    THEN COALESCE(retry_count, 0) + 1
                    ELSE COALESCE(retry_count, 0)
                END,
                error_message = CASE
                    WHEN fetch_status = 'pending' OR analysis_status = 'processing'
                    THEN 'Service restarted'
                    ELSE error_message
                END,
                error_type = CASE
                    WHEN fetch_status = 'pending' OR analysis_status = 'processing'
                    THEN 'unknown'
                    ELSE error_type
                END,
                fetch_status = CASE WHEN fetch_status = 'pending' THEN 'failed' ELSE fetch_status END,
                analysis_status = CASE WHEN analysis_status = 'processing' THEN 'failed' ELSE analysis_status END
            WHERE fetch_status = 'pending' OR analysis_status = 'processing'
        """))
        await session.commit()


def _job_json(job):
    return {"id": job.id, "kind": job.kind, "status": job.status,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": job.result, "error": job.error_message}


async def _reserve_job(kind):
    """只获取任务锁，不创建记录。记录由 _run_job 或 _submit_job 创建。"""
    if _job_lock.locked():
        raise HTTPException(409, "已有采集或补算任务正在运行")
    await _job_lock.acquire()


async def _execute_job(job_id, work):
    global _last_job_write_failure
    task = asyncio.current_task()
    _background_tasks.add(task)
    status, result, error = "completed", None, None
    try:
        result = await work()
        if result and (result.get("total_failed", 0) or result.get("failed", 0)):
            status = "partial"
    except asyncio.CancelledError:
        status, error = "interrupted", "Task cancelled"
        raise
    except Exception as exc:
        status, error = "failed", str(exc)[:1000]
    # #7: 最终状态提交——有界重试，防止DB临时故障导致永久running
    try:
        for _attempt in range(3):
            try:
                async with async_session() as session:
                    job = await session.get(FetchJob, job_id)
                    job.status, job.result, job.error_message = status, result, error
                    job.finished_at = _utcnow()
                    await session.commit()
                _last_job_write_failure = None  # DB恢复正常，清除降级标记
                break
            except Exception as e:
                if _attempt < 2:
                    print(f"[execute_job] 状态提交失败(尝试{_attempt+1}/3): {e}")
                    await asyncio.sleep(1)
                else:
                    print(f"[execute_job] [WARN] 状态提交最终失败: {job_id} 将保持running直到重启 (DEGRADED)")
                    _last_job_write_failure = {"job_id": job_id, "status": status, "time": _utcnow()}
    finally:
        _job_lock.release()
        _background_tasks.discard(task)
    return {"id": job_id, "status": status, "result": result, "error": error}


async def _run_job(kind, work):
    # #4: 不使用伪队列——直接尝试获取锁，冲突时留记录
    try:
        await _reserve_job(kind)
    except HTTPException as exc:
        if exc.status_code == 409:
            # #6: 跳过的任务留持久记录——可观测
            async with async_session() as session:
                skipped = FetchJob(id=str(uuid4()), kind=kind, status="skipped",
                    started_at=_utcnow(), finished_at=_utcnow(),
                    error_message="Busy: another task holds the lock")
                session.add(skipped)
                await session.commit()
            print(f"[scheduler] {kind}: busy, recorded as skipped")
            return {"status": "busy"}
        else:
            raise
    # #5: 创建任务记录——失败时释放锁
    try:
        async with async_session() as session:
            job = FetchJob(id=str(uuid4()), kind=kind, status="running", started_at=_utcnow())
            session.add(job)
            await session.commit()
            job_id = job.id
    except BaseException:
        _job_lock.release()
        raise
    return await _execute_job(job_id, work)


async def _submit_job(kind, work):
    await _reserve_job(kind)
    # #5: 创建任务记录——BaseException-safe（覆盖CancelledError）
    try:
        async with async_session() as session:
            job = FetchJob(id=str(uuid4()), kind=kind, status="running", started_at=_utcnow())
            session.add(job)
            await session.commit()
            job_id = job.id
    except BaseException:
        _job_lock.release()
        raise
    task = asyncio.create_task(_execute_job(job_id, work))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"code": 0, "data": {"id": job_id, "status": "accepted", "message": "任务已接受，可在状态页查看结果"}}


async def _fetch_work():
    result = await run_fetch_only()
    digests = await regenerate_dirty_digests()
    result["digests"] = digests
    result["total_failed"] = result.get("total_failed", 0) + digests["failed"]
    return result


async def _digest_work():
    result = await run_fetch_only()
    digests = await regenerate_dirty_digests(include_latest=True)
    result["digests"] = digests
    result["total_failed"] = result.get("total_failed", 0) + digests["failed"]
    return result


async def _backfill_work(mid, since):
    bloggers = [{"mid": mid}] if mid else await _get_enabled_bloggers()
    results = []
    # Issue #6: 共享预算——所有博主共用 backfill_cap
    remaining = get_config().get("data", {}).get("backfill_cap", 20)
    # #5: 共享页面预算——所有博主共用，防止单博主异常放大请求量
    page_budget = {"remaining": 50}
    for b in bloggers:
        if remaining <= 0 or page_budget["remaining"] <= 0:
            break
        try:
            r = await run_backfill(b["mid"], since, remaining, page_budget=page_budget)
            results.append(r)
            remaining -= r.get("attempted", 0)
        except Exception as exc:
            # Issue #6: 单博主异常不阻塞后续博主
            print(f"[backfill] blogger {b['mid']} failed: {exc}")
            results.append({"mid": b["mid"], "name": b.get("name", str(b["mid"])),
                            "processed": 0, "failed": 0, "attempted": 0, "error": str(exc)[:200]})
    # Issue #6: 补偿阶段始终执行，不受博主异常影响
    digests = await regenerate_dirty_digests()
    not_visited = sum(r.get("not_visited", 0) for r in results)
    if not_visited:
        print(f"[backfill] {not_visited} bloggers skipped (page budget exhausted)")
    return {"bloggers": results, "failed": sum(r.get("failed", 0) for r in results) + digests["failed"], "digests": digests}


async def _retry_work():
    from src.fetcher import _bili_client, _resolve_error_type
    from src.retry_strategy import is_permanent, should_retry
    async with async_session() as session:
        videos = (await session.execute(select(Video).outerjoin(Transcript, Video.bvid == Transcript.bvid)
            .outerjoin(Summary, Video.bvid == Summary.bvid).where(Video.mid.in_(select(Blogger.mid).where(Blogger.enabled == True)),
                Video.publish_time >= _HARD_SINCE,  # Issue #2: 日期下限
                Video.publish_time.isnot(None),
                or_(Video.fetch_status != "ok", Transcript.bvid.is_(None), Transcript.full_text == "", Summary.bvid.is_(None))))).scalars().all()
    # Issue #3: 统一重试资格检查——与 run_retry_failed() 一致
    retryable = []
    for v in videos:
        err_type = await _resolve_error_type(v)
        if is_permanent(err_type):
            print(f"[retry_work] {v.bvid}: 永久失败({err_type})，跳过")
            continue
        if not should_retry(v.retry_count or 0, err_type):
            print(f"[retry_work] {v.bvid}: {err_type} 达上限({v.retry_count})，跳过")
            continue
        retryable.append(v)
        if len(retryable) >= 50:
            break
    result = {"processed": 0, "skipped": len(videos) - len(retryable), "failed": 0}
    async with _bili_client() as client:
        for video in retryable:
            try:
                await _process_video(client, video.mid, _video_info(video), is_retry=True)
                result["processed"] += 1
            except Exception:
                result["failed"] += 1
            await asyncio.sleep(get_config().get("limits", {}).get("video_interval_seconds", 5))
    digests = await regenerate_dirty_digests()
    result["failed"] += digests["failed"]
    return result


async def _cached_cookie_status():
    import time
    from src.bilibili import BiliClient
    global _cookie_cache
    async with _cookie_lock:
        timestamp, data = _cookie_cache
        if time.monotonic() - timestamp < 60:
            return data
        async with BiliClient(sessdata=get_env("SESSDATA")) as client:
            data = await client.validate_sessdata()
        _cookie_cache = (time.monotonic(), data)
        return data


@app.get("/api/tasks/{job_id}", dependencies=[Depends(require_api_key)])
async def task_status(job_id: str):
    async with async_session() as session:
        job = await session.get(FetchJob, job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return {"code": 0, "data": _job_json(job)}


from src.bilibili import BiliAPIError


@app.exception_handler(BiliAPIError)
async def upstream_error(request, exc):
    return JSONResponse(status_code=502, content={"detail": "B站请求失败，请稍后重试", "upstream_code": exc.code})
