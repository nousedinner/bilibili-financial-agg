"""Private short-lived checkpoints for one-use Bilibili login responses."""
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime
from uuid import UUID

from src.credentials import credential_store


def _path(session_id):
    name = str(UUID(session_id))
    return credential_store().path.parent / '.login-checkpoints' / (name + '.json')


def load(session_id, version, expires_at):
    path = _path(session_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != version or data.get('expires_at') != expires_at.isoformat():
        raise ValueError('Login checkpoint scope changed')
    return data['payload']


def save(session_id, version, expires_at, payload):
    path = _path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version': version, 'expires_at': expires_at.isoformat(), 'payload': payload}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def delete(session_id):
    _path(session_id).unlink(missing_ok=True)


def prune(at):
    directory = credential_store().path.parent / '.login-checkpoints'
    if not directory.exists():
        return
    for path in directory.glob('*.json'):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if str(UUID(path.stem)) != path.stem:
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if datetime.fromisoformat(data['expires_at']) <= at:
                path.unlink()
        except (ValueError, KeyError, TypeError, OSError):
            continue
