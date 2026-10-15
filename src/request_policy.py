"""Request-level pacing and separate HTTP/risk cooldowns, shared per event loop."""
import asyncio
from contextvars import ContextVar
import time
from urllib.parse import urlsplit
import weakref

from curl_cffi.requests import AsyncSession
from src.config import get_config
from src.lifecycle import WorkflowError

active_transport = ContextVar("bili_transport", default=None)
_policies = weakref.WeakKeyDictionary()


class TransportRouter:
    async def get(self, *args, **kwargs):
        session = active_transport.get()
        if session is not None:
            return await session.get(*args, **kwargs)
        # Standalone subtitle downloads have no account cookies to share.
        async with AsyncSession(impersonate="chrome") as temporary:
            return await temporary.get(*args, **kwargs)


class RequestPolicy:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.endpoint_blocks = {}
        self.risk_until = 0.0

    @staticmethod
    def endpoint(url):
        parsed = urlsplit(url)
        return (parsed.hostname, parsed.path)

    async def before(self, url):
        cfg = get_config().get("limits", {})
        async with self.lock:
            current = time.monotonic()
            remaining = self.risk_until - current
            if remaining > 0:
                raise WorkflowError("risk_blocked", "request", delay=remaining)
            remaining = self.endpoint_blocks.get(self.endpoint(url), 0) - current
            if remaining > 0:
                raise WorkflowError("http_blocked", "request", delay=remaining)
            delay = self.next_request - current
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_request = time.monotonic() + max(0.0, float(cfg.get("request_interval_seconds", 1.0)))

    def observe(self, url, code):
        cfg = get_config().get("limits", {})
        if code in (-352, -403):
            self.risk_until = time.monotonic() + float(cfg.get("risk_cooldown_seconds", 900))
        elif code in (-412, 412, 429):
            self.endpoint_blocks[self.endpoint(url)] = time.monotonic() + float(cfg.get("http_cooldown_seconds", 120))


def request_policy():
    loop = asyncio.get_running_loop()
    if loop not in _policies:
        _policies[loop] = RequestPolicy()
    return _policies[loop]
