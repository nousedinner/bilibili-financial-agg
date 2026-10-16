"""抓取调度器 — 单视频管线 + 全量抓取 + 历史补抓。"""

import asyncio
import json
import logging
import os
import sqlite3
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


async def _resolve_error_type(v) -> str:
    """统一错误类型解析：优先用持久化值，无则分类一次并持久化。所有入口统一调用。"""
    err_type = getattr(v, 'error_type', None)
    if err_type:
        return err_type
    err_type = classify_error(getattr(v, 'error_message', '') or '', getattr(v, 'duration', 0) or 0)
    # 持久化
    try:
        async with async_session() as session:
            video = await session.get(Video, v.bvid)
            if video and not video.error_type:
                video.error_type = err_type
                await session.commit()
    except Exception as e:
        logger.warning(f"[fetcher] {v.bvid}: 持久化error_type失败({e})")
    return err_type
from src.models import (
    Blogger, Video, Transcript, Summary,
    CommentAnalysis, DanmakuAnalysis, Dynamic,
    FetchWatermark, DailyDigest, PendingDynamic, DirtyDigest,
    FetchJob,
)
from src.db import async_session
from src.config import get_config, get_env
from src.lifecycle import fence, OwnershipLost

logger = logging.getLogger(__name__)

# ── Issue #14 #3: 独立SQLite outbox — 任务终态持久化，MySQL不可写时保结果 ──
_outbox_conn: Optional[sqlite3.Connection] = None

def _outbox_path() -> str:
    return get_config().get("data", {}).get("outbox_path",
        "/app/data/job_outbox.db")

def _get_outbox_conn() -> sqlite3.Connection:
    global _outbox_conn
    if _outbox_conn is not None:
        return _outbox_conn
    path = _outbox_path()
    # Issue #15 #8: 路径校验——目录不存在或不可写时 fallback 到 /tmp
    outbox_dir = os.path.dirname(path)
    try:
        os.makedirs(outbox_dir, exist_ok=True)
    except (OSError, PermissionError) as e:
        logger.warning(f"[outbox] 无法创建目录 {outbox_dir}: {e}，fallback 到 /tmp")
        path = os.path.join("/tmp", "job_outbox.db")
    if not os.access(os.path.dirname(path), os.W_OK):
        logger.warning(f"[outbox] 目录 {os.path.dirname(path)} 不可写，fallback 到 /tmp")
        path = os.path.join("/tmp", "job_outbox.db")
        os.makedirs("/tmp", exist_ok=True)
    _outbox_conn = sqlite3.connect(path, timeout=5)
    _outbox_conn.execute("PRAGMA journal_mode=WAL")
    _outbox_conn.execute("""CREATE TABLE IF NOT EXISTS job_outbox (
        job_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        result TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        retry_count INTEGER DEFAULT 0,
        last_retry_at TEXT
    )""")
    _outbox_conn.commit()
    return _outbox_conn

def outbox_save(job_id: str, status: str, result: Optional[dict] = None, error: Optional[str] = None) -> bool:
    """保存任务终态到独立SQLite文件。返回是否成功。"""
    try:
        conn = _get_outbox_conn()
        conn.execute(
            "INSERT OR REPLACE INTO job_outbox (job_id, status, result, error_message, created_at, retry_count, last_retry_at) VALUES (?, ?, ?, ?, ?, 0, NULL)",
            (job_id, status, json.dumps(result) if result else None, error,
             datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[outbox] save failed for {job_id}: {e}")
        return False

def outbox_pop(job_id: str):
    """MySQL终态写入成功后，从outbox移除。"""
    try:
        conn = _get_outbox_conn()
        conn.execute("DELETE FROM job_outbox WHERE job_id = ?", (job_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"[outbox] pop failed for {job_id}: {e}")

def outbox_pending() -> list:
    """获取所有待重放的outbox条目。"""
    try:
        conn = _get_outbox_conn()
        rows = conn.execute("SELECT job_id, status, result, error_message FROM job_outbox").fetchall()
        return [{"job_id": r[0], "status": r[1],
                 "result": json.loads(r[2]) if r[2] else None, "error": r[3]} for r in rows]
    except Exception as e:
        logger.error(f"[outbox] load failed: {e}")
        return []

async def outbox_replay_on_startup():
    """启动时重放outbox：将MySQL中仍为running的job更新为outbox中的终态。"""
    pending = outbox_pending()
    if not pending:
        return
    logger.info(f"[outbox] replaying {len(pending)} pending job states")

    for entry in pending:
        try:
            async with async_session() as session:
                await fence(session)
                job = await session.get(FetchJob, entry["job_id"])
                if job and job.status in ("running", "queued"):
                    job.status = entry["status"]
                    job.result = entry["result"]
                    job.error_message = entry["error"]
                    job.finished_at = _utcnow()
                    await session.commit()
                outbox_pop(entry["job_id"])
                logger.info(f"[outbox] replayed {entry['job_id']}: {entry['status']}")
        except OwnershipLost:
            raise
        except Exception as e:
            logger.error(f"[outbox] replay failed for {entry['job_id']}: {e}")
            try:
                conn = _get_outbox_conn()
                conn.execute(
                    "UPDATE job_outbox SET retry_count = retry_count + 1, last_retry_at = ? WHERE job_id = ?",
                    (_utcnow().isoformat(), entry["job_id"]))
                conn.commit()
            except Exception:
                pass

def outbox_close():
    """关闭SQLite连接。"""
    global _outbox_conn
    if _outbox_conn:
        try:
            _outbox_conn.close()
        except Exception:
            pass
        _outbox_conn = None

# ── 硬性时间下限：绝对业务边界，不可由配置修改 ──
_HARD_SINCE = datetime(2026, 7, 1)  # 绝对常量，backfill_since 只能 >= 此值

def _is_before_hard_since(pub_dt) -> bool:
    """发布时间是否早于硬性下限（含 None/无效时间）。"""
    return pub_dt is None or pub_dt < _HARD_SINCE


def _utcnow() -> datetime:
    """返回当前时间（naive，MySQL CST时区）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _bili_client() -> BiliClient:
    from src.credentials import credential_store
    from src.authorization import record_auth_check
    credential = credential_store().current()
    async def observe(version, state, reason):
        await record_auth_check(async_session, version, state, reason)
    return BiliClient(sessdata=credential.sessdata, credential_version=credential.version,
        cookie_extra=credential.cookie_header(), auth_observer=observe)


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
        logger.info("[fetcher] 没有启用的博主，跳过抓取")
        return {"total_new": 0, "total_failed": 0, "bloggers": {}}

    cfg = get_config().get("data", {})
    # Issue #11 #1: 任务级共享预算——pages/new_videos/retries 三项独立扣减
    budget = {
        "pages": cfg.get("task_page_budget", 50),
        "new_videos": cfg.get("task_video_budget", cfg.get("first_run_cap", 50)),
        "retries": cfg.get("task_retry_budget", 50),
    }
    results = {"total_new": 0, "total_failed": 0, "bloggers": {}}
    visited = 0

    async with _bili_client() as client:
        # Validate cookie
        cookie_status = await client.validate_sessdata()
        from src.authorization import record_auth_check
        await record_auth_check(async_session, getattr(client, "credential_version", 1),
            cookie_status.get("state", "valid" if cookie_status.get("valid") else "unknown"), cookie_status.get("reason"))
        if cookie_status.get("state") == "invalid":
            client._sessdata, client._cookie_extra = "", ""
            if hasattr(client, "_transport"):
                client._transport.cookies.clear()
            logger.warning("[fetcher] B站授权无效，已记录告警并停止使用该会话进行普通请求")

        for blogger in bloggers:
            if not blogger.get("enabled", True):
                continue

            # Issue #13 #4: pages=0 时不 break，_fetch_blogger 内部跳扫描但保留重试

            mid = blogger["mid"]
            name = blogger.get("name", str(mid))
            logger.info(f"[fetcher] 处理博主: {name} (mid={mid})")

            try:
                b_result = await _fetch_blogger(client, mid, name, budget=budget)
                results["bloggers"][mid] = b_result
                results["total_new"] += b_result.get("new_count", 0)
                results["total_failed"] += b_result.get("failed_count", 0)
                visited += 1
            except Exception as e:
                logger.error(f"[fetcher] 博主 {name} 抓取失败: {e}")
                results["bloggers"][mid] = {"error": str(e)}
                results["total_failed"] += 1
                visited += 1

    # 页面预算在某个博主内部耗尽时，该博主仍然算“已访问”，但任务并未完成。
    not_visited = len(bloggers) - visited
    has_more = any(
        item.get("has_more", False)
        for item in results["bloggers"].values()
        if isinstance(item, dict)
    )
    results["truncated"] = not_visited > 0 or has_more
    results["not_visited"] = not_visited
    results["budget_exhausted"] = (not_visited > 0 or has_more) and budget["pages"] <= 0
    return results


async def run_daily_fetch() -> dict:
    """兼容旧接口：抓取 + 汇总。"""
    results = await run_fetch_only()
    bloggers = await _get_enabled_bloggers()
    await _generate_digest(bloggers, results)
    return results


async def run_backfill(mid: int, since: str, cap: int = 20, page_budget: dict = None, until=None) -> dict:
    from src.collection import manual_backfill
    async with _bili_client() as client:
        return await manual_backfill(async_session, client, mid, since, _process_video,
                                     cap=cap, page_budget=page_budget, until=until)

async def run_retry_failed(batch_limit: int = 50) -> dict:
    from src.collection import retry_ready
    async with _bili_client() as client:
        return await retry_ready(async_session, client, _process_video, limit=batch_limit)

async def _fetch_blogger(client: BiliClient, mid: int, name: str, budget: dict = None) -> dict:
    from src.collection import scan_blogger
    return await scan_blogger(async_session, client, mid, name, _process_video, budget=budget)

async def _process_video_core(client: BiliClient, mid: int, vinfo: dict, is_retry: bool = False):
    from src.pipeline import process
    from src.analyzer import analyze_interactions
    return await process(async_session, client, vinfo["bvid"], fetch_transcript=fetch_transcript,
        analyze_video=analyze_video, analyze_interactions=analyze_interactions,
        validate_analysis=_validate_analysis, mark_digest=_mark_digest_dirty)

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
    pub_dt = _publish_time(pub_ts) if pub_ts else None
    if pub_dt is None:
        raise ValueError("Dynamic publication timestamp is missing; refusing fabricated publication time")

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

    logger.info(f"[fetcher] dynamic {dyn_id} -- {analysis.get('sentiment', 'neutral')}")


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
                    Video.publish_time >= _HARD_SINCE))).all()
            videos = [{"bvid": v.bvid, "title": v.title or "", "summary": s.summary or "",
                "sentiment": s.sentiment or "neutral", "key_points": s.key_points or []} for v, s in rows]
            scores.extend(s.sentiment_score for _, s in rows if s.sentiment_score is not None)
            dyns = (await session.execute(select(Dynamic).where(Dynamic.mid == b["mid"],
                Dynamic.publish_time >= start, Dynamic.publish_time < end,
                Dynamic.publish_time >= _HARD_SINCE))).scalars().all()
            if videos or dyns:
                analyses.append({"mid": b["mid"], "name": b.get("name", str(b["mid"])), "videos": videos,
                    "dynamics": [{"summary": d.summary or "", "sentiment": d.sentiment or "neutral"} for d in dyns]})

    digest_content = None
    if analyses:
        digest_content = await generate_daily_digest(analyses)
        if digest_content.get("analysis_failed") or _validate_digest(digest_content).get("analysis_failed"):
            raise ValueError(f"Daily digest {target_date} analysis failed; existing content preserved")
        digest_content["bloggers"] = [b["name"] for b in analyses]
        digest_content["sentiment_score"] = round(sum(scores) / len(scores), 2) if scores else 0.0

    # Issue #11 #4: DailyDigest 保存 + DirtyDigest 删除合并为单一事务
    async with async_session() as session:
        await fence(session)
        if digest_content:
            await session.merge(DailyDigest(digest_date=target_date, content=digest_content))
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
    from src.models import VideoWork
    from src.lifecycle import WorkflowError
    bvid = vinfo.get("bvid")
    if not bvid or _is_before_hard_since(_publish_time(vinfo.get("created", 0))):
        raise WorkflowError("outside_scope", "admission")
    async with async_session() as session:
        work = await session.get(VideoWork, bvid)
        if work is None or work.mid != mid:
            raise WorkflowError("not_admitted", "admission")
    return await _process_video_core(client, mid, vinfo, is_retry)

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
            await fence(session)
            session.add(DirtyDigest(digest_date=latest))
            await session.commit()
        # Issue #14 #6: 多查1条检测是否有待处理日期
        dates = (await session.execute(select(DirtyDigest.digest_date).where(
            DirtyDigest.digest_date <= latest,
            DirtyDigest.digest_date >= hard_since_date,  # Issue #2: 绝对日期下限
            DirtyDigest.retry_count < 5,  # Issue #10: 失败5次后终止
        ).order_by(DirtyDigest.digest_date).limit(max_per_round + 1))).scalars().all()
    bloggers = await _get_enabled_bloggers()
    has_more_digests = len(dates) > max_per_round
    result = {"generated": 0, "failed": 0, "has_more": has_more_digests}
    for day in dates[:max_per_round]:
        try:
            await _generate_digest(bloggers, {}, day)
            result["generated"] += 1
        except OwnershipLost:
            raise
        except Exception as exc:
            result["failed"] += 1
            async with async_session() as session:
                await fence(session)
                row = await session.get(DirtyDigest, day)
                row.error_message = str(exc)[:500]
                row.retry_count = (row.retry_count or 0) + 1
                await session.commit()
    return result
