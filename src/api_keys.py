"""One-way storage for high-entropy connection keys; never accept a digest as a key."""
from hashlib import sha256
import re


def key_digest(key):
    return "sha256:" + sha256(key.encode("utf-8")).hexdigest()


def is_key_digest(value):
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
