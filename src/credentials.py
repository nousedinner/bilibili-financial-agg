"""One server-owned Bilibili credential, versioned and atomically replaced."""
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

from src.config import get_config, get_env


@dataclass(frozen=True, repr=False)
class Credential:
    version: int
    sessdata: str
    bili_jct: str = ""
    dedeuserid: str = ""
    refresh_token: str = ""

    def __repr__(self):
        return f"Credential(version={self.version}, configured={bool(self.sessdata)})"

    def cookie_header(self):
        parts = [("SESSDATA", self.sessdata), ("bili_jct", self.bili_jct), ("DedeUserID", self.dedeuserid)]
        return "; ".join(f"{key}={value}" for key, value in parts if value)


class CredentialStore:
    def __init__(self, path=None):
        self.path = Path(path or get_config().get("data", {}).get("credential_path", "/app/data/bili-credential.json"))
        self.lock = asyncio.Lock()

    def current(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return self._parse(data, int(data["version"]))
            except (OSError, ValueError, KeyError, TypeError):
                # Never silently fall back to a possibly revoked environment credential.
                raise RuntimeError("Stored Bilibili credential cannot be loaded") from None
        return Credential(1, get_env("SESSDATA"), get_env("BILI_JCT"), get_env("BILI_UID"))

    @staticmethod
    def _parse(data, version):
        if not isinstance(data, dict) or version < 1:
            raise ValueError("Invalid credential")
        values = {key: str(data.get(key) or "") for key in ("sessdata", "bili_jct", "dedeuserid", "refresh_token")}
        for value in values.values():
            if len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 or c == ';' for c in value):
                raise ValueError("Invalid credential format")
        if not values["sessdata"] or (values["dedeuserid"] and not values["dedeuserid"].isdigit()):
            raise ValueError("Invalid credential format")
        return Credential(version, **values)

    async def replace(self, candidate, *, expected_version):
        # Caller must verify the candidate online before entering this method.
        async with self.lock:
            previous = self.current()
            if previous.version != expected_version:
                raise ValueError("Credential changed; start a new authorization session")
            credential = self._parse(candidate, previous.version + 1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".bili-credential-", dir=self.path.parent)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(asdict(credential), stream, ensure_ascii=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return credential


_stores = {}


def credential_store():
    path = str(Path(get_config().get("data", {}).get("credential_path", "/app/data/bili-credential.json")).resolve())
    # The lock must be shared by all requests, not recreated for every replace.
    if path not in _stores:
        _stores[path] = CredentialStore(path)
    return _stores[path]
