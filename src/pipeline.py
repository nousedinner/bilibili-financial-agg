"""Process one admitted video; checkpoints are separate from published results."""
from datetime import timedelta

from src.lifecycle import (WorkflowError, OwnershipLost, failure_reason, content_version,
    work_context, owner_context, before_expensive_call, now)
from src.models import (Video, VideoWork, WorkStage, TranscriptCheckpoint, Transcript,
    Summary, CommentAnalysis, DanmakuAnalysis)
from src.workflow import WorkContext
from src.retry_strategy import should_retry


async def _interaction(context, name, fetch):
    stage = await context.get_stage(name)
    if stage and stage.state == "complete" and stage.cid == context.cid:
        return stage.result, False
    if stage and not should_retry(stage.attempts or 0, stage.reason or "unknown"):
        return None, False
    if stage and stage.next_attempt_at and stage.next_attempt_at > now():
        return None, False
    try:
        await context.stage(name, "running", input_version=content_version(context.bvid, context.cid))
        result = await fetch()
        if not isinstance(result, dict):
            raise WorkflowError("malformed_interaction", name)
        await context.stage(name, "complete", result=result)
        return result, True
    except (OwnershipLost,):
        raise
    except Exception as exc:
        await context.stage(name, "failed", reason=failure_reason(exc))
        return None, False


async def process(sessions, client, bvid, *, fetch_transcript, analyze_video,
                  analyze_interactions, validate_analysis, mark_digest):
    from src.analyzer import ANALYSIS_SCHEMA
    context = WorkContext(sessions, bvid, int(getattr(client, "credential_version", 1) or 1))
    token = work_context.set(context)
    published = False
    try:
        async with sessions() as session:
            work = await context.check_scope(session)
            payload = dict(work.payload)
            work.state, work.reason, work.updated_at = "running", None, now()
            ticket = owner_context.get()
            work.generation = ticket.generation if ticket else None
            await session.commit()

        metadata = await context.get_stage("metadata")
        if not metadata or metadata.state != "complete" or not context.cid:
            await context.stage("metadata", "running")
            info = await client.get_video_info(bvid)
            cid = info.get("cid") or (info.get("pages") or [{}])[0].get("cid")
            if not cid:
                raise WorkflowError("incomplete_info", "metadata")
            duration = info.get("duration") or payload.get("length", 0)
            if isinstance(duration, str):
                seconds = 0
                for part in duration.split(":"):
                    seconds = seconds * 60 + int(part or 0)
                duration = seconds
            duration = int(duration or 0)
            async with sessions() as session:
                work = await context.check_scope(session)
                if work.cid and work.cid != cid:
                    work.transcript_version = None
                work.cid = int(cid)
                payload["length"] = duration
                work.payload = payload
                video = await session.get(Video, bvid)
                if video.duration is None:
                    video.duration = duration
                await session.commit()
            context.cid = int(cid)
            await context.stage("metadata", "complete", result={"duration": duration, "cid": int(cid)})
        duration = int(payload.get("length") or (metadata.result or {}).get("duration", 0) if metadata else payload.get("length") or 0)

        async with sessions() as session:
            work = await context.check_scope(session)
            revision = await session.get(TranscriptCheckpoint, work.transcript_version) if work.transcript_version else None
            if revision and revision.cid != context.cid:
                revision = None
            legacy = await session.get(Transcript, bvid) if revision is None else None
        if revision is None:
            if legacy and legacy.full_text:
                result = {"source": legacy.source, "text": legacy.full_text, "segments": legacy.segment_count or 1}
            else:
                context.current_stage = "subtitle"
                result = await fetch_transcript(client, bvid, context.cid, duration)
            version = await context.checkpoint(result)
            await context.stage("asr", "complete" if result["source"] == "asr" else "skipped",
                reason=None if result["source"] == "asr" else "subtitle_available",
                input_version=version)
            async with sessions() as session:
                revision = await session.get(TranscriptCheckpoint, version)
        version = revision.version

        aid = int(payload.get("aid") or 0)
        async def comments_fetch():
            nonlocal aid
            if not aid:
                aid = await client.get_video_aid(bvid)
                if not aid:
                    raise WorkflowError("missing_aid", "comments")
                async with sessions() as session:
                    work = await context.check_scope(session)
                    work.payload = {**work.payload, "aid": aid}
                    video = await session.get(Video, bvid)
                    video.aid = aid
                    await session.commit()
            return await client.get_comments(oid=aid, oid_type=1, count=20)

        comments_data, comments_changed = await _interaction(context, "comments", comments_fetch)
        danmaku_data, danmaku_changed = await _interaction(context, "danmaku",
            lambda: client.get_danmaku(context.cid, max_count=2000))
        comments = comments_data.get("replies", []) if comments_data else []
        danmakus = danmaku_data.get("items", []) if danmaku_data else []
        interaction_version = content_version(comments_data, danmaku_data)
        body_version = content_version(version, context.policy)
        body_stage = await context.get_stage("analysis")
        cached = (body_stage and body_stage.state == "complete" and body_stage.input_version == body_version
                  and isinstance(body_stage.result, dict) and "analysis" in body_stage.result)
        if cached:
            analysis = dict(body_stage.result["analysis"])
            interaction_stage = await context.get_stage("interaction_analysis")
            if (not interaction_stage or interaction_stage.state != "complete"
                    or interaction_stage.input_version != interaction_version
                    or (interaction_stage.result or {}).get("_output_schema") != ANALYSIS_SCHEMA):
                try:
                    await context.stage("interaction_analysis", "running", input_version=interaction_version)
                    await before_expensive_call()
                    updated = await analyze_interactions(comments, danmakus)
                    if updated is None:
                        raise WorkflowError("llm_failed", "interaction_analysis")
                    analysis.update(updated)
                    await context.stage("interaction_analysis", "complete", result=updated, input_version=interaction_version)
                except OwnershipLost:
                    raise
                except Exception as exc:
                    reason = failure_reason(exc)
                    await context.stage("interaction_analysis", "failed", reason=reason)
                    raise WorkflowError(reason if reason == "retry_exhausted" else "llm_failed",
                                        "interaction_analysis") from exc
            else:
                analysis.update(interaction_stage.result or {})
        else:
            await context.stage("analysis", "running", input_version=body_version)
            await before_expensive_call()
            analysis = await analyze_video(str(payload.get("title") or ""), revision.full_text, comments, danmakus, duration)
            if analysis.get("analysis_failed") or not validate_analysis(analysis,
                    comment_samples=len(comments), danmaku_samples=len(danmakus)):
                raise WorkflowError("llm_failed", "analysis", detail=analysis.get("_failure"))
            await context.stage("analysis", "complete", input_version=body_version,
                                result={"analysis": analysis, "transcript_version": version})
            await context.stage("interaction_analysis", "complete", input_version=interaction_version,
                result={**{key: analysis[key] for key in ("comment_sentiment", "danmaku_sentiment", "comment_keywords", "danmaku_keywords")},
                        "_output_schema": ANALYSIS_SCHEMA, "_model_fields": analysis.get("_model_fields")})

        if analysis.get("analysis_failed") or not validate_analysis(analysis,
                comment_samples=len(comments), danmaku_samples=len(danmakus)):
            raise WorkflowError("llm_failed", "analysis", detail=analysis.get("_failure"))

        async with sessions() as session:
            work = await context.check_scope(session)
            if work.transcript_version != version:
                raise WorkflowError("input_changed", "analysis")
            await session.merge(Transcript(bvid=bvid, source=revision.source, full_text=revision.full_text,
                                          segment_count=revision.segment_count))
            fields = ("summary", "key_points", "sentiment", "sentiment_score", "risk_warnings", "data_citations", "tags")
            await session.merge(Summary(bvid=bvid, **{key: analysis[key] for key in fields}))
            if comments_data is not None:
                cs = analysis["comment_sentiment"]
                await session.merge(CommentAnalysis(ref_id=bvid, ref_type="video", total_count=comments_data["total"],
                    fetched_count=len(comments), sentiment_bullish=cs["bullish"] if cs else None, sentiment_bearish=cs["bearish"] if cs else None,
                    sentiment_neutral=cs["neutral"] if cs else None, hot_comments=[c.get("content", {}).get("message", "") for c in comments[:5]],
                    keywords=analysis["comment_keywords"]))
            if danmaku_data is not None:
                ds = analysis["danmaku_sentiment"]
                await session.merge(DanmakuAnalysis(bvid=bvid, total_count=danmaku_data["total"], sampled_count=len(danmakus),
                    sentiment_bullish=ds["bullish"] if ds else None, sentiment_bearish=ds["bearish"] if ds else None, sentiment_neutral=ds["neutral"] if ds else None,
                    keywords=analysis["danmaku_keywords"]))
            video = await session.get(Video, bvid)
            video.title, video.duration, video.view_count = str(payload.get("title") or "")[:500], duration, payload.get("play", 0)
            video.fetch_status, video.analysis_status = "ok", "completed"
            video.retry_count, video.error_type, video.error_message = 0, None, None
            video.fetched_at = now()
            if aid:
                video.aid = aid
            work.state, work.reason, work.updated_at = "complete", None, now()
            work.published_version = version
            work.next_attempt_at = None
            await mark_digest(session, work.publish_time)
            await session.commit()
            published = True
        return {"status": "complete", "transcript_version": version,
                "comments_status": "complete" if comments_data is not None else "unavailable",
                "danmaku_status": "complete" if danmaku_data is not None else "unavailable"}
    except OwnershipLost:
        # No writes are allowed after loss of the generation, including error writes.
        raise
    except BaseException as exc:
        import asyncio
        if not isinstance(exc, (Exception, asyncio.CancelledError)):
            raise
        reason = "interrupted" if isinstance(exc, asyncio.CancelledError) else failure_reason(exc)
        stage_name = getattr(exc, "stage", context.current_stage)
        if stage_name == "request":
            stage_name = context.current_stage
        state = "blocked_auth" if reason == "auth_required" else "skipped" if reason.startswith("policy_") else "failed"
        try:
            async with sessions() as session:
                # Finalization is fenced, but expiry/disabled-blogger checks must
                # not prevent an already running item from recording its outcome.
                work = await context.finalize_scope(session)
                if work is None:
                    raise exc
                if context.terminal_reason:
                    reason, state = context.terminal_reason, "skipped"
                if reason in ("scope_expired", "outside_scope", "blogger_disabled", "not_admitted"):
                    state = "skipped"
                stage = await context.update_stage(session, work, stage_name, state, reason=reason,
                    result={"failure": getattr(exc, "detail", None)} if getattr(exc, "detail", None) else None)
                work.reason, work.credential_version, work.updated_at = reason, context.credential_version, now()
                if state in ("blocked_auth", "skipped"):
                    work.state = state
                elif reason == "interrupted":
                    work.state = "interrupted"
                else:
                    work.state = "retry" if should_retry(stage.attempts or 0, reason) else "exhausted"
                work.next_attempt_at = stage.next_attempt_at
                video = await session.get(Video, bvid)
                public_summary, public_transcript = await session.get(Summary, bvid), await session.get(Transcript, bvid)
                if video is not None and not (public_summary and public_transcript):
                    video.fetch_status = "failed"
                    video.analysis_status = "failed" if stage_name.startswith("analysis") else "pending"
                    video.error_type = reason[:20]
                    video.error_message = f"{stage_name}: {reason}"
                    video.retry_count = stage.attempts or 0
                await session.commit()
        except OwnershipLost:
            raise
        if state == "skipped" and not isinstance(exc, asyncio.CancelledError):
            return {"status": "skipped", "reason": reason}
        raise
    finally:
        work_context.reset(token)
