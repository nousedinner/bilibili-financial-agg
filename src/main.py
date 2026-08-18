"""FastAPI 应用 — REST API + 定时调度。"""

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, Query, Header, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_config, get_env
from src.db import init_db, async_session, engine
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest, ApiUser,
)
from src.fetcher import run_daily_fetch, run_fetch_only, run_backfill, _get_enabled_bloggers, _generate_digest


# ------------------------------------------------------------------
# Lifespan: init DB + start scheduler
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    _start_scheduler()
    yield


def _start_scheduler():
    """启动APScheduler定时任务：12:00/21:00抓取 + 22:00汇总。"""
    cfg = get_config()
    scheduler_cfg = cfg.get("scheduler", {})

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

        # 12:00 抓取
        fetch_cron_1 = scheduler_cfg.get("fetch_cron_1", "0 12 * * *")
        parts = fetch_cron_1.split()
        scheduler.add_job(_scheduled_fetch, CronTrigger(minute=parts[0], hour=parts[1]))
        print(f"[scheduler] 抓取任务1: {fetch_cron_1}")

        # 21:00 抓取
        fetch_cron_2 = scheduler_cfg.get("fetch_cron_2", "0 21 * * *")
        parts = fetch_cron_2.split()
        scheduler.add_job(_scheduled_fetch, CronTrigger(minute=parts[0], hour=parts[1]))
        print(f"[scheduler] 抓取任务2: {fetch_cron_2}")

        # 22:00 汇总锁定
        digest_cron = scheduler_cfg.get("digest_cron", "0 22 * * *")
        parts = digest_cron.split()
        scheduler.add_job(_scheduled_digest, CronTrigger(minute=parts[0], hour=parts[1]))
        print(f"[scheduler] 汇总任务: {digest_cron}")

        scheduler.start()
    except Exception as e:
        print(f"[scheduler] 启动失败: {e}")


async def _scheduled_fetch():
    """定时任务入口：只抓取，不生成汇总。"""
    print(f"[scheduler] 开始抓取: {datetime.now()}")
    try:
        result = await run_fetch_only()
        print(f"[scheduler] 抓取完成: {result.get('total_new', 0)} 新内容")
    except Exception as e:
        print(f"[scheduler] 抓取失败: {e}")


async def _scheduled_digest():
    """定时任务入口：只生成汇总，不抓取。"""
    print(f"[scheduler] 开始生成汇总: {datetime.now()}")
    try:
        bloggers = await _get_enabled_bloggers()
        await _generate_digest(bloggers, {})
        print(f"[scheduler] 汇总生成完成")
    except Exception as e:
        print(f"[scheduler] 汇总生成失败: {e}")


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
                {"mid": b.mid, "name": b.name, "tags": b.tags, "added_at": str(b.added_at)}
                for b in result
            ],
        }


@app.post("/api/bloggers", dependencies=[Depends(require_api_key)])
async def add_blogger(name: str = Query(...), mid: Optional[int] = Query(None), tags: str = Query("")):
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
    return {"code": 0, "message": f"博主 {mid} 已禁用"}


@app.post("/api/bloggers/sync", dependencies=[Depends(require_api_key)])
async def sync_followings():
    """同步B站关注列表 — 返回尚未添加到监控的新关注。"""
    from src.bilibili import BiliClient
    from src.config import get_env

    sessdata = get_env("SESSDATA")
    bili_uid = get_env("BILI_UID")
    if not sessdata:
        raise HTTPException(400, "未配置SESSDATA")
    if not bili_uid:
        raise HTTPException(400, "未配置BILI_UID")

    # 1. 拉关注列表
    async with BiliClient(sessdata=sessdata) as client:
        result = await client.get_followings(int(bili_uid), pn=1, ps=200)

    if not result["list"]:
        return {"code": 0, "data": {"total_followings": result["total"], "new": [], "message": "获取关注列表失败或为空"}}

    # 2. 对比DB，找出未添加的
    async with async_session() as session:
        stmt = select(Blogger.mid)
        existing_mids = set((await session.execute(stmt)).scalars().all())

    new_followings = [
        f for f in result["list"]
        if f["mid"] not in existing_mids
    ]

    return {
        "code": 0,
        "data": {
            "total_followings": result["total"],
            "already_tracked": len(existing_mids),
            "new_count": len(new_followings),
            "new": new_followings,
        },
    }


@app.post("/api/bloggers/sync/add", dependencies=[Depends(require_api_key)])
async def sync_add_bloggers(
    mids: str = Query(..., description="逗号分隔的mid列表"),
    names: str = Query("", description="逗号分隔的用户名列表（与mids一一对应）"),
):
    """从同步结果中批量添加博主。mids和names逗号分隔，一一对应。"""
    mid_list = [int(m.strip()) for m in mids.split(",") if m.strip()]
    name_list = [n.strip() for n in names.split(",") if n.strip()]
    if not mid_list:
        raise HTTPException(400, "mids不能为空")
    # 补齐names
    while len(name_list) < len(mid_list):
        name_list.append(f"用户{mid_list[len(name_list)]}")

    added = []
    async with async_session() as session:
        for i, mid in enumerate(mid_list):
            name = name_list[i]
            existing = await session.get(Blogger, mid)
            if existing:
                if not existing.enabled:
                    existing.enabled = True
                    added.append({"mid": mid, "name": existing.name, "action": "re-enabled"})
                continue

            session.add(Blogger(mid=mid, name=name, tags=[]))
            added.append({"mid": mid, "name": name, "action": "added"})

        await session.commit()

    return {"code": 0, "data": {"added": added}}


# ------------------------------------------------------------------
# API用户管理
# ------------------------------------------------------------------

class CreateUserRequest(BaseModel):
    username: str

@app.post("/api/bootstrap")
async def bootstrap_user(req: CreateUserRequest):
    """首次启动时创建第一个API用户（仅当无任何用户时可用）。"""
    async with async_session() as session:
        count = (await session.execute(select(func.count(ApiUser.id)))).scalar() or 0
        if count > 0:
            raise HTTPException(409, "已有用户存在，请使用 /api/users 创建")
        api_key = secrets.token_urlsafe(32)
        session.add(ApiUser(username=req.username, api_key=api_key))
        await session.commit()
    return {"code": 0, "data": {"username": req.username, "api_key": api_key}}


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
    tag: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    async with async_session() as session:
        stmt = select(Video, Summary).outerjoin(Summary, Video.bvid == Summary.bvid)

        if blogger:
            stmt = stmt.where(Video.mid == blogger)

        # Count
        count_stmt = select(func.count(Video.bvid))
        if blogger:
            count_stmt = count_stmt.where(Video.mid == blogger)
        total = (await session.execute(count_stmt)).scalar() or 0

        # Paginate
        stmt = stmt.order_by(desc(Video.publish_time)).offset((page - 1) * limit).limit(limit)
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
            }
            # Tag filter
            if tag and s and s.tags and tag not in s.tags:
                continue
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
    before: Optional[float] = None,
):
    """时间线（视频+动态混合）。"""
    async with async_session() as session:
        # 只返回启用博主的内容
        enabled_mids = select(Blogger.mid).where(Blogger.enabled == True)

        # Videos
        v_stmt = (
            select(Video, Summary)
            .outerjoin(Summary, Video.bvid == Summary.bvid)
            .where(Video.mid.in_(enabled_mids))
        )
        if before:
            v_stmt = v_stmt.where(Video.publish_time < datetime.fromtimestamp(before))
        v_stmt = v_stmt.order_by(desc(Video.publish_time)).limit(limit)
        videos = (await session.execute(v_stmt)).all()

        # Dynamics
        d_stmt = select(Dynamic)
        if before:
            d_stmt = d_stmt.where(Dynamic.publish_time < datetime.fromtimestamp(before))
        d_stmt = d_stmt.order_by(desc(Dynamic.publish_time)).limit(limit)
        dynamics = (await session.execute(d_stmt)).scalars().all()

        # Merge and sort
        feed = []
        for v, s in videos:
            feed.append({
                "type": "video",
                "bvid": v.bvid,
                "mid": v.mid,
                "title": v.title,
                "publish_time": v.publish_time.isoformat() if v.publish_time else None,
                "sentiment": s.sentiment if s else None,
                "sentiment_score": s.sentiment_score if s else None,
                "summary": s.summary[:200] if s and s.summary else None,
            })
        for d in dynamics:
            feed.append({
                "type": "dynamic",
                "dyn_id": d.dyn_id,
                "mid": d.mid,
                "publish_time": d.publish_time.isoformat() if d.publish_time else None,
                "sentiment": d.sentiment,
                "summary": d.summary,
            })

        feed.sort(key=lambda x: x.get("publish_time", "") or "", reverse=True)
        return {"code": 0, "data": {"items": feed[:limit]}}


@app.get("/api/daily", dependencies=[Depends(require_api_key)])
async def list_daily_dates():
    """可用日期列表。"""
    async with async_session() as session:
        stmt = select(DailyDigest.digest_date).order_by(desc(DailyDigest.digest_date))
        dates = (await session.execute(stmt)).scalars().all()
        return {"code": 0, "data": {"dates": [d.isoformat() for d in dates]}}


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
    return {"status": "ok"}


@app.get("/api/status", dependencies=[Depends(require_api_key)])
async def status():
    async with async_session() as session:
        blogger_count = (await session.execute(select(func.count(Blogger.mid)).where(Blogger.enabled == True))).scalar() or 0
        video_count = (await session.execute(select(func.count(Video.bvid)))).scalar() or 0
        failed_count = (await session.execute(select(func.count(Video.bvid)).where(Video.fetch_status == "failed"))).scalar() or 0

        # Last fetch watermark
        wm_stmt = select(FetchWatermark).order_by(desc(FetchWatermark.updated_at)).limit(1)
        wm = (await session.execute(wm_stmt)).scalar_one_or_none()

        # Cookie status
        from src.bilibili import BiliClient
        sessdata = get_env("SESSDATA")
        cookie_info = {"valid": False, "expire_date": ""}
        if sessdata:
            async with BiliClient(sessdata=sessdata) as client:
                cookie_info = await client.validate_sessdata()

        return {
            "code": 0,
            "data": {
                "bloggers": blogger_count,
                "videos": video_count,
                "failed": failed_count,
                "last_fetch": wm.updated_at.isoformat() if wm else None,
                "cookie_valid": cookie_info.get("valid", False),
                "cookie_expire": cookie_info.get("expire_date", ""),
            },
        }


@app.post("/api/fetch/trigger", dependencies=[Depends(require_api_key)])
async def trigger_fetch():
    """手动触发抓取。"""
    asyncio.create_task(_run_fetch_task())
    return {"code": 0, "message": "抓取任务已触发"}


async def _run_fetch_task():
    try:
        result = await run_fetch_only()
        print(f"[trigger] 抓取完成: {result}")
    except Exception as e:
        print(f"[trigger] 抓取失败: {e}")


class BackfillRequest(BaseModel):
    since: str
    mid: Optional[int] = None


@app.post("/api/fetch/backfill", dependencies=[Depends(require_api_key)])
async def trigger_backfill(req: BackfillRequest):
    """历史补抓（后台执行）。"""
    asyncio.create_task(_run_backfill_task(req.mid, req.since))
    return {"code": 0, "message": f"补抓任务已触发 (since={req.since})"}


async def _run_backfill_task(mid: int = None, since: str = "2026-07-01"):
    cfg = get_config()
    cap = cfg.get("data", {}).get("backfill_cap", 20)
    try:
        if mid:
            result = await run_backfill(mid, since, cap)
            print(f"[backfill] 完成: {result}")
        else:
            bloggers = cfg.get("bloggers", [])
            for b in bloggers:
                result = await run_backfill(b["mid"], since, cap)
                print(f"[backfill] {b.get('name', b['mid'])}: {result}")
    except Exception as e:
        print(f"[backfill] 失败: {e}")


@app.post("/api/transcripts/retry", dependencies=[Depends(require_api_key)])
async def retry_transcripts():
    """补字幕：给没有字幕的视频重新跑ASR。"""
    asyncio.create_task(_retry_transcripts_task())
    return {"code": 0, "message": "字幕补全任务已触发"}


async def _retry_transcripts_task():
    """后台任务：找出没字幕的视频，尝试ASR转写。"""
    from src.transcript import fetch_transcript
    from src.analyzer import analyze_video
    from src.bilibili import BiliClient
    from src.config import get_env

    sessdata = get_env("SESSDATA")

    async with async_session() as session:
        # 找出没有字幕的视频
        stmt = (
            select(Video)
            .outerjoin(Transcript, Video.bvid == Transcript.bvid)
            .where(Video.fetch_status == "ok")
            .where(Transcript.bvid.is_(None))
        )
        videos = (await session.execute(stmt)).scalars().all()

    if not videos:
        print("[retry] 没有需要补字幕的视频")
        return

    print(f"[retry] 找到 {len(videos)} 个没字幕的视频，开始ASR转写")

    async with BiliClient(sessdata=sessdata) as client:
        for video in videos:
            try:
                # 获取CID
                vid_info = await client.get_video_info(video.bvid)
                cid = vid_info.get("cid", 0) or vid_info.get("pages", [{}])[0].get("cid", 0)
                if not cid:
                    print(f"[retry] {video.bvid}: 无法获取CID，跳过")
                    continue

                # 尝试获取字幕（含ASR降级）
                result = await fetch_transcript(client, video.bvid, cid, video.duration or 0)
                text = result.get("text", "")

                if text:
                    # 保存字幕
                    async with async_session() as session:
                        session.add(Transcript(
                            bvid=video.bvid,
                            source=result["source"],
                            full_text=text,
                            segment_count=result["segments"],
                        ))
                        await session.commit()
                    print(f"[retry] ✅ {video.bvid}: {result['source']} ({len(text)} chars)")

                    # 重新分析
                    comments = await client.get_comments(oid=vid_info.get("aid", 0), oid_type=1, count=20)
                    danmakus = await client.get_danmaku(cid, max_count=2000)
                    analysis = await analyze_video(video.title, text, comments, danmakus, video.duration or 0)

                    async with async_session() as session:
                        existing_sum = await session.get(Summary, video.bvid)
                        if existing_sum:
                            existing_sum.summary = analysis["summary"]
                            existing_sum.key_points = analysis["key_points"]
                            existing_sum.sentiment = analysis["sentiment"]
                            existing_sum.sentiment_score = analysis["sentiment_score"]
                            existing_sum.risk_warnings = analysis["risk_warnings"]
                            existing_sum.data_citations = analysis["data_citations"]
                            existing_sum.tags = analysis["tags"]
                        await session.commit()
                    print(f"[retry] ✅ {video.bvid}: 分析已更新")
                else:
                    print(f"[retry] ❌ {video.bvid}: ASR也拿不到字幕")

            except Exception as e:
                print(f"[retry] ❌ {video.bvid}: {e}")

            await asyncio.sleep(5)  # 限速

    print("[retry] 字幕补全完成")
