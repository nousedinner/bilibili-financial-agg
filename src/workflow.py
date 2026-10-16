"""Explicit admission and stage checkpoints. Legacy rows never become a queue."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4
import math

from sqlalchemy import select, or_, and_, exists

from src.config import get_config
from src.credentials import credential_store
from src.retry_strategy import max_retries_for
from src.lifecycle import (HARD_SINCE, STAGES, WorkflowError, automatic_floor,
    validate_publication, now, fence, content_version)
from src.models import (WorkBatch, VideoWork, WorkStage, TranscriptCheckpoint,
    ASRSegment, DailyUsage, Blogger, Video, Transcript, Summary)

PAID_ANALYSIS_STAGES = frozenset({"analysis", "interaction_analysis"})


async def recover_abandoned_work(sessions):
    """Called only by the exclusive job holder, before it starts any pipeline."""
    from sqlalchemy import update
    async with sessions() as session:
        await fence(session)
        abandoned = select(VideoWork.bvid).where(VideoWork.state == "running")
        await session.execute(update(WorkStage).where(WorkStage.bvid.in_(abandoned),
            WorkStage.state == "running").values(state="failed", reason="interrupted", next_attempt_at=now()))
        await session.execute(update(VideoWork).where(VideoWork.state == "running").values(
            state="interrupted", reason="interrupted", next_attempt_at=now(), updated_at=now()))
        await session.commit()


def publication(payload):
    from zoneinfo import ZoneInfo
    try:
        timestamp = float(payload.get("created") or 0)
        return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) if timestamp > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


async def create_batch(sessions, *, origin, since, until, max_videos=20, max_pages=50, mid=None):
    if origin not in ("incremental", "manual_backfill", "retry", "auth_recovery"):
        raise ValueError("Invalid task origin")
    if since < HARD_SINCE or until < since or max_videos < 1 or max_videos > 200 or not 1 <= max_pages <= 200:
        raise ValueError("Invalid task scope")
    batch = WorkBatch(id=str(uuid4()), origin=origin, mid=mid, since=since, until=until,
        selected_bvids=[], max_videos=max_videos, max_pages=max_pages,
        max_audio_seconds=int(get_config().get("asr", {}).get("batch_audio_minutes", 60)) * 60,
        spent_audio_seconds=0, spent_pages=0, status="discovering")
    async with sessions() as session:
        await fence(session)
        session.add(batch)
        await session.commit()
    return batch.id


async def admit(sessions, batch_id, mid, payload, *, allow_legacy=False):
    bvid, published = payload.get("bvid"), publication(payload)
    if not isinstance(bvid, str) or not bvid or len(bvid) > 20:
        raise WorkflowError("invalid_bvid", "admission")
    async with sessions() as session:
        await fence(session)
        batch = (await session.execute(select(WorkBatch).where(WorkBatch.id == batch_id).with_for_update())).scalar_one()
        blogger = await session.get(Blogger, mid)
        if blogger is None or not blogger.enabled:
            raise WorkflowError("blogger_disabled", "admission")
        validate_publication(published, since=batch.since, until=batch.until)
        if batch.mid is not None and batch.mid != mid:
            raise WorkflowError("outside_scope", "admission")
        selected = list(batch.selected_bvids or [])
        if batch.status != "discovering" and bvid not in selected:
            raise WorkflowError("scope_frozen", "admission")
        if bvid not in selected and len(selected) >= batch.max_videos:
            return False
        old = await session.get(Video, bvid)
        work = (await session.execute(select(VideoWork).where(VideoWork.bvid == bvid)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if work and batch.origin == "incremental":
            return False
        if old is not None:
            if not allow_legacy:
                return False
            if old.fetch_status == "ok" and await session.get(Summary, bvid) and await session.get(Transcript, bvid):
                return False
        if work is not None and work.state == "running":
            raise WorkflowError("task_running", "admission")
        if bvid not in selected:
            selected.append(bvid)
            batch.selected_bvids = selected
        at = now()
        expires = at + timedelta(days=7)
        if batch.origin == "incremental":
            expires = min(expires, published + timedelta(days=int(get_config().get("data", {}).get("automatic_window_days", 7))))
        if expires <= at:
            raise WorkflowError("scope_expired", "admission")
        policy = {"llm_model": get_config().get("llm", {}).get("model", "mimo-v2.6-flash"),
            "asr_model": get_config().get("asr", {}).get("model", "mimo-v2.5-asr"), "prompt_version": "video-v3"}
        if work is None:
            work = VideoWork(bvid=bvid, mid=mid, batch_id=batch_id, origin=batch.origin,
                last_trigger=batch.origin, payload={**payload, "_policy": policy}, publish_time=published,
                expires_at=expires, credential_version=credential_store().current().version, state="pending")
            session.add(work)
            await session.flush()
            for name in STAGES:
                session.add(WorkStage(bvid=bvid, stage=name, state="pending", attempts=0))
        else:
            # Only an explicit manual selection may move an existing item into a
            # new finite batch. Successful stage checkpoints are not invalidated.
            if batch.origin != "manual_backfill":
                return False
            work.batch_id, work.origin, work.last_trigger = batch_id, batch.origin, batch.origin
            work.expires_at, work.state, work.reason = expires, "pending", None
            work.next_attempt_at = None
            work.credential_version = credential_store().current().version
            rows = (await session.execute(select(WorkStage).where(WorkStage.bvid == bvid))).scalars().all()
            for row in rows:
                if row.state not in ("complete", "no_track", "skipped"):
                    row.state, row.reason, row.next_attempt_at = "pending", None, None
                    if row.stage not in PAID_ANALYSIS_STAGES:
                        row.attempts = 0
        if old is None:
            session.add(Video(bvid=bvid, mid=mid, title=str(payload.get("title") or "")[:500],
                publish_time=published, fetch_status="pending", analysis_status="pending", retry_count=0))
        await session.commit()
    return True


async def freeze_batch(sessions, batch_id, *, complete=True, spent_pages=None):
    async with sessions() as session:
        await fence(session)
        batch = await session.get(WorkBatch, batch_id)
        if spent_pages is not None:
            batch.spent_pages = spent_pages
        batch.status = "ready" if complete else "partial_discovery"
        await session.commit()


async def due_work(sessions, limit=50, *, batch_id=None, mid=None):
    at = now()
    from src.authorization import auth_wait_enabled
    retry_states = ["pending", "retry", "interrupted"]
    if not await auth_wait_enabled(sessions):
        # A lost/unverified notification channel cannot leave admitted work
        # silently waiting forever. This resumes the existing item, not history.
        retry_states.append("blocked_auth")
    stage_due = exists(select(WorkStage.bvid).where(WorkStage.bvid == VideoWork.bvid,
        WorkStage.state == "failed", WorkStage.attempts < 3,
        or_(WorkStage.reason.is_(None), WorkStage.reason != "permanent"),
        or_(WorkStage.next_attempt_at.is_(None), WorkStage.next_attempt_at <= at)))
    query = select(VideoWork).where(VideoWork.mid.in_(select(Blogger.mid).where(Blogger.enabled == True)),
        VideoWork.publish_time >= HARD_SINCE, VideoWork.expires_at > at,
        or_(and_(VideoWork.state.in_(retry_states),
                 or_(VideoWork.next_attempt_at.is_(None), VideoWork.next_attempt_at <= at)),
            and_(VideoWork.state == "complete", stage_due)))
    if batch_id:
        query = query.where(VideoWork.batch_id == batch_id)
    if mid is not None:
        query = query.where(VideoWork.mid == mid)
    async with sessions() as session:
        return list((await session.execute(query.order_by(VideoWork.admitted_at, VideoWork.bvid).limit(limit))).scalars())


@dataclass
class WorkContext:
    sessions: object
    bvid: str
    credential_version: int
    current_stage: str = "metadata"
    cid: int = 0
    policy: dict = None
    generation: int = None
    terminal_reason: str = None

    async def check_scope(self, session):
        await fence(session)
        work = (await session.execute(select(VideoWork).where(VideoWork.bvid == self.bvid)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if work is None:
            raise WorkflowError("not_admitted", "admission")
        batch = await session.get(WorkBatch, work.batch_id)
        if batch is None or self.bvid not in (batch.selected_bvids or []) or work.expires_at <= now():
            raise WorkflowError("scope_expired", "admission")
        validate_publication(work.publish_time, since=batch.since, until=batch.until)
        if batch.mid is not None and batch.mid != work.mid:
            raise WorkflowError("outside_scope", "admission")
        if work.origin == "incremental":
            validate_publication(work.publish_time, since=automatic_floor(now(), get_config().get("data", {}).get("automatic_window_days", 7)))
        blogger = await session.get(Blogger, work.mid)
        if blogger is None or not blogger.enabled:
            raise WorkflowError("blogger_disabled", "admission")
        self.cid = work.cid or 0
        self.policy = work.payload.get("_policy", {})
        from src.lifecycle import owner_context
        ticket = owner_context.get()
        self.generation = ticket.generation if ticket else None
        return work

    async def finalize_scope(self, session):
        await fence(session)
        work = (await session.execute(select(VideoWork).where(VideoWork.bvid == self.bvid)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if work is not None and self.generation is not None and work.generation != self.generation:
            from src.lifecycle import OwnershipLost
            raise OwnershipLost()
        if work is not None:
            batch = await session.get(WorkBatch, work.batch_id)
            blogger = await session.get(Blogger, work.mid)
            if batch is None or self.bvid not in (batch.selected_bvids or []) or work.expires_at <= now():
                self.terminal_reason = "scope_expired"
            elif blogger is None or not blogger.enabled:
                self.terminal_reason = "blogger_disabled"
        return work

    async def stage(self, name, state, *, reason=None, result=None, input_version=None):
        async with self.sessions() as session:
            work = await self.check_scope(session)
            row = await self.update_stage(session, work, name, state, reason=reason,
                                          result=result, input_version=input_version)
            await session.commit()
        return row

    async def update_stage(self, session, work, name, state, *, reason=None, result=None, input_version=None):
        row = await session.get(WorkStage, (self.bvid, name))
        if row is None:
            row = WorkStage(bvid=self.bvid, stage=name, attempts=0, state="pending")
            session.add(row)
        if input_version and row.input_version and input_version != row.input_version:
            # A new comment/body version invalidates its cache, not its spending
            # limit. Manual re-admission must preserve these counters as well.
            if name not in PAID_ANALYSIS_STAGES:
                row.attempts = 0
            row.result = None
        if state == "running":
            if (row.attempts or 0) >= max_retries_for(row.reason or "unknown"):
                raise WorkflowError("retry_exhausted", name)
            row.attempts = (row.attempts or 0) + 1
            self.current_stage = name
        if state == "blocked_auth" and row.state == "running":
            row.attempts = max(0, (row.attempts or 0) - 1)
        row.state, row.reason, row.cid = state, reason, self.cid or work.cid
        row.credential_version, row.updated_at = self.credential_version, now()
        if input_version:
            row.input_version = input_version
        if result is not None:
            row.result = result
        if state == "failed":
            delay = 900 if reason == "risk_blocked" else 120 if reason == "http_blocked" else 60 * 2 ** min(row.attempts or 0, 5)
            row.next_attempt_at = now() + timedelta(seconds=delay)
        else:
            row.next_attempt_at = None
        return row

    async def get_stage(self, name):
        async with self.sessions() as session:
            return await session.get(WorkStage, (self.bvid, name))

    async def checkpoint(self, result):
        text = result.get("text")
        if not isinstance(text, str) or not text.strip() or result.get("partial") or not self.cid:
            raise WorkflowError("incomplete_transcript", "asr")
        version = content_version("transcript-v1", self.bvid, self.cid, result["source"], text, result["segments"])
        async with self.sessions() as session:
            work = await self.check_scope(session)
            if await session.get(TranscriptCheckpoint, version) is None:
                session.add(TranscriptCheckpoint(version=version, bvid=self.bvid, cid=self.cid,
                    source=result["source"], full_text=text, segment_count=result["segments"]))
            work.transcript_version = version
            await session.commit()
        return version

    async def load_segment(self, run_key, ordinal, input_hash):
        async with self.sessions() as session:
            await self.check_scope(session)
            row = await session.get(ASRSegment, (run_key, ordinal))
            if row and row.bvid == self.bvid and row.cid == self.cid and row.input_hash == input_hash:
                return row.text
        return None

    async def save_segment(self, run_key, ordinal, input_hash, start, end, model, text):
        async with self.sessions() as session:
            await self.check_scope(session)
            await session.merge(ASRSegment(run_key=run_key, ordinal=ordinal, input_hash=input_hash,
                bvid=self.bvid, cid=self.cid, start_seconds=start, end_seconds=end, model=model, text=text))
            await session.commit()

    async def reserve_audio(self, seconds):
        async with self.sessions() as session:
            work = await self.check_scope(session)
            batch = (await session.execute(select(WorkBatch).where(WorkBatch.id == work.batch_id).with_for_update())).scalar_one()
            day = now().date()
            usage = (await session.execute(select(DailyUsage).where(DailyUsage.day == day).with_for_update())).scalar_one_or_none()
            if usage is None:
                usage = DailyUsage(day=day, audio_seconds=0, llm_calls=0)
                session.add(usage)
            daily_cap = float(get_config().get("asr", {}).get("daily_audio_minutes", 180)) * 60
            if usage.audio_seconds + seconds > daily_cap or batch.spent_audio_seconds + seconds > batch.max_audio_seconds:
                raise WorkflowError("audio_budget", "asr")
            usage.audio_seconds += seconds
            batch.spent_audio_seconds += math.ceil(seconds)
            await session.commit()

    async def reserve_llm(self):
        async with self.sessions() as session:
            await self.check_scope(session)
            await _reserve_llm_in_transaction(session)
            await session.commit()


async def _reserve_llm_in_transaction(session):
    day = now().date()
    usage = (await session.execute(select(DailyUsage).where(DailyUsage.day == day).with_for_update())).scalar_one_or_none()
    if usage is None:
        usage = DailyUsage(day=day, audio_seconds=0, llm_calls=0)
        session.add(usage)
    if usage.llm_calls >= int(get_config().get("limits", {}).get("daily_llm_calls", 300)):
        raise WorkflowError("llm_budget", "analysis")
    usage.llm_calls += 1


async def reserve_global_llm(sessions):
    """Digest and other owned model requests share the video pipeline's daily cap."""
    async with sessions() as session:
        await fence(session)
        await _reserve_llm_in_transaction(session)
        await session.commit()
