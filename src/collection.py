"""Finite discovery, immutable manual batches, and admitted-only recovery."""
import asyncio
from datetime import datetime

from sqlalchemy import select
from src.config import get_config
from src.lifecycle import HARD_SINCE, WorkflowError, automatic_floor, fence, now
from src.models import Blogger, Video, VideoWork, WorkBatch, FetchWatermark, DiscoveryScan
from src.workflow import create_batch, admit, freeze_batch, due_work, publication


def default_budget():
    data = get_config().get("data", {})
    return {"pages": int(data.get("task_page_budget", 50)),
            "new_videos": int(data.get("task_video_budget", data.get("first_run_cap", 20))),
            "retries": int(data.get("task_retry_budget", 50))}


async def _process_selected(sessions, client, jobs, process):
    result = {"processed": 0, "failed": 0, "skipped": 0, "details": []}
    for job in jobs:
        try:
            outcome = await process(client, job.mid, job.payload, is_retry=True)
            key = "skipped" if isinstance(outcome, dict) and outcome.get("status") == "skipped" else "processed"
            result[key] += 1
            result["details"].append({"bvid": job.bvid, "status": key})
        except Exception as exc:
            result["failed"] += 1
            result["details"].append({"bvid": job.bvid, "status": "failed",
                                      "reason": getattr(exc, "reason", type(exc).__name__)})
        await asyncio.sleep(max(0, float(get_config().get("limits", {}).get("video_interval_seconds", 5))))
    return result


async def scan_blogger(sessions, client, mid, name, process, *, budget=None):
    budget = default_budget() if budget is None else budget
    result = {"name": name, "new_count": 0, "failed_count": 0, "retried_count": 0,
              "dynamics_count": 0, "skipped_count": 0}
    at = now()
    floor = automatic_floor(at, get_config().get("data", {}).get("automatic_window_days", 7))
    configured = get_config().get("data", {}).get("backfill_since")
    if configured:
        floor = max(floor, datetime.fromisoformat(configured))
    async with sessions() as session:
        watermark = await session.get(FetchWatermark, mid)
        previous = (await session.execute(select(DiscoveryScan).where(DiscoveryScan.mid == mid,
            DiscoveryScan.state.in_(["complete", "window_limited", "initialized"]))
            .order_by(DiscoveryScan.until.desc()).limit(1))).scalar_one_or_none()
        initial = (await session.execute(select(DiscoveryScan).where(DiscoveryScan.mid == mid,
            DiscoveryScan.state == "initialized").order_by(DiscoveryScan.until).limit(1))).scalar_one_or_none()
        if initial:
            floor = max(floor, initial.since)
        anchor = previous.newest_bvid if previous and previous.newest_bvid else watermark.last_bvid if watermark else None
        anchor_time = previous.newest_publish_time if previous else watermark.last_publish_time if watermark else None
        blogger = await session.get(Blogger, mid)
    bootstrap = anchor is None
    # No watermark means establish a starting point, never silently backfill.
    if bootstrap:
        floor = at
    batch_id = await create_batch(sessions, origin="incremental", mid=mid, since=floor, until=at,
        max_videos=max(1, min(200, budget["new_videos"])), max_pages=max(1, min(200, budget["pages"])))
    seen, selected = set(), []
    complete, limited, newest, exhausted = False, False, None, False
    error = None
    anchor_seen = False

    def consume_retry():
        if budget["pages"] <= 0:
            return False
        budget["pages"] -= 1
        return True

    try:
        page = 1
        while budget["pages"] > 0:
            budget["pages"] -= 1
            listing = await client.get_video_list(mid, page=page, page_size=30, consume_retry_attempt=consume_retry)
            items = listing.get("list", {}).get("vlist", [])
            if not isinstance(items, list):
                raise WorkflowError("malformed_listing", "discovery")
            if not items:
                complete = True
                break
            fresh = [item for item in items if item.get("bvid") and item["bvid"] not in seen]
            if not fresh:
                raise WorkflowError("pagination_repeated", "discovery")
            if newest is None:
                newest = next((item for item in fresh if publication(item) is not None
                               and publication(item) <= at and (bootstrap or publication(item) >= floor)), None)
            if bootstrap:
                complete = True
                break
            reached = False
            for item in fresh:
                seen.add(item["bvid"])
                published = publication(item)
                if item["bvid"] == anchor:
                    anchor_seen = True
                    anchor_time = anchor_time or published
                    continue
                if published is None:
                    # Missing timestamps are not replaced with 'today' and do not
                    # provide evidence that the surrounding range is complete.
                    raise WorkflowError("missing_publication", "discovery")
                if published < floor:
                    continue
                if published > at:
                    continue
                if anchor_seen and anchor_time and published < anchor_time:
                    continue
                async with sessions() as session:
                    existing_video = await session.get(Video, item["bvid"])
                    existing_work = await session.get(VideoWork, item["bvid"])
                if existing_video or existing_work:
                    continue
                if budget["new_videos"] <= 0:
                    exhausted = True
                    break
                if await admit(sessions, batch_id, mid, item):
                    budget["new_videos"] -= 1
                    selected.append(item["bvid"])
            if exhausted:
                break
            tail_time = publication(fresh[-1])
            reached = bool(tail_time and ((anchor_seen and anchor_time and tail_time < anchor_time) or tail_time < floor))
            limited = bool(reached and not anchor_seen)
            if reached:
                complete = True
                break
            page += 1
        if not complete:
            result["has_more"] = True
    except Exception as exc:
        error = exc
        result["error"] = str(exc)[:200]
        result["failed_count"] += 1
    finally:
        await freeze_batch(sessions, batch_id, complete=complete)
        async with sessions() as session:
            await fence(session)
            scan = DiscoveryScan(id=batch_id, mid=mid, since=floor, until=at,
                state="initialized" if bootstrap and complete else "window_limited" if limited and complete else "complete" if complete else "partial",
                newest_bvid=newest.get("bvid") if newest else anchor,
                newest_publish_time=publication(newest) if newest else anchor_time)
            if limited and anchor_time and anchor_time < floor:
                scan.gap_from, scan.gap_until = anchor_time, floor
            session.add(scan)
            # The legacy marker is advanced only across a proven contiguous range.
            # Recent-window coverage and skipped historical gaps live in scans.
            if complete and newest and not limited:
                wm = await session.get(FetchWatermark, mid) or FetchWatermark(mid=mid)
                wm.last_bvid, wm.last_publish_time, wm.updated_at = newest["bvid"], publication(newest), at
                session.add(wm)
            await session.commit()
    jobs = await due_work(sessions, limit=min(50, len(selected) + max(0, budget["retries"])), mid=mid)
    chosen = []
    for job in jobs:
        if job.bvid in selected:
            chosen.append(job)
        elif budget["retries"] > 0:
            budget["retries"] -= 1
            chosen.append(job)
    outcomes = await _process_selected(sessions, client, chosen, process)
    result["failed_count"] += outcomes["failed"]
    result["skipped_count"] = outcomes["skipped"]
    complete_ids = {item["bvid"] for item in outcomes["details"] if item["status"] == "processed"}
    result["new_count"] = len(complete_ids.intersection(selected))
    result["retried_count"] = len(complete_ids.difference(selected))
    if error is not None and not selected:
        raise error
    return result


async def manual_backfill(sessions, client, mid, since, process, *, cap=20, page_budget=None, until=None):
    since_dt = datetime.fromisoformat(since) if isinstance(since, str) else since
    upper = until or now()
    if since_dt < HARD_SINCE or since_dt > upper:
        raise WorkflowError("outside_scope", "admission")
    budget = page_budget if page_budget is not None else {"remaining": 50}
    async with sessions() as session:
        blogger = await session.get(Blogger, mid)
        if blogger is None or not blogger.enabled:
            raise WorkflowError("blogger_disabled", "admission")
    if budget["remaining"] <= 0:
        return {"mid": mid, "processed": 0, "failed": 0, "attempted": 0, "has_more": True}
    batch_id = await create_batch(sessions, origin="manual_backfill", mid=mid, since=since_dt,
        until=upper, max_videos=min(200, cap), max_pages=min(200, budget["remaining"]))
    count, reached, error = 0, False, None
    seen = set()
    try:
        page = 1
        while budget["remaining"] > 0 and count < cap and not reached:
            budget["remaining"] -= 1
            def retry_token():
                if budget["remaining"] <= 0:
                    return False
                budget["remaining"] -= 1
                return True
            data = await client.get_video_list(mid, page=page, page_size=30, consume_retry_attempt=retry_token)
            items = data.get("list", {}).get("vlist", [])
            if not items:
                reached = True
                break
            fresh = [item for item in items if item.get("bvid") and item["bvid"] not in seen]
            if not fresh:
                raise WorkflowError("pagination_repeated", "discovery")
            for item in fresh:
                seen.add(item["bvid"])
                published = publication(item)
                if published is None:
                    continue
                if published < since_dt:
                    reached = True
                    continue
                if published > upper:
                    continue
                if count >= cap:
                    break
                if await admit(sessions, batch_id, mid, item, allow_legacy=True):
                    count += 1
            page += 1
    except Exception as exc:
        error = getattr(exc, "reason", type(exc).__name__)
    await freeze_batch(sessions, batch_id, complete=True)
    jobs = await due_work(sessions, limit=cap, batch_id=batch_id)
    result = await _process_selected(sessions, client, jobs, process)
    result = {**result, "mid": mid, "name": blogger.name, "batch_id": batch_id,
        "attempted": count, "has_more": not reached}
    if error is not None:
        result["error"] = error
        result["failed"] += 1
    return result


async def retry_ready(sessions, client, process, *, limit=50):
    jobs = await due_work(sessions, limit=min(200, limit) + 1)
    result = await _process_selected(sessions, client, jobs[:limit], process)
    result["has_more"] = len(jobs) > limit
    return result
