"""Shared failure semantics and immutable task boundaries (no network or DB)."""
from contextvars import ContextVar
from datetime import datetime, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

HARD_SINCE = datetime(2026, 7, 1)
STAGES = ("metadata", "subtitle", "asr", "comments", "danmaku", "analysis")
owner_context = ContextVar("pipeline_owner", default=None)
work_context = ContextVar("pipeline_work", default=None)


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def content_version(*parts):
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class WorkflowError(ValueError):
    def __init__(self, reason, stage, *, delay=0):
        super().__init__(f"{stage}: {reason}")
        self.reason, self.stage, self.delay = reason, stage, delay


class OwnershipLost(WorkflowError):
    def __init__(self):
        super().__init__("ownership_lost", "runtime")


def failure_reason(exc):
    if isinstance(exc, WorkflowError):
        return exc.reason
    code = getattr(exc, "code", None)
    if code in (-101, -111, 401):
        return "auth_required"
    if code in (-352, -403):
        return "risk_blocked"
    if code in (-412, 403, 412, 429):
        return "http_blocked"
    if code == -404:
        return "permanent"
    if isinstance(exc, (TimeoutError, OSError)) or code in (-1, 408, 500, 502, 503, 504):
        return "transient"
    return "failed"


def automatic_floor(at, days=7):
    return max(HARD_SINCE, at - timedelta(days=max(1, min(int(days), 30))))


def validate_publication(published, *, since=HARD_SINCE, until=None):
    if published is None or published < max(HARD_SINCE, since):
        raise WorkflowError("outside_scope", "admission")
    if until is not None and published > until:
        raise WorkflowError("outside_scope", "admission")


async def before_expensive_call():
    owner = owner_context.get()
    if owner is not None:
        await owner.check()


async def fence(session):
    owner = owner_context.get()
    if owner is not None:
        await owner.fence(session)
