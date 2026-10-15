"""Tri-state authorization health and a durable single-owner notification outbox."""
import asyncio
import base64
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import httpx
from sqlalchemy import select, func

from src.config import get_config, get_env
from src.credentials import credential_store
from src.lifecycle import now, fence, HARD_SINCE
from src.models import AuthState, AuthIncident, AlertDelivery, AppDevice, VideoWork, WorkStage


def _event(session, incident, kind, version, at):
    suffix = at.date().isoformat() if kind == "reminder" else kind
    event_id = f"{incident.id}:{suffix}"
    session.add(AlertDelivery(id=event_id, incident_id=incident.id, kind=kind,
        credential_version=version, payload={"event_id": event_id, "incident_id": incident.id,
        "kind": kind, "credential_version": str(version), "route": "bili_auth",
        "occurred_at": at.isoformat()}, state="pending", created_at=at))


async def record_auth_check(sessions, version, state, reason=None):
    if state not in ("valid", "invalid", "unknown"):
        raise ValueError("Invalid authorization state")
    current_version = credential_store().current().version
    if version != current_version:
        return {"ignored": "stale_credential"}
    at = now()
    async with sessions() as session:
        await fence(session)
        if version != credential_store().current().version:
            return {"ignored": "stale_credential"}
        row = (await session.execute(select(AuthState).where(AuthState.id == 1).with_for_update())).scalar_one_or_none()
        if row and row.credential_version > version:
            return {"ignored": "stale_credential"}
        if row is None:
            row = AuthState(id=1, credential_version=version, state="unknown")
            session.add(row)
        previous_version = row.credential_version
        row.credential_version, row.state, row.reason, row.checked_at = version, state, reason, at
        incident = await session.get(AuthIncident, row.incident_id) if row.incident_id else None
        if state == "invalid":
            if incident is None or incident.resolved_at is not None:
                incident = AuthIncident(id=str(uuid4()), credential_version=version,
                    opened_at=at, last_reminded_at=at)
                session.add(incident)
                row.incident_id = incident.id
                _event(session, incident, "invalid", version, at)
            elif at - incident.last_reminded_at >= timedelta(days=1):
                _event(session, incident, "reminder", version, at)
                incident.last_reminded_at = at
            row.last_confirmed = "invalid"
        elif state == "valid":
            row.last_confirmed, row.last_valid_at = "valid", at
            if incident and incident.resolved_at is None:
                incident.resolved_at, row.recovered_at = at, at
                _event(session, incident, "recovered", version, at)
                pending = (await session.execute(select(AlertDelivery).where(
                    AlertDelivery.incident_id == incident.id,
                    AlertDelivery.kind.in_(["invalid", "reminder"]),
                    AlertDelivery.state.in_(["pending", "retry", "waiting_configuration", "waiting_device"])))) .scalars().all()
                for delivery in pending:
                    delivery.state = "cancelled"
            # Recovery never selects from the legacy videos table. Even an explicit
            # old auth failure is ineligible after its admitted scope expires.
            if version > 1:
                jobs = (await session.execute(select(VideoWork).where(
                    VideoWork.state == "blocked_auth", VideoWork.credential_version < version,
                    VideoWork.expires_at > at, VideoWork.publish_time >= HARD_SINCE))).scalars().all()
                for job in jobs:
                    blocked_version = job.credential_version
                    job.state, job.reason, job.next_attempt_at = "pending", None, at
                    job.credential_version = version
                    job.last_trigger = "auth_recovery"
                    stages = (await session.execute(select(WorkStage).where(
                        WorkStage.bvid == job.bvid, WorkStage.state == "blocked_auth",
                        WorkStage.credential_version == blocked_version))).scalars().all()
                    for stage in stages:
                        stage.state, stage.reason, stage.next_attempt_at = "pending", None, at
                        stage.credential_version = version
        elif incident and incident.resolved_at is None and row.last_confirmed == "invalid":
            if at - incident.last_reminded_at >= timedelta(days=1):
                _event(session, incident, "reminder", version, at)
                incident.last_reminded_at = at
        # Unknown never resolves an existing incident or erases last_confirmed.
        await session.commit()
    return {"state": state, "credential_version": version}


async def check_authorization(sessions):
    from src.bilibili import BiliClient
    credential = credential_store().current()
    async with BiliClient(credential.sessdata, credential_version=credential.version,
                          cookie_extra=credential.cookie_header()) as client:
        result = await client.validate_sessdata()
    await record_auth_check(sessions, credential.version, result["state"], result.get("reason"))
    return result


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FcmSender:
    def __init__(self):
        self.path = get_env("FCM_SERVICE_ACCOUNT_FILE") or get_config().get("push", {}).get("service_account_file", "")
        self.access_token, self.expires_at = None, 0
        self.lock = asyncio.Lock()

    def configured(self):
        return bool(self.path and Path(self.path).is_file())

    async def _access(self, http):
        async with self.lock:
            if self.access_token and self.expires_at > time.time() + 60:
                return self.access_token
            account_path = Path(self.path)
            if os.name != "nt" and account_path.stat().st_mode & 0o077:
                raise ValueError("service_account_permissions")
            account = json.loads(account_path.read_text(encoding="utf-8"))
            if account.get("token_uri") != "https://oauth2.googleapis.com/token":
                raise ValueError("service_account_token_uri")
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            at = int(time.time())
            header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
            claims = _b64(json.dumps({"iss": account["client_email"],
                "scope": "https://www.googleapis.com/auth/firebase.messaging",
                "aud": account["token_uri"], "iat": at, "exp": at + 3600}, separators=(",", ":")).encode())
            unsigned = f"{header}.{claims}".encode()
            key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
            signature = _b64(key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256()))
            response = await http.post(account["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": unsigned.decode() + "." + signature})
            if response.status_code in (400, 401, 403):
                raise PushFailure("configuration_invalid")
            response.raise_for_status()
            self.access_token = response.json()["access_token"]
            self.expires_at = time.time() + int(response.json().get("expires_in", 3600))
            return self.access_token

    async def send(self, token, payload):
        if not self.configured():
            raise ValueError("push_not_configured")
        account = json.loads(Path(self.path).read_text(encoding="utf-8"))
        project = account.get("project_id", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project):
            raise ValueError("invalid_project_id")
        kind = payload["kind"]
        title = "服务器 B 站授权已恢复" if kind == "recovered" else "服务器 B 站授权需要处理"
        body = "授权已恢复，可继续采集。" if kind == "recovered" else "点击查看授权状态并重新授权。"
        if kind == "test":
            title, body = "财经聚合通知测试", "点击此通知确认手机已收到服务器推送。"
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
            access = await self._access(http)
            response = await http.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                headers={"Authorization": "Bearer " + access}, json={"message": {
                    "token": token, "data": {k: str(v) for k, v in payload.items()},
                    "notification": {"title": title, "body": body},
                    "android": {"priority": "high", "ttl": "3600s", "collapse_key": "bili_auth",
                        "notification": {"channel_id": "server_auth", "tag": "bili_auth"}}}})
            if response.status_code >= 400:
                try:
                    details = response.json().get("error", {}).get("details", [])
                    unregistered = any(item.get("errorCode") == "UNREGISTERED" for item in details)
                except (ValueError, TypeError, AttributeError):
                    unregistered = False
                if unregistered:
                    raise PushFailure("device_unregistered")
                if response.status_code in (400, 401, 403, 404):
                    raise PushFailure("configuration_invalid")
                response.raise_for_status()


class PushFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


_sender = None


def push_sender():
    global _sender
    if _sender is None or _sender.path != (get_env("FCM_SERVICE_ACCOUNT_FILE") or get_config().get("push", {}).get("service_account_file", "")):
        _sender = FcmSender()
    return _sender


async def deliver_alerts(sessions):
    sender = push_sender()
    at = now()
    current_version = credential_store().current().version
    async with sessions() as session:
        await fence(session)
        device = await session.get(AppDevice, 1)
        rows = (await session.execute(select(AlertDelivery).where(
            AlertDelivery.state.in_(["pending", "retry", "waiting_configuration", "waiting_device"]),
            (AlertDelivery.next_attempt_at.is_(None) | (AlertDelivery.next_attempt_at <= at)))
            .order_by(AlertDelivery.created_at).limit(10))).scalars().all()
        events = []
        for row in rows:
            incident = await session.get(AuthIncident, row.incident_id) if row.kind != "test" else None
            if row.kind in ("invalid", "reminder") and (
                row.credential_version != current_version or (incident and incident.resolved_at is not None)):
                row.state = "cancelled"
                continue
            if not sender.configured():
                row.state, row.error_code = "waiting_configuration", "push_not_configured"
                row.next_attempt_at = at + timedelta(minutes=30)
            elif not device or not device.notification_enabled:
                row.state, row.error_code = "waiting_device", "notification_not_enabled"
                row.next_attempt_at = at + timedelta(minutes=30)
            else:
                row.state = "sending"
                row.attempts += 1
                row.payload = {**row.payload, "device_hash": hashlib.sha256(device.token.encode()).hexdigest()}
                events.append((row.id, dict(row.payload), device.token))
        await session.commit()
    for event_id, payload, token in events:
        try:
            # Recheck after the outbox read, before external delivery.
            if payload["kind"] in ("invalid", "reminder") and int(payload["credential_version"]) != credential_store().current().version:
                state, error = "cancelled", None
            else:
                await sender.send(token, payload)
                state, error = "submitted", None
        except PushFailure as exc:
            state, error = "waiting_device" if exc.reason == "device_unregistered" else "waiting_configuration", exc.reason
        except (ValueError, KeyError, OSError):
            state, error = "waiting_configuration", "configuration_invalid"
        except Exception as exc:
            state, error = "retry", type(exc).__name__
        async with sessions() as session:
            await fence(session)
            row = await session.get(AlertDelivery, event_id)
            row.state, row.error_code = state, error
            if state in ("waiting_device", "waiting_configuration"):
                device = await session.get(AppDevice, 1)
                if device and hashlib.sha256(device.token.encode()).hexdigest() == payload.get("device_hash"):
                    device.verified_at = None
            if state == "submitted":
                row.submitted_at = now()
            else:
                row.next_attempt_at = now() + timedelta(seconds=min(3600, 30 * 2 ** min(row.attempts, 7)))
            await session.commit()
    return {"selected": len(events)}


async def auth_wait_enabled(sessions):
    if get_config().get("asr", {}).get("auth_failure_policy", "continue") != "wait":
        return False
    if sessions is None or not push_sender().configured():
        return False
    async with sessions() as session:
        device = await session.get(AppDevice, 1)
        return bool(device and device.notification_enabled and device.verified_at)


async def public_auth_status(sessions):
    version = credential_store().current().version
    async with sessions() as session:
        row = await session.get(AuthState, 1)
        device = await session.get(AppDevice, 1)
        pending = (await session.execute(select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.state.in_(["pending", "retry", "waiting_configuration", "waiting_device", "sending"])))) .scalar()
        return {"state": row.state if row and row.credential_version == version else "unknown",
            "last_confirmed": row.last_confirmed if row else None,
            "reason": row.reason if row and row.credential_version == version else None,
            "credential_version": version,
            "checked_at": row.checked_at.isoformat() if row and row.checked_at else None,
            "recovered_at": row.recovered_at.isoformat() if row and row.recovered_at else None,
            "incident_id": row.incident_id if row else None,
            "push": {"configured": push_sender().configured(), "registered": device is not None,
                "notification_enabled": bool(device and device.notification_enabled),
                "verified": bool(device and device.verified_at), "pending": pending}}
