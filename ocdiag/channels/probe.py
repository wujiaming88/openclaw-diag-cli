"""Active probe + secret resolution.

This module is only invoked when the operator runs with ``--probe`` —
the rest of the channel collector stays read-only and offline. The
probe path itself is also strictly read-only: we hit each platform's
canonical *token-introspection* endpoint, which validates the
credential against a real API server but does NOT send messages,
mutate state, or consume usage quota beyond a single auth call.

Secret resolution
-----------------
OpenClaw stores credentials as ``SecretRef`` objects, e.g.

    "appSecret": {"source": "file",
                  "provider": "lark-secrets",
                  "id": "/lark/appSecret"}

There is no public CLI that prints these in plaintext (``openclaw
config get`` returns ``__OPENCLAW_REDACTED__`` on purpose). The only
zero-dependency way to actually probe credential validity is to read
the underlying secrets store ourselves. We support the two common
shapes:

  - ``source: "env"``   →  ``os.environ[id]``
  - ``source: "file"``  →  read ``provider.path`` JSON, address with
                            ``id`` as a JSON pointer (default
                            ``mode: "json"``); ``mode: "singleValue"``
                            treats the entire file as the secret.

``source: "exec"`` is intentionally NOT supported in P0 — it would
spawn an external process whose security policy varies per host. The
probe reports ``SECRET_UNRESOLVED`` and skips the network call rather
than guessing.

Memory hygiene: resolved plaintext secrets are returned as plain
strings, fed straight into the urllib request, and never copied into
``Report.data``, ``evidence``, or any structured field. The variant
diagnostic surfaces only ``ref:<source>:<provider>`` metadata so a
human reader sees *where* the secret comes from but never *what* it
is. Even the secret length is intentionally not surfaced (length is
useful as a brute-force oracle for some token formats).
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# Hard timeout for token-probe HTTP calls. Matches the upstream plugin's
# probe budget (10s). Long enough for a slow corporate proxy / first DNS
# resolve; short enough to not stall ``channel --probe`` for minutes.
PROBE_TIMEOUT_S = 10.0


# ─── secret resolution ───────────────────────────────────────────────


@dataclass
class SecretResolution:
    ok: bool
    value: Optional[str] = None      # ONLY held in memory while probing
    code: Optional[str] = None       # error code on failure
    msg: Optional[str] = None        # human-readable error
    ref_label: Optional[str] = None  # e.g. "ref:file:lark-secrets"


def _expand_path(p: str) -> str:
    """Expand ~ and env vars in a secrets-store path.

    OpenClaw config files routinely use ``~`` for the home dir; expand
    explicitly so we don't pass a literal tilde down to ``open()``.
    """
    return os.path.expanduser(os.path.expandvars(p))


def _decode_json_pointer_token(token: str) -> str:
    # JSON Pointer RFC 6901: ``~1`` decodes to ``/`` and ``~0`` decodes to
    # ``~``. Order matters — ``~1`` first so the literal ``~`` it leaves
    # behind isn't double-decoded.
    return token.replace("~1", "/").replace("~0", "~")


def _read_json_pointer(root: Any, pointer: str) -> Tuple[bool, Any, str]:
    """Address ``pointer`` (RFC 6901) into ``root``; return (ok, value, err)."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None, (
            "secret id must be an absolute JSON pointer "
            "(e.g. \"/providers/openai/apiKey\")"
        )
    cur = root
    tokens = pointer[1:].split("/")
    for raw in tokens:
        token = _decode_json_pointer_token(raw)
        if isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError:
                return False, None, f"non-integer index '{token}' on array"
            if idx < 0 or idx >= len(cur):
                return False, None, f"index {idx} out of bounds"
            cur = cur[idx]
            continue
        if not isinstance(cur, dict) or token not in cur:
            return False, None, f"key '{token}' not found"
        cur = cur[token]
    return True, cur, ""


def resolve_secret_ref(
    ref: Any,
    cfg: Dict[str, Any],
) -> SecretResolution:
    """Resolve a ``SecretRef`` against ``cfg.secrets.providers``.

    Accepts either a plain string (already-resolved literal — return as
    is) or a ``{source, provider, id}`` dict. URI-string refs
    (``env://NAME``) are explicitly *not* supported in P0; they surface
    as ``SECRET_UNRESOLVED`` so the caller knows to skip the probe.
    """
    # Already-plain literal — caller stuffed the secret into config
    # directly. Honor it.
    if isinstance(ref, str):
        # env:// or file:// URI shorthand is NOT supported in P0.
        if ref.startswith(("env://", "file://", "exec://")):
            return SecretResolution(
                ok=False,
                code="SECRET_UNRESOLVED",
                msg=f"URI-string SecretRef ({ref.split('://')[0]}://) "
                    "not supported in P0",
                ref_label=f"ref:{ref.split('://')[0]}",
            )
        return SecretResolution(ok=True, value=ref, ref_label="literal")

    if not isinstance(ref, dict):
        return SecretResolution(
            ok=False, code="SECRET_UNRESOLVED",
            msg="appSecret is neither string nor SecretRef object",
        )

    source = ref.get("source")
    provider = ref.get("provider")
    sid = ref.get("id")
    label = f"ref:{source}:{provider}" if source else "ref:unknown"

    if not source or not sid:
        return SecretResolution(
            ok=False, code="SECRET_UNRESOLVED",
            msg="SecretRef missing source/id", ref_label=label,
        )

    if source == "env":
        # provider is conventional ("default"); the secret store is
        # process env. id is the variable name.
        val = os.environ.get(sid)
        if val:
            return SecretResolution(ok=True, value=val, ref_label=label)
        return SecretResolution(
            ok=False, code="SECRET_UNRESOLVED",
            msg=f"env var {sid} not set", ref_label=label,
        )

    if source == "file":
        providers = (
            cfg.get("secrets", {}).get("providers", {})
            if isinstance(cfg, dict) else {}
        )
        prov_cfg = providers.get(provider) if isinstance(providers, dict) else None
        if not isinstance(prov_cfg, dict):
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg=f"secrets.providers.{provider} not configured",
                ref_label=label,
            )
        path = prov_cfg.get("path")
        if not isinstance(path, str) or not path:
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg=f"secrets.providers.{provider}.path missing",
                ref_label=label,
            )
        mode = prov_cfg.get("mode", "json")  # default "json" per upstream
        full_path = _expand_path(path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg=f"secrets file unreadable: {type(e).__name__}",
                ref_label=label,
            )
        if mode == "singleValue":
            return SecretResolution(
                ok=True, value=raw.strip(), ref_label=label,
            )
        # mode == "json"
        try:
            doc = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg=f"secrets file not JSON: {type(e).__name__}",
                ref_label=label,
            )
        ok, val, err = _read_json_pointer(doc, sid)
        if not ok:
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg=f"JSON pointer lookup failed: {err}",
                ref_label=label,
            )
        if not isinstance(val, str) or not val:
            return SecretResolution(
                ok=False, code="SECRET_UNRESOLVED",
                msg="resolved value is not a non-empty string",
                ref_label=label,
            )
        return SecretResolution(ok=True, value=val, ref_label=label)

    if source == "exec":
        # Intentionally not implemented in P0 — would spawn external
        # process whose policy varies per host. Surface honestly.
        return SecretResolution(
            ok=False, code="SECRET_UNRESOLVED",
            msg="source=exec not supported in P0",
            ref_label=label,
        )

    return SecretResolution(
        ok=False, code="SECRET_UNRESOLVED",
        msg=f"unknown source: {source}",
        ref_label=label,
    )


# ─── HTTP probe ──────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    state: str                       # "valid" | "invalid" | "unreachable" | "skipped"
    code: Optional[str] = None       # short machine code
    msg: Optional[str] = None        # human reason
    api_code: Optional[int] = None   # platform's own response code (when reachable)
    domain: Optional[str] = None     # endpoint domain we hit
    extra: Dict[str, Any] = field(default_factory=dict)


def _post_json(url: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str]:
    """POST a JSON body, return (http_status, parsed_body, error_msg).

    On parse / network failure, http_status is 0 and error_msg is set.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "openclaw-diag/channel-probe",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S, context=ctx) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # HTTP 4xx/5xx — treat as reachable with non-2xx status; we'll
        # return the status code so the caller can decide.
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            return e.code, json.loads(raw) if raw else {}, ""
        except (ValueError, json.JSONDecodeError):
            return e.code, {}, raw[:200]
    except urllib.error.URLError as e:
        # DNS, refused, TLS, etc. — UNREACHABLE.
        return 0, {}, f"URLError: {e.reason}"
    except (socket.timeout, TimeoutError):
        return 0, {}, "timeout"
    except OSError as e:
        return 0, {}, f"OSError: {e}"
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        parsed = {}
    return status, parsed, ""


def feishu_token_probe(
    app_id: str, app_secret: str, domain: str = "feishu",
) -> ProbeResult:
    """Probe a Feishu / Lark tenant_access_token endpoint.

    Endpoint per OpenAPI docs (and confirmed in upstream plugin's
    streaming-card token loader):

      POST {base}/auth/v3/tenant_access_token/internal
      Content-Type: application/json
      {"app_id": "...", "app_secret": "..."}

    Domain mapping mirrors upstream's ``resolveApiBase``:
      - ``feishu`` (default) → https://open.feishu.cn
      - ``lark``             → https://open.larksuite.com
      - explicit https URL  → used verbatim (self-deployed instances)

    Result classification:
      - ``valid``        — code==0 and a tenant_access_token was issued
      - ``invalid``      — reachable but Feishu rejected (any non-zero code)
      - ``unreachable``  — network error, timeout, TLS failure, DNS, etc.
                           NEVER mark as ``invalid`` — we cannot tell
                           whether the credential is wrong or whether
                           we just can't see Feishu right now.
    """
    if domain == "lark":
        base = "https://open.larksuite.com"
    elif domain and domain != "feishu" and domain.startswith("http"):
        base = domain.rstrip("/")
    else:
        base = "https://open.feishu.cn"
    url = f"{base}/open-apis/auth/v3/tenant_access_token/internal"

    status, body, err = _post_json(
        url, {"app_id": app_id, "app_secret": app_secret},
    )
    if err:
        return ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg=err, domain=base,
        )
    api_code = body.get("code") if isinstance(body, dict) else None
    api_msg = body.get("msg") if isinstance(body, dict) else None
    if status == 200 and api_code == 0 and body.get("tenant_access_token"):
        # Don't return the token itself — caller doesn't need it for
        # diagnosis. ``expire`` is metadata; safe to surface.
        return ProbeResult(
            state="valid", api_code=0, domain=base,
            extra={"expire_s": body.get("expire")},
        )
    if status == 200 and isinstance(api_code, int) and api_code != 0:
        return ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=api_code, msg=str(api_msg)[:200],
            domain=base,
        )
    # Got a response but couldn't classify — treat as unreachable so
    # we don't false-positive a credential rejection from a misrouted
    # response.
    return ProbeResult(
        state="unreachable", code="PROBE_UNREACHABLE",
        msg=f"unexpected response status={status} code={api_code}",
        domain=base,
    )


def _get_json(url: str) -> Tuple[int, Dict[str, Any], str]:
    """GET ``url``, return (http_status, parsed_body, error_msg).

    Mirrors ``_post_json`` for endpoints whose canonical shape is GET
    with credentials in the query string (notably WeCom's
    ``/cgi-bin/gettoken``). On parse / network failure, http_status is
    0 and error_msg is set.
    """
    req = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "openclaw-diag/channel-probe"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S, context=ctx) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            return e.code, json.loads(raw) if raw else {}, ""
        except (ValueError, json.JSONDecodeError):
            return e.code, {}, raw[:200]
    except urllib.error.URLError as e:
        return 0, {}, f"URLError: {e.reason}"
    except (socket.timeout, TimeoutError):
        return 0, {}, "timeout"
    except OSError as e:
        return 0, {}, f"OSError: {e}"
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        parsed = {}
    return status, parsed, ""


def dingtalk_token_probe(
    client_id: str, client_secret: str,
) -> ProbeResult:
    """Probe a DingTalk OAuth2 access-token endpoint.

    Endpoint per upstream ``probe.ts:101-104`` (verified literal):

      POST https://api.dingtalk.com/v1.0/oauth2/accessToken
      Content-Type: application/json
      {"appKey": "...", "appSecret": "..."}

    Result classification:
      - ``valid``        — response carries ``accessToken``
      - ``invalid``      — reachable, but DingTalk returned ``errcode!=0``
                           or no token (typical for bad clientId/secret)
      - ``unreachable``  — network error / timeout / DNS / TLS failure;
                           NEVER promote to ``invalid`` (we cannot
                           distinguish "creds bad" from "no internet").
    """
    url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    base = "https://api.dingtalk.com"
    status, body, err = _post_json(
        url, {"appKey": client_id, "appSecret": client_secret},
    )
    if err:
        return ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg=err, domain=base,
        )
    if not isinstance(body, dict):
        return ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg=f"non-dict response status={status}", domain=base,
        )
    access_token = body.get("accessToken")
    api_errcode = body.get("code")
    api_errmsg = body.get("message")
    if status == 200 and access_token:
        return ProbeResult(
            state="valid", api_code=0, domain=base,
            extra={"expire_s": body.get("expireIn")},
        )
    # No token returned. The DingTalk gateway uses a top-level ``code``
    # string + ``message`` (e.g. ``"InvalidAuthentication"``) — we
    # surface it but treat any non-2xx or token-absent reply as invalid.
    if status >= 400 or api_errcode is not None or api_errmsg is not None:
        # api_code can be a string (e.g. "InvalidAuthentication"); coerce
        # for storage but keep the original message for the operator.
        coerced_code = api_errcode if isinstance(api_errcode, int) else status
        return ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=coerced_code,
            msg=str(api_errmsg or api_errcode or f"status={status}")[:200],
            domain=base,
            extra={"raw_code": api_errcode} if api_errcode is not None else {},
        )
    return ProbeResult(
        state="unreachable", code="PROBE_UNREACHABLE",
        msg=f"unexpected response status={status}", domain=base,
    )


def wecom_agent_token_probe(
    corp_id: str, corp_secret: str,
) -> ProbeResult:
    """Probe a WeCom Agent (自建应用) gettoken endpoint.

    **Bot mode is not probed** — the WS Bot path doesn't expose a
    simple HTTP credential-introspection endpoint, so the variant
    diagnostic surfaces ``WECOM_BOT_NO_PROBE_ENDPOINT`` info there
    instead of calling this helper.

    Endpoint per upstream ``const.ts:165`` + ``agent/api-client.ts:103``
    (verified literal):

      GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?
          corpid=<corpId>&corpsecret=<agent.corpSecret>

    Response shape: ``{access_token, expires_in, errcode, errmsg}``.

    Result classification:
      - ``valid``        — ``errcode==0`` AND ``access_token`` returned
      - ``invalid``      — reachable, ``errcode!=0`` (records errcode+errmsg)
      - ``unreachable``  — network error / timeout (never promote to invalid)
    """
    qs = urllib.parse.urlencode(
        {"corpid": corp_id, "corpsecret": corp_secret},
    )
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{qs}"
    base = "https://qyapi.weixin.qq.com"
    status, body, err = _get_json(url)
    if err:
        return ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg=err, domain=base,
        )
    if not isinstance(body, dict):
        return ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg=f"non-dict response status={status}", domain=base,
        )
    api_code = body.get("errcode")
    api_msg = body.get("errmsg")
    if status == 200 and api_code == 0 and body.get("access_token"):
        return ProbeResult(
            state="valid", api_code=0, domain=base,
            extra={"expire_s": body.get("expires_in")},
        )
    if status == 200 and isinstance(api_code, int) and api_code != 0:
        return ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=api_code, msg=str(api_msg)[:200],
            domain=base,
        )
    return ProbeResult(
        state="unreachable", code="PROBE_UNREACHABLE",
        msg=f"unexpected response status={status} errcode={api_code}",
        domain=base,
    )
