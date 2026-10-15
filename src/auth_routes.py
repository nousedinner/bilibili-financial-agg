"""Single-owner authorization and notification endpoints; no cookies in responses."""
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.authorization import (public_auth_status, record_auth_check, deliver_alerts,
                               check_authorization, push_sender)
from src.bilibili import BiliClient
from src.config import get_env
from src.credentials import credential_store, CredentialStore
from src.lifecycle import now, owner_context, fence
from src.models import AppDevice, AlertDelivery, LoginSession, Video, WorkBatch, VideoWork, DiscoveryScan


class DeviceInput(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    notification_enabled: bool


class LoginPoll(BaseModel):
    nonce: str = Field(min_length=20, max_length=128)


class ManualRetry(BaseModel):
    bvids: list[str] = Field(min_length=1, max_length=50)


def build_router(api_dependency, sessions_getter, guard_getter):
    router = APIRouter(dependencies=[Depends(api_dependency)])

    async def management(x_management_key: str | None = Header(None)):
        key = get_env("FIN_AGG_MANAGEMENT_KEY")
        if not key:
            from pathlib import Path
            path = Path(get_env("FIN_AGG_MANAGEMENT_KEY_FILE", "/app/data/management-key"))
            if path.is_file():
                key = path.read_text(encoding="utf-8").strip()
        if not key:
            raise HTTPException(503, "尚未配置管理密钥")
        if not x_management_key or not secrets.compare_digest(x_management_key, key):
            raise HTTPException(403, "需要独立管理密钥")

    @asynccontextmanager
    async def owned():
        guard = guard_getter()
        ticket = guard.ticket() if guard else None
        if ticket:
            await ticket.check()
        token = owner_context.set(ticket)
        try:
            yield
        finally:
            owner_context.reset(token)

    @router.get("/api/bili/auth/status")
    async def auth_status():
        return {"code": 0, "data": await public_auth_status(sessions_getter())}

    @router.get("/api/work/status")
    async def work_status():
        from sqlalchemy import func
        async with sessions_getter()() as session:
            counts = dict((await session.execute(select(VideoWork.state, func.count()).group_by(VideoWork.state))).all())
            scans = (await session.execute(select(DiscoveryScan).order_by(DiscoveryScan.until.desc()).limit(12))).scalars().all()
            return {"code": 0, "data": {"admitted": sum(counts.values()), "states": counts,
                "coverage": [{"mid": scan.mid, "state": scan.state,
                    "since": scan.since.isoformat(), "until": scan.until.isoformat(),
                    "gap_from": scan.gap_from.isoformat() if scan.gap_from else None,
                    "gap_until": scan.gap_until.isoformat() if scan.gap_until else None} for scan in scans]}}

    @router.post("/api/bili/auth/check", dependencies=[Depends(management)])
    async def auth_check():
        async with owned():
            await check_authorization(sessions_getter())
        return {"code": 0, "data": await public_auth_status(sessions_getter())}

    @router.post("/api/notifications/device")
    async def register_device(req: DeviceInput):
        async with owned(), sessions_getter()() as session:
            await fence(session)
            device = await session.get(AppDevice, 1)
            if device is None:
                device = AppDevice(id=1, token=req.token)
                session.add(device)
            if device.token != req.token or not req.notification_enabled:
                device.verified_at = None
            device.token, device.notification_enabled, device.updated_at = req.token, req.notification_enabled, now()
            pending = (await session.execute(select(AlertDelivery).where(
                AlertDelivery.state.in_(["waiting_device", "waiting_configuration"])))).scalars().all()
            for event in pending:
                event.next_attempt_at = now()
            await session.commit()
        return {"code": 0, "data": {"registered": True, "notification_enabled": req.notification_enabled}}

    @router.get("/api/notifications/events")
    async def notification_events():
        async with sessions_getter()() as session:
            rows = (await session.execute(select(AlertDelivery).where(
                AlertDelivery.state != "cancelled").order_by(AlertDelivery.created_at.desc()).limit(20))).scalars().all()
            return {"code": 0, "data": [{"id": row.id, "kind": row.kind, "state": row.state,
                "credential_version": row.credential_version, "created_at": row.created_at.isoformat(),
                "acknowledged": row.acknowledged_at is not None} for row in rows]}

    @router.post("/api/notifications/test")
    async def test_notification():
        if not push_sender().configured():
            raise HTTPException(503, "服务器尚未配置推送服务；不能用本地弹窗代替推送验收")
        event_id = "test:" + str(uuid4())
        async with owned():
            async with sessions_getter()() as session:
                await fence(session)
                device = await session.get(AppDevice, 1)
                if device is None or not device.notification_enabled:
                    raise HTTPException(409, "请先启用并登记手机通知")
                version = credential_store().current().version
                session.add(AlertDelivery(id=event_id, incident_id=event_id, kind="test", credential_version=version,
                    payload={"event_id": event_id, "kind": "test", "route": "bili_auth", "credential_version": str(version)},
                    state="pending"))
                await session.commit()
            await deliver_alerts(sessions_getter())
        return {"code": 0, "data": {"id": event_id, "message": "请将 APP 退到后台，实际收到后点击服务器测试通知完成确认"}}

    @router.post("/api/notifications/events/{event_id}/ack")
    async def acknowledge(event_id: str):
        async with owned(), sessions_getter()() as session:
            await fence(session)
            event = await session.get(AlertDelivery, event_id)
            if event is None:
                raise HTTPException(404, "通知不存在")
            event.acknowledged_at = now()
            if event.kind == "test" and event.state == "submitted":
                device = await session.get(AppDevice, 1)
                if device and device.notification_enabled and event.payload.get("device_hash") == hashlib.sha256(device.token.encode()).hexdigest():
                    device.verified_at = now()
            await session.commit()
        return {"code": 0, "data": {"acknowledged": True}}

    @router.post("/api/bili/auth/sessions", dependencies=[Depends(management)])
    async def create_login():
        async with owned():
            async with BiliClient() as client:
                result = await client._get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate", signed=False)
            data = result.get("data") or {}
            qr_url, qr_key = data.get("url", ""), data.get("qrcode_key", "")
            if urlsplit(qr_url).hostname != "passport.bilibili.com" or urlsplit(qr_url).scheme != "https" or not qr_key:
                raise HTTPException(502, "上游返回的授权会话无效")
            session_id, nonce = str(uuid4()), secrets.token_urlsafe(32)
            async with sessions_getter()() as session:
                await fence(session)
                session.add(LoginSession(id=session_id, nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                    qrcode_key=qr_key, credential_version=credential_store().current().version,
                    state="pending", expires_at=now() + timedelta(minutes=3)))
                await session.commit()
        return {"code": 0, "data": {"id": session_id, "nonce": nonce, "qr_url": qr_url, "expires_in": 180}}

    @router.post("/api/bili/auth/sessions/{session_id}/poll", dependencies=[Depends(management)])
    async def poll_login(session_id: str, req: LoginPoll):
        async with owned():
            async with sessions_getter()() as session:
                await fence(session)
                row = (await session.execute(select(LoginSession).where(LoginSession.id == session_id).with_for_update())).scalar_one_or_none()
                if row is None or not secrets.compare_digest(row.nonce_hash, hashlib.sha256(req.nonce.encode()).hexdigest()):
                    raise HTTPException(404, "授权会话不存在")
                if row.state == "complete":
                    return {"code": 0, "data": {"state": "complete"}}
                if row.expires_at <= now() or row.state == "expired":
                    raise HTTPException(410, "二维码已过期，请重新发起授权")
                if row.last_polled_at and now() - row.last_polled_at < timedelta(seconds=3):
                    raise HTTPException(429, "请稍后检查授权结果")
                row.last_polled_at = now()
                qr_key, expected_version = row.qrcode_key, row.credential_version
                await session.commit()
            async with BiliClient() as client:
                result = await client._get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    {"qrcode_key": qr_key}, signed=False)
            data = result.get("data") or {}
            code = data.get("code")
            if code in (86101, 86090):
                return {"code": 0, "data": {"state": "awaiting_scan" if code == 86101 else "awaiting_confirmation"}}
            if code != 0:
                raise HTTPException(410 if code == 86038 else 502, "授权会话已过期或上游响应异常")
            query = parse_qs(urlsplit(data.get("url", "")).query)
            candidate = {"sessdata": (query.get("SESSDATA") or [""])[0],
                "bili_jct": (query.get("bili_jct") or [""])[0],
                "dedeuserid": (query.get("DedeUserID") or [""])[0],
                "refresh_token": str(data.get("refresh_token") or "")}
            if not candidate["bili_jct"] or not candidate["dedeuserid"].isdigit() or int(candidate["dedeuserid"]) <= 0:
                raise HTTPException(502, "授权响应缺少账号或会话字段")
            try:
                parsed = CredentialStore._parse(candidate, expected_version + 1)
            except ValueError:
                raise HTTPException(502, "授权响应缺少有效凭据") from None
            async with BiliClient(parsed.sessdata, cookie_extra=parsed.cookie_header()) as client:
                validation = await client.validate_sessdata()
                if validation["state"] != "valid":
                    raise HTTPException(502, "候选凭据尚未通过验证，已保留原有授权")
                # nav account identity must be checked separately from a user-supplied cookie label.
                nav = await client._get("https://api.bilibili.com/x/web-interface/nav", signed=False)
                actual_uid = str(nav.get("data", {}).get("mid", ""))
                if actual_uid != candidate["dedeuserid"] or (get_env("BILI_UID") and actual_uid != get_env("BILI_UID")):
                    raise HTTPException(409, "授权账号与服务器采集账号不一致")
            async with sessions_getter()() as session:
                await fence(session)
                try:
                    credential = await credential_store().replace(candidate, expected_version=expected_version)
                except ValueError:
                    raise HTTPException(409, "授权版本已变化，请重新发起登录") from None
                row = await session.get(LoginSession, session_id)
                row.state, row.qrcode_key, row.nonce_hash = "complete", "", ""
                await session.commit()
            await record_auth_check(sessions_getter(), credential.version, "valid")
        return {"code": 0, "data": {"state": "complete", "credential_version": credential.version}}

    @router.post("/api/work/retry", dependencies=[Depends(management)])
    async def explicit_retry(req: ManualRetry):
        from src.workflow import create_batch, admit, freeze_batch
        from src.lifecycle import HARD_SINCE
        from src.fetcher import _video_info
        bvids = list(dict.fromkeys(req.bvids))
        async with owned():
            async with sessions_getter()() as session:
                videos = list((await session.execute(select(Video).where(Video.bvid.in_(bvids)))).scalars())
            if len(videos) != len(bvids) or any(not video.publish_time or video.publish_time < HARD_SINCE for video in videos):
                raise HTTPException(400, "存在越界或不存在的视频")
            batch_id = await create_batch(sessions_getter(), origin="manual_backfill", since=HARD_SINCE,
                until=now(), max_videos=len(bvids), max_pages=1)
            admitted = 0
            for video in videos:
                admitted += bool(await admit(sessions_getter(), batch_id, video.mid, _video_info(video), allow_legacy=True))
            await freeze_batch(sessions_getter(), batch_id)
        return {"code": 0, "data": {"batch_id": batch_id, "admitted": admitted, "selected": bvids}}

    return router
