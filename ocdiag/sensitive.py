"""Mask sensitive config values (keys, secrets, tokens)."""

from __future__ import annotations

import re

SENSITIVE_PATTERN = re.compile(
    r"(key|secret|token|password|credential|auth|private|signing)",
    re.IGNORECASE,
)

SENSITIVE_KEY_NAMES = {
    "apikey", "appkey", "appsecret", "secret",
    "token", "password", "encryptkey", "verificationtoken",
    "webhook", "accesstoken", "refreshtoken", "signingsecret",
    "clientsecret",
}


def mask(val) -> str:
    """Mask a sensitive value, preserving first/last 4 chars for long strings."""
    s = str(val)
    if len(s) <= 4:
        return "****"
    if len(s) <= 10:
        return s[:2] + "****"
    return s[:4] + "****" + s[-4:]


def is_sensitive_key(key_path: str) -> bool:
    """Check if a dotted config key path is sensitive."""
    last = key_path.rsplit(".", 1)[-1].lower().replace("-", "").replace("_", "")
    return last in SENSITIVE_KEY_NAMES or SENSITIVE_PATTERN.search(last) is not None


def safe_val(key: str, val, max_len: int = 300) -> str:
    """Return display-safe value: mask if sensitive, truncate if long."""
    if SENSITIVE_PATTERN.search(key):
        return mask(val) if val else '""'
    s = str(val)
    return s[:max_len] + "..." if len(s) > max_len else s
