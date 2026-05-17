"""Mask sensitive config values + sanitize free-form text.

Two layers:

1. ``mask`` / ``safe_val`` / ``is_sensitive_key`` — used when we already know
   we're looking at a config key/value pair (configuration flatten, env vars).
   Masking is keyed off the *key name*.

2. ``sanitize_text`` — used when scanning free-form text (shell history lines,
   plugin error messages, systemd unit files, session message bodies). We don't
   know the structure, so we run a pattern-based scrubber. Best-effort: the
   patterns below cover the common token shapes (Anthropic/OpenAI sk-, GitHub
   ghp_/gho_/ghs_/github_pat_, npm npm_, AWS AKIA, ``Bearer xxx``, URL
   credentials, ``KEY=value`` with secret-ish key). It will miss bespoke or
   obfuscated formats — callers who need stronger guarantees should mask the
   whole field.

The ``--unmask`` flag, declared in ``ocdiag.cli``, propagates to call sites
that opt-in to honouring it (currently the session extract tool).
"""

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


# ── sanitize_text ──

# Token shapes worth scrubbing by themselves (no key=value context).
# Each pattern matches the *whole* secret; we replace with `<***>` keeping
# the leading prefix so the reader can still tell what kind of secret it was.
_TOKEN_PATTERNS = [
    # Anthropic / OpenAI style (`sk-...` / `sk-ant-...`)
    (re.compile(r"\b(sk-(?:ant-)?[A-Za-z0-9_\-]{16,})"), "sk-<***>"),
    # GitHub PAT family
    (re.compile(r"\b(gh[posu]_[A-Za-z0-9]{20,})"), "<gh-token>"),
    (re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"), "<github_pat>"),
    # npm
    (re.compile(r"\b(npm_[A-Za-z0-9]{30,})"), "<npm_token>"),
    # AWS access key id
    (re.compile(r"\b(AKIA[0-9A-Z]{16})"), "<AKIA-***>"),
    # Authorization headers
    (re.compile(r"(Bearer\s+)([A-Za-z0-9_\-\.=]{8,})", re.IGNORECASE), r"\1<***>"),
    # URLs with embedded credentials: scheme://user:pass@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+\-.]*://)([^/\s:@]+):([^/\s@]+)@"), r"\1<user>:<***>@"),
]

# KEY=VALUE / KEY: VALUE in free text where the key looks secret-ish.
# Use SENSITIVE_PATTERN over the key name; match value up to whitespace, quote,
# or end-of-line.  Three forms:
#   KEY=value         (env var, dotenv)
#   KEY="value"       (shell quoted)
#   KEY: value        (yaml-ish)
_KV_BARE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\-\.]*"
    r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH|PRIVATE|SIGNING)[A-Za-z0-9_\-\.]*)"
    r"\s*=\s*([^\s\"';#]+)",
    re.IGNORECASE,
)
_KV_QUOTED = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\-\.]*"
    r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH|PRIVATE|SIGNING)[A-Za-z0-9_\-\.]*)"
    r"\s*=\s*([\"'])([^\"']+)\2",
    re.IGNORECASE,
)
_KV_COLON = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\-\.]*"
    r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH|PRIVATE|SIGNING)[A-Za-z0-9_\-\.]*)"
    r"\s*:\s*([^\s\"';#,}\]]+)",
    re.IGNORECASE,
)


def sanitize_text(text: str, context: str = "generic") -> str:
    """Scrub well-known secret shapes from free-form text.

    Best-effort, not a guarantee. Returns the text unchanged if it's not a str.
    """
    if not isinstance(text, str) or not text:
        return text

    # Order: longer/more-specific (KV with quotes) first, then bare KV, then
    # bare token shapes. KV passes also catch things like `API_KEY=abc` where
    # the value would not match a token pattern.
    def _kv_quoted_sub(m):
        return f"{m.group(1)}={m.group(2)}<***>{m.group(2)}"

    def _kv_bare_sub(m):
        return f"{m.group(1)}=<***>"

    def _kv_colon_sub(m):
        return f"{m.group(1)}: <***>"

    text = _KV_QUOTED.sub(_kv_quoted_sub, text)
    text = _KV_BARE.sub(_kv_bare_sub, text)
    text = _KV_COLON.sub(_kv_colon_sub, text)
    for pat, repl in _TOKEN_PATTERNS:
        text = pat.sub(repl, text)
    return text
