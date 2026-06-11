"""Tests for the channel collector + its variant rule tables.

Coverage:
  - detect.py: package + config-key fallback + ambiguous mark
  - probe.py: SecretRef resolution (file/json + file/singleValue + env +
              unsupported source=exec); JSON pointer edge cases
  - feishu_bundled.py:
      * L1 rules (CRED_MISSING, CONN_MODE_INCONSISTENT,
        DM_POLICY_OPEN_NO_WILDCARD, GATE_SENDER_NOT_IN_ALLOWLIST)
      * Log signature regexes (drop / WS / probe-timeout / webhook anomaly)
      * Probe path: bypassed when secret unresolved (no network call)
  - End-to-end collector: detection-zero → NO_CHANNEL_DETECTED;
    detection-positive → produces sections; --probe off in `all`.

All tests use synthetic fixtures + tempdirs so the suite has no
dependency on a real Feishu install. Probe tests stub the network
call entirely; we never reach out to open.feishu.cn from CI.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocdiag.channels import detect as channel_detect  # noqa: E402
from ocdiag.channels import probe as probe_mod  # noqa: E402
from ocdiag.channels.variants import (  # noqa: E402
    dingtalk as variant_dingtalk,
    feishu_bundled,
    feishu_lark,
    wecom as variant_wecom,
)
from ocdiag.core import registry  # noqa: E402
from ocdiag.core.context import DiagContext  # noqa: E402
from ocdiag.core.types import Verdict  # noqa: E402


# ─── log fixture helpers (path-aware) ────────────────────────────────


# Paths that look like a real plugin-emitted line (the runtime sink the
# openclaw logger forwards plugin emissions through). Any fixture using
# this path is treated by ``log_utils.iter_plugin_log_lines`` as a
# plugin-tree line and runs through the regex.
_PLUGIN_PATH_FEISHU_BUNDLED = (
    "file:///root/.openclaw/npm/projects/openclaw-feishu-x/"
    "node_modules/@openclaw/feishu/dist/monitor.account.js:1:1"
)
_PLUGIN_PATH_FEISHU_LARK = (
    "file:///root/.openclaw/npm/projects/openclaw-lark-x/"
    "node_modules/@larksuite/openclaw-lark/dist/index.js:1:1"
)
_PLUGIN_PATH_DINGTALK = (
    "file:///root/.openclaw/npm/projects/openclaw-dingtalk-x/"
    "node_modules/@dingtalk-real-ai/dingtalk-connector/dist/index.js:1:1"
)
_PLUGIN_PATH_WECOM = (
    "file:///root/.openclaw/npm/projects/openclaw-wecom-x/"
    "node_modules/@wecom/wecom-openclaw-plugin/dist/index.js:1:1"
)
# The gateway console relay sink: this is where the openclaw runtime
# captures assistant-stdout-style output (including, fatally, the
# diagnostic's own report text when piped back through the gateway).
# Lines on this path MUST be rejected before any signature runs —
# otherwise we self-pollinate every time a previous channel run's
# output mentions a literal signature.
_GATEWAY_CONSOLE_RELAY_PATH = (
    "file:///root/.local/share/pnpm/global/5/.pnpm/openclaw@2026.6.1/"
    "node_modules/openclaw/dist/console-DcpfMatG.js:153:46"
)


def _make_log_line(message: str, full_file_path: str, ts: str) -> str:
    """Build a single openclaw-logger JSON line for fixtures.

    Sets the ``message`` field directly so tests don't have to worry
    about whether the variant code reads "0"/"1"/"message" — they all
    converge on this. Path goes under ``_meta.path.fullFilePath`` to
    match the real openclaw logger's output shape.
    """
    return json.dumps({
        "time": ts,
        "_meta": {
            "logLevelName": "INFO",
            "path": {"fullFilePath": full_file_path},
        },
        "message": message,
    }, ensure_ascii=False)


# ─── detect.py ───────────────────────────────────────────────────────


def test_detect_no_packages_no_config(tmp_path):
    """Empty npm root + empty config → no variants detected.

    Mirrors the freshly-installed OpenClaw case where nobody set up a
    channel yet; the collector must return cleanly without inventing one.
    """
    npm_root = tmp_path / "npm" / "projects"
    npm_root.mkdir(parents=True)
    found = channel_detect.detect_variants(str(npm_root), {"channels": {}})
    assert found == []


def test_detect_npm_package_strongest_signal(tmp_path):
    """An installed @openclaw/feishu wins over any config-key ambiguity."""
    npm_root = tmp_path / "npm" / "projects"
    pkg_dir = npm_root / "openclaw-feishu-abc" / "node_modules" / "@openclaw" / "feishu"
    pkg_dir.mkdir(parents=True)
    found = channel_detect.detect_variants(
        str(npm_root),
        {"channels": {"feishu": {}}},  # would be ambiguous on its own
    )
    assert len(found) == 1
    assert found[0].variant == "feishu-bundled"
    assert found[0].detect_basis.startswith("pkg:")
    assert not found[0].ambiguous


def test_detect_config_fallback_marks_ambiguous(tmp_path):
    """config:channels.feishu alone yields BOTH bundled+lark candidates,
    each flagged ambiguous so the collector doesn't lock in the wrong one."""
    npm_root = tmp_path / "npm" / "projects"
    npm_root.mkdir(parents=True)
    found = channel_detect.detect_variants(
        str(npm_root), {"channels": {"feishu": {"appId": "x"}}},
    )
    variants = sorted(d.variant for d in found)
    assert variants == ["feishu-bundled", "feishu-lark"]
    assert all(d.ambiguous for d in found)


def test_detect_dingtalk_unambiguous_via_config(tmp_path):
    npm_root = tmp_path / "npm" / "projects"
    npm_root.mkdir(parents=True)
    found = channel_detect.detect_variants(
        str(npm_root), {"channels": {"dingtalk": {}}},
    )
    assert [d.variant for d in found] == ["dingtalk"]
    assert not found[0].ambiguous


# ─── probe.py: secret resolution ─────────────────────────────────────


def test_resolve_secret_literal_string():
    """A plain string SecretRef is treated as already-resolved literal."""
    res = probe_mod.resolve_secret_ref("hardcoded-value", cfg={})
    assert res.ok
    assert res.value == "hardcoded-value"
    assert res.ref_label == "literal"


def test_resolve_secret_uri_string_unsupported():
    res = probe_mod.resolve_secret_ref("env://API_KEY", cfg={})
    assert not res.ok
    assert res.code == "SECRET_UNRESOLVED"
    assert "URI-string" in res.msg


def test_resolve_secret_env_present(monkeypatch):
    monkeypatch.setenv("PROBE_TEST_SECRET", "env-value")
    res = probe_mod.resolve_secret_ref(
        {"source": "env", "provider": "default", "id": "PROBE_TEST_SECRET"},
        cfg={},
    )
    assert res.ok and res.value == "env-value"
    assert res.ref_label == "ref:env:default"


def test_resolve_secret_env_missing():
    res = probe_mod.resolve_secret_ref(
        {"source": "env", "provider": "default", "id": "ABSENT_VAR_XYZ"},
        cfg={},
    )
    assert not res.ok and res.code == "SECRET_UNRESOLVED"


def test_resolve_secret_file_json_pointer(tmp_path):
    """source=file mode=json → walk JSON pointer into the file content."""
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps({
        "lark": {"appSecret": "abc123"},
    }))
    cfg = {
        "secrets": {
            "providers": {
                "lark-secrets": {
                    "source": "file",
                    "path": str(secrets_path),
                },
            },
        },
    }
    res = probe_mod.resolve_secret_ref(
        {"source": "file", "provider": "lark-secrets",
         "id": "/lark/appSecret"},
        cfg,
    )
    assert res.ok and res.value == "abc123"


def test_resolve_secret_file_json_pointer_missing_key(tmp_path):
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps({"lark": {"appSecret": "abc"}}))
    cfg = {
        "secrets": {
            "providers": {
                "p": {"source": "file", "path": str(secrets_path)},
            },
        },
    }
    res = probe_mod.resolve_secret_ref(
        {"source": "file", "provider": "p", "id": "/missing/key"}, cfg,
    )
    assert not res.ok
    assert "JSON pointer lookup failed" in res.msg


def test_resolve_secret_file_single_value(tmp_path):
    """mode=singleValue → entire file (trimmed) is the secret."""
    secrets_path = tmp_path / "single.txt"
    secrets_path.write_text("  raw-secret-value  \n")
    cfg = {
        "secrets": {"providers": {
            "p": {
                "source": "file", "path": str(secrets_path),
                "mode": "singleValue",
            },
        }},
    }
    res = probe_mod.resolve_secret_ref(
        {"source": "file", "provider": "p", "id": "value"}, cfg,
    )
    assert res.ok and res.value == "raw-secret-value"


def test_resolve_secret_exec_unsupported_in_p0():
    res = probe_mod.resolve_secret_ref(
        {"source": "exec", "provider": "vault", "id": "foo"},
        cfg={"secrets": {"providers": {"vault": {"source": "exec"}}}},
    )
    assert not res.ok
    assert "source=exec" in res.msg


def test_json_pointer_tilde_escape(tmp_path):
    """~1 decodes to /, ~0 decodes to ~ — RFC 6901."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"a/b": {"c~d": "value"}}))
    cfg = {"secrets": {"providers": {"x": {"source": "file", "path": str(p)}}}}
    res = probe_mod.resolve_secret_ref(
        {"source": "file", "provider": "x", "id": "/a~1b/c~0d"}, cfg,
    )
    assert res.ok and res.value == "value"


# ─── feishu_bundled L1 rules ─────────────────────────────────────────


def _diagnose_with(channels_feishu: Dict[str, Any], **kwargs):
    """Helper: run feishu_bundled.diagnose against a synthetic config."""
    cfg = {"channels": {"feishu": channels_feishu}}
    return feishu_bundled.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", **kwargs,
    )


def test_l1_cred_missing():
    rep = _diagnose_with({"appId": ""})  # no creds at top, no accounts
    assert len(rep.accounts) == 1
    codes = [f.code for f in rep.accounts[0].findings]
    assert "CRED_MISSING" in codes


def test_l1_cred_present_no_findings():
    """Healthy config produces no L1 findings beyond the summary."""
    rep = _diagnose_with({
        "appId": "cli_xxx",
        "appSecret": "secret",
        "dmPolicy": "allowlist",
    })
    assert len(rep.accounts) == 1
    fail_codes = [
        f.code for f in rep.accounts[0].findings if f.verdict == "fail"
    ]
    assert fail_codes == []


def test_l1_conn_mode_webhook_missing_secrets():
    rep = _diagnose_with({
        "appId": "cli_xxx",
        "appSecret": "s",
        "connectionMode": "webhook",
        # missing verificationToken AND encryptKey
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "CONN_MODE_INCONSISTENT" in codes


def test_l1_dm_policy_open_no_wildcard():
    rep = _diagnose_with({
        "appId": "cli_xxx", "appSecret": "s",
        "dmPolicy": "open",
        "allowFrom": ["ou_specific"],   # missing "*"
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_POLICY_OPEN_NO_WILDCARD" in codes


def test_l1_dm_policy_open_with_wildcard_ok():
    rep = _diagnose_with({
        "appId": "cli_xxx", "appSecret": "s",
        "dmPolicy": "open",
        "allowFrom": ["*"],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_POLICY_OPEN_NO_WILDCARD" not in codes


def test_l1_gate_sender_not_in_allowlist():
    rep = _diagnose_with(
        {
            "appId": "cli_xxx", "appSecret": "s",
            "dmPolicy": "allowlist",
            "allowFrom": ["ou_someone_else"],
        },
        sender_open_id="ou_outsider",
    )
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in codes


def test_l1_no_sender_check_when_sender_absent():
    """Without --sender, GATE_SENDER_NOT_IN_ALLOWLIST must NOT fire."""
    rep = _diagnose_with({
        "appId": "cli_xxx", "appSecret": "s",
        "dmPolicy": "allowlist",
        "allowFrom": ["ou_someone"],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" not in codes


def test_l1_account_inheritance():
    """Account-level fields override top-level. Top-level appId
    should still flow into an account that didn't redeclare it."""
    rep = _diagnose_with({
        "appId": "cli_top",
        "appSecret": "s_top",
        "accounts": {
            "a1": {"dmPolicy": "open"},  # inherits creds, sets policy
            "a2": {"appId": "cli_a2", "appSecret": "s2"},
        },
    })
    by_id = {a.account_id: a for a in rep.accounts}
    # a1 inherits creds → no CRED_MISSING; but DM_POLICY_OPEN_NO_WILDCARD
    # because allowFrom isn't declared at all (empty).
    assert "CRED_MISSING" not in [
        f.code for f in by_id["a1"].findings
    ]
    assert "DM_POLICY_OPEN_NO_WILDCARD" in [
        f.code for f in by_id["a1"].findings
    ]


# ─── feishu_bundled L2/L3 log signatures ─────────────────────────────


# Real log lines verified against the host's @openclaw/feishu/dist
# (monitor.account-BvKcwxaW.js / monitor.state-r4OLFBfg.js — see
# feishu_bundled._LOG_SIGNATURES for the source-line citations). Each
# fixture carries a plugin-tree ``_meta.path.fullFilePath`` so it
# clears the self-pollution path filter; the regex sees only the
# message body, anchored at the start.
_FAKE_LOG_LINES = [
    # WS lifecycle line whose ``err`` carries the SDK terminal constant
    # ``WebSocket connect failed and autoReconnect is disabled`` →
    # promoted to ws_autoreconnect_disabled (fail).
    _make_log_line(
        "feishu[main]: WebSocket connection ended, recreating client "
        "in 5000ms: WebSocket connect failed and autoReconnect is "
        "disabled",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:34:56",
    ),
    # WS lifecycle line whose ``err`` carries ``WebSocket reconnect
    # exhausted after 6 attempts`` → promoted to ws_reconnect_exhausted.
    _make_log_line(
        "feishu[main]: WebSocket start failed, retrying in 30000ms: "
        "WebSocket reconnect exhausted after 6 attempts",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:34:58",
    ),
    # WS lifecycle line whose ``err`` is a plain transient error → stays
    # at the warn ws_start_failed_retrying / ws_connection_ended_recreating.
    _make_log_line(
        "feishu[clawdoctor]: WebSocket start failed, retrying in "
        "1000ms: socket hang up",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:34:59",
    ),
    _make_log_line(
        "feishu[main]: bot info probe timed out after 10000ms; "
        "continuing startup",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:35:00",
    ),
    _make_log_line(
        "feishu[clawdoctor]: blocked unauthorized sender ou_xyz "
        "(dmPolicy=allowlist)",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:36:00",
    ),
    _make_log_line(
        "feishu[main]: message in group oc_chat123 did not mention bot",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:37:00",
    ),
    _make_log_line(
        "feishu[clawdoctor]: webhook anomaly path=/feishu/events "
        "status=403 count=5",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:38:00",
    ),
    _make_log_line(
        "feishu: skipping duplicate message msg-abc",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:39:00",
    ),
    # bot-identity recovery: silent-drop family (verified literal —
    # see monitor.account-BvKcwxaW.js:3500 / 3506).
    _make_log_line(
        "feishu[main]: bot identity background retry exhausted; "
        "requireMention group messages may be skipped until restart",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:40:00",
    ),
    _make_log_line(
        "feishu[main]: requireMention group messages stay gated until "
        "bot identity recovery succeeds",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:40:05",
    ),
    # Comment pairing (info — monitor.account-BvKcwxaW.js:4476).
    _make_log_line(
        "feishu[main]: comment pairing request sender=ou_pair code=ABC123",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:41:00",
    ),
    # Decoy line that should NOT match anything.
    _make_log_line(
        "feishu[main]: WebSocket client started",
        _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:50:00",
    ),
]


def test_log_scan_matches_known_signatures(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_FAKE_LOG_LINES) + "\n")
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep.log_findings}
    assert "LOG_WS_AUTORECONNECT_DISABLED" in codes
    assert "LOG_WS_RECONNECT_EXHAUSTED" in codes
    assert "LOG_WS_START_FAILED_RETRYING" in codes
    assert "LOG_BOT_PROBE_TIMED_OUT" in codes
    assert "LOG_BLOCKED_UNAUTHORIZED_DM" in codes
    assert "LOG_GROUP_DID_NOT_MENTION_BOT" in codes
    assert "LOG_WEBHOOK_ANOMALY" in codes
    assert "LOG_SKIPPING_DUPLICATE_MESSAGE" in codes
    assert "LOG_BOT_IDENTITY_RETRY_EXHAUSTED" in codes
    assert "LOG_REQUIRE_MENTION_GATED" in codes
    assert "LOG_COMMENT_PAIRING_REQUEST" in codes


def test_log_scan_severity_classification(tmp_path):
    """SDK-promoted ws_* → fail, blocked_dm → warn,
    skipping_duplicate → ok (informational)."""
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_FAKE_LOG_LINES) + "\n")
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    by_code = {f.code: f.verdict for f in rep.log_findings}
    assert by_code["LOG_WS_AUTORECONNECT_DISABLED"] == "fail"
    assert by_code["LOG_WS_RECONNECT_EXHAUSTED"] == "fail"
    assert by_code["LOG_BOT_PROBE_TIMED_OUT"] == "fail"
    assert by_code["LOG_BOT_IDENTITY_RETRY_EXHAUSTED"] == "fail"
    assert by_code["LOG_BLOCKED_UNAUTHORIZED_DM"] == "warn"
    assert by_code["LOG_REQUIRE_MENTION_GATED"] == "warn"
    # The plain (non-SDK-terminal) WS retry line stays at warn — only
    # the err-substring-promoted variants are fail.
    assert by_code["LOG_WS_START_FAILED_RETRYING"] == "warn"
    assert by_code["LOG_SKIPPING_DUPLICATE_MESSAGE"] == "ok"
    assert by_code["LOG_COMMENT_PAIRING_REQUEST"] == "ok"


def test_ws_err_promotion_does_not_fire_on_plain_retry(tmp_path):
    """A WebSocket retry line WITHOUT either SDK terminal substring
    must stay at the (warn) base key, NOT promote to a fail key. This
    guards against regressing back to the bug where the SDK constants
    were wrongly anchored to fake "feishu[acct]:" prefixes — and
    proves we don't double-count."""
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text(
        _make_log_line(
            "feishu[main]: WebSocket start failed, retrying in 1000ms: "
            "ETIMEDOUT",
            _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T12:34:00",
        ) + "\n",
    )
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep.log_findings}
    assert "LOG_WS_START_FAILED_RETRYING" in codes
    assert "LOG_WS_RECONNECT_EXHAUSTED" not in codes
    assert "LOG_WS_AUTORECONNECT_DISABLED" not in codes


def test_bot_identity_retry_exhausted_signature(tmp_path):
    """The high-value silent-drop signal must be a fail-level finding —
    requireMention group messages get skipped after this until restart.
    Verified literal at monitor.account-BvKcwxaW.js:3500."""
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text(
        _make_log_line(
            "feishu[clawdoctor]: bot identity background retry "
            "exhausted; requireMention group messages may be skipped "
            "until restart",
            _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T13:00:00",
        ) + "\n",
    )
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    by_code = {f.code: f.verdict for f in rep.log_findings}
    assert by_code["LOG_BOT_IDENTITY_RETRY_EXHAUSTED"] == "fail"
    # The intermediate "background retry N/M failed" lines must NOT
    # match (only the final "exhausted" line is the silent-drop trigger).
    log.write_text(
        _make_log_line(
            "feishu[clawdoctor]: bot identity background retry 2/4 "
            "failed; next attempt in 30s",
            _PLUGIN_PATH_FEISHU_BUNDLED, "2026-06-10T13:00:00",
        ) + "\n",
    )
    rep2 = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep2.log_findings}
    assert "LOG_BOT_IDENTITY_RETRY_EXHAUSTED" not in codes


def test_log_scan_no_log_files_silent():
    """Empty log set → no log findings (don't fabricate "all clear" — the
    collector layer handles the explicit ok message)."""
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[], detect_basis="test",
    )
    assert rep.log_findings == []


# ─── probe path: skip-when-unresolved guard ──────────────────────────


def test_probe_skips_when_secret_unresolved(monkeypatch):
    """If appSecret is a SecretRef we can't resolve, the probe MUST NOT
    call out to the network — the finding records SECRET_UNRESOLVED.

    We assert this by replacing ``feishu_token_probe`` with a sentinel
    that flips a flag; flag must remain False after the run.
    """
    called = {"network": False}

    def fake_probe(*args, **kwargs):
        called["network"] = True
        return probe_mod.ProbeResult(state="valid")

    monkeypatch.setattr(probe_mod, "feishu_token_probe", fake_probe)

    cfg = {
        "channels": {"feishu": {
            "appId": "cli_xxx",
            # ref points to a provider that doesn't exist
            "appSecret": {
                "source": "file", "provider": "missing-provider",
                "id": "/foo",
            },
        }},
    }
    rep = feishu_bundled.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", do_probe=True,
    )
    assert called["network"] is False
    codes = [f.code for f in rep.probe_findings]
    assert "SECRET_UNRESOLVED" in codes


def test_probe_classifies_invalid_response(monkeypatch):
    """When Feishu returns code != 0, surface as invalid (not unreachable)."""
    monkeypatch.setattr(
        probe_mod, "feishu_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=10003, msg="invalid app_id",
            domain="https://open.feishu.cn",
        ),
    )
    cfg = {"channels": {"feishu": {
        "appId": "cli_xxx", "appSecret": "literal-secret",
    }}}
    rep = feishu_bundled.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", do_probe=True,
    )
    fs = rep.probe_findings
    assert len(fs) == 1
    assert fs[0].verdict == "fail" and fs[0].code == "PROBE_INVALID"


def test_probe_classifies_unreachable(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "feishu_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg="timeout", domain="https://open.feishu.cn",
        ),
    )
    cfg = {"channels": {"feishu": {
        "appId": "cli_xxx", "appSecret": "literal-secret",
    }}}
    rep = feishu_bundled.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", do_probe=True,
    )
    fs = rep.probe_findings
    assert len(fs) == 1
    # Unreachable must NEVER turn into a fail/credential verdict — that
    # would false-positive a network outage as "wrong credentials".
    assert fs[0].verdict == "warn" and fs[0].code == "PROBE_UNREACHABLE"


# ─── End-to-end via the registered collector ─────────────────────────


def _make_temp_ctx(tmp_path: Path, channels_feishu: Dict[str, Any] = None,
                   install_pkg: bool = False) -> DiagContext:
    """Build a hermetic DiagContext under tmp_path."""
    home = tmp_path
    (home / "agents").mkdir(exist_ok=True, parents=True)
    cfg_path = home / "openclaw.json"
    cfg = {
        "gateway": {"port": 18789},
        "agents": {"defaults": {}, "list": []},
        "channels": (
            {"feishu": channels_feishu} if channels_feishu else {}
        ),
    }
    cfg_path.write_text(json.dumps(cfg))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    if install_pkg:
        # Fake the package install so detect.py recognizes it.
        pkg_dir = (
            home / "npm" / "projects" / "openclaw-feishu-test"
            / "node_modules" / "@openclaw" / "feishu"
        )
        pkg_dir.mkdir(parents=True)
    return DiagContext(
        openclaw_home=home,
        config_path=cfg_path,
        log_dir=log_dir,
        sessions_base=home / "agents",
    )


def test_collector_no_channel_detected(tmp_path):
    """Empty install + empty config → NO_CHANNEL_DETECTED ok finding."""
    registry.discover()
    ctx = _make_temp_ctx(tmp_path)
    coll = registry.get("channel")
    assert coll is not None
    report = coll.collect(ctx)
    assert report.module_id == "channel"
    assert report.verdict == Verdict.OK
    codes = [c.name for s in report.sections for c in s.checks]
    assert "NO_CHANNEL_DETECTED" in codes


def test_collector_runs_feishu_bundled_on_detection(tmp_path):
    """Detection-positive end-to-end: package present + feishu config
    present → channel report contains a feishu-bundled L1 section."""
    registry.discover()
    ctx = _make_temp_ctx(
        tmp_path,
        channels_feishu={"appId": "cli_xxx", "appSecret": "literal"},
        install_pkg=True,
    )
    coll = registry.get("channel")
    report = coll.collect(ctx)
    titles = [s.title for s in report.sections]
    assert any("feishu-bundled · L1" in t for t in titles)
    # Probe section must NOT be present (default ctx.probe is False).
    assert not any("L5" in t for t in titles)


def test_collector_probe_section_only_with_probe_flag(tmp_path, monkeypatch):
    """ctx.probe=True → an L5 section appears; ctx.probe=False → it doesn't.

    Stub the network call so the test doesn't hit Feishu.
    """
    registry.discover()
    monkeypatch.setattr(
        probe_mod, "feishu_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="valid", api_code=0, domain="https://open.feishu.cn",
        ),
    )
    ctx = _make_temp_ctx(
        tmp_path,
        channels_feishu={"appId": "cli_xxx", "appSecret": "literal"},
        install_pkg=True,
    )
    ctx.probe = True
    coll = registry.get("channel")
    report = coll.collect(ctx)
    titles = [s.title for s in report.sections]
    assert any("feishu-bundled · L5" in t for t in titles)


def test_collector_sender_flag_via_ctx_triggers_gate(tmp_path):
    """``ctx.sender_open_id`` (the path the CLI's ``--sender`` flag uses)
    must reach the variant's L1 rules and produce
    GATE_SENDER_NOT_IN_ALLOWLIST when the sender isn't on allowFrom.

    This is the end-to-end seam main.py builds via ``--sender ou_xxx``.
    Without this plumbing the flag would silently no-op in CLI runs.
    """
    registry.discover()
    ctx = _make_temp_ctx(
        tmp_path,
        channels_feishu={
            "appId": "cli_xxx", "appSecret": "literal",
            "dmPolicy": "allowlist",
            "allowFrom": ["ou_someone_else"],
        },
        install_pkg=True,
    )
    ctx.sender_open_id = "ou_outsider"
    coll = registry.get("channel")
    report = coll.collect(ctx)
    codes = [c.name for s in report.sections for c in s.checks]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in codes


def test_collector_sender_flag_via_ctx_in_allowlist_no_finding(tmp_path):
    """When the sender IS on allowFrom, no GATE_SENDER finding fires —
    the gate is silent on the happy path (it's a hint, not noise)."""
    registry.discover()
    ctx = _make_temp_ctx(
        tmp_path,
        channels_feishu={
            "appId": "cli_xxx", "appSecret": "literal",
            "dmPolicy": "allowlist",
            "allowFrom": ["ou_insider"],
        },
        install_pkg=True,
    )
    ctx.sender_open_id = "ou_insider"
    coll = registry.get("channel")
    report = coll.collect(ctx)
    codes = [c.name for s in report.sections for c in s.checks]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" not in codes


def test_main_build_context_populates_sender_open_id():
    """``main._build_context(args)`` must lift ``args.sender`` (the
    --sender CLI flag) into ``ctx.sender_open_id`` so collectors can
    reach it without a kwargs hack."""
    from argparse import Namespace
    from ocdiag.main import _build_context

    args = Namespace(
        config="/tmp/cfg.json", log_dir="/tmp/logs",
        sessions_base="/tmp/sessions", openclaw_home="/tmp/home",
        format=None, json=False, no_color=False, unmask=False,
        probe=False, sender="ou_test_sender",
    )
    ctx = _build_context(args)
    assert ctx.sender_open_id == "ou_test_sender"

    args.sender = None
    ctx = _build_context(args)
    assert ctx.sender_open_id is None


def test_collector_does_not_leak_secret_in_envelope(tmp_path, monkeypatch):
    """The plaintext secret must NEVER appear in any rendered field —
    not Section message, not detail, not data — when probe runs.

    Use a recognisable plaintext sentinel and check that it's absent
    from the JSON envelope dump.
    """
    registry.discover()

    SENTINEL = "PLAINTEXT-SECRET-SHOULD-NOT-LEAK-9f3a"

    # Stub the probe so we control the exact response, but the
    # collector still has to call resolve_secret_ref on SENTINEL.
    seen_secret = {"v": None}

    def fake_probe(app_id, app_secret, domain="feishu"):
        seen_secret["v"] = app_secret
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://open.feishu.cn",
        )

    monkeypatch.setattr(probe_mod, "feishu_token_probe", fake_probe)

    ctx = _make_temp_ctx(
        tmp_path,
        channels_feishu={"appId": "cli_xxx", "appSecret": SENTINEL},
        install_pkg=True,
    )
    ctx.probe = True
    coll = registry.get("channel")
    report = coll.collect(ctx)

    # Sanity: the secret WAS passed to the probe (we want this — else the
    # test isn't actually exercising the leak surface).
    assert seen_secret["v"] == SENTINEL

    # Now serialize the full envelope and assert the sentinel is
    # absent from EVERYTHING the user would see.
    from ocdiag.render.json_renderer import to_envelope
    envelope = to_envelope(report)
    blob = json.dumps(envelope, ensure_ascii=False)
    assert SENTINEL not in blob, "plaintext secret leaked into envelope"


# ─── feishu_lark variant ─────────────────────────────────────────────
#
# The lark plugin is not installed on this host; every lark test runs
# off synthetic fixtures + TS source citations. ``probe.py`` already
# routes ``domain="lark"`` to open.larksuite.com (verified in P0); these
# tests stub that call.


def _diagnose_lark_with(channels_feishu: Dict[str, Any], **kwargs):
    cfg = {"channels": {"feishu": channels_feishu}}
    return feishu_lark.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", **kwargs,
    )


def test_lark_l1_cred_missing():
    rep = _diagnose_lark_with({"appId": ""})
    assert len(rep.accounts) == 1
    codes = [f.code for f in rep.accounts[0].findings]
    assert "CRED_MISSING" in codes


def test_lark_l1_webhook_mode_not_implemented():
    """Lark-specific: connectionMode=webhook is a silent NOOP at runtime.
    Surface as fail so the operator knows events will never arrive
    (verified at channel/monitor.ts:58-61)."""
    rep = _diagnose_lark_with({
        "appId": "cli_xx", "appSecret": "s",
        "connectionMode": "webhook",
        "verificationToken": "v", "encryptKey": "k", "webhookPort": 3001,
    })
    findings = rep.accounts[0].findings
    by_code = {f.code: f for f in findings}
    assert "WEBHOOK_NOT_IMPLEMENTED" in by_code
    assert by_code["WEBHOOK_NOT_IMPLEMENTED"].verdict == "fail"
    # Lark variant must NOT also emit CONN_MODE_INCONSISTENT — the
    # webhook fields are meaningless in lark, since the path is dead.
    assert "CONN_MODE_INCONSISTENT" not in by_code


def test_lark_l1_dm_policy_open_no_wildcard():
    rep = _diagnose_lark_with({
        "appId": "cli_xx", "appSecret": "s",
        "dmPolicy": "open",
        "allowFrom": ["ou_specific"],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_POLICY_OPEN_NO_WILDCARD" in codes


def test_lark_l1_legacy_groupallowfrom_chatid():
    """gate.ts:195-205 — runtime warns when groupAllowFrom carries
    chat_id (oc_xxx) entries. Surface as warn so the operator knows
    to migrate to the per-group ``groups`` config."""
    rep = _diagnose_lark_with({
        "appId": "cli_xx", "appSecret": "s",
        "groupAllowFrom": ["ou_alice", "oc_legacy_chat_123"],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GROUP_ALLOWFROM_LEGACY_CHATID" in codes


def test_lark_l1_no_legacy_warn_when_clean():
    """All ou_xxx entries → no legacy warning."""
    rep = _diagnose_lark_with({
        "appId": "cli_xx", "appSecret": "s",
        "groupAllowFrom": ["ou_alice", "ou_bob"],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GROUP_ALLOWFROM_LEGACY_CHATID" not in codes


def test_lark_l1_domain_mismatch_when_explicit_feishu():
    """An explicit ``domain: "feishu"`` on a lark-detected variant is
    almost always a misconfig — the probe will hit feishu.cn instead
    of larksuite.com."""
    rep = _diagnose_lark_with({
        "appId": "cli_xx", "appSecret": "s",
        "domain": "feishu",
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DOMAIN_MISMATCH" in codes


def test_lark_l1_no_domain_mismatch_default():
    """Unset domain (defaults to "lark") doesn't trigger the warn."""
    rep = _diagnose_lark_with({"appId": "cli_xx", "appSecret": "s"})
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DOMAIN_MISMATCH" not in codes


def test_lark_l1_gate_sender_not_in_allowlist():
    rep = _diagnose_lark_with(
        {
            "appId": "cli_xx", "appSecret": "s",
            "dmPolicy": "allowlist",
            "allowFrom": ["ou_someone_else"],
        },
        sender_open_id="ou_outsider",
    )
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in codes


# Real lark log lines verified against
# repos/channel-src/openclaw-lark/src/* — see citations beside each.
# Each fixture carries a plugin-tree path so it survives the self-
# pollution path filter; signature regexes anchor on the message body.
_LARK_FAKE_LOG_LINES = [
    # monitor.ts:59 — webhook NOOP
    _make_log_line(
        "feishu[main]: webhook mode not implemented in monitor",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:00:00",
    ),
    # monitor.ts:73
    _make_log_line(
        "feishu[main]: starting WebSocket connection...",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:00:01",
    ),
    # gate.ts:225
    _make_log_line(
        "feishu[main]: group oc_chat1 blocked by group-level policy",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:01:00",
    ),
    # gate.ts:239
    _make_log_line(
        "feishu[main]: group oc_chat2 disabled by per-group config",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:02:00",
    ),
    # gate.ts:369
    _make_log_line(
        "feishu[main]: sender ou_alice not allowed in group oc_chat3",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:03:00",
    ),
    # gate.ts:401
    _make_log_line(
        "feishu[main]: message in group oc_chat4 did not mention bot, "
        "recording to history",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:04:00",
    ),
    # gate.ts:284 (allowBots=false)
    _make_log_line(
        "feishu[main]: drop bot sender ou_bot1 in oc_chat5 "
        "(allowBots=false)",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:05:00",
    ),
    # gate.ts:293 (allowBots=mentions)
    _make_log_line(
        "feishu[main]: drop bot sender ou_bot2 in oc_chat6 "
        "(allowBots=mentions, not mentioned)",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:06:00",
    ),
    # gate.ts:435
    _make_log_line(
        "feishu[main]: DM disabled by policy, rejecting sender ou_bob",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:07:00",
    ),
    # gate.ts:453
    _make_log_line(
        "feishu[main]: sender ou_charlie not in DM allowlist",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:08:00",
    ),
    # gate.ts:474
    _make_log_line(
        "feishu[main]: sender ou_dave not paired, creating pairing "
        "request",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:09:00",
    ),
    # event-handlers.ts:102
    _make_log_line(
        "feishu[main]: duplicate message om_xxx, skipping",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:10:00",
    ),
    # event-handlers.ts:108
    _make_log_line(
        "feishu[main]: message om_yyy expired, discarding",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:11:00",
    ),
    # handler.ts:106
    _make_log_line(
        "feishu[main]: empty message om_zzz (no text, no media), "
        "skipping",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:12:00",
    ),
    # event-handlers.ts:89
    _make_log_line(
        "feishu[main]: drop self-echo message om_aaa",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:13:00",
    ),
    # vc-meeting-invited-handler.ts:156
    _make_log_line(
        "feishu[main]: vc invited event rejected (dmPolicy=disabled)",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:14:00",
    ),
    # vc-meeting-invited-handler.ts:192
    _make_log_line(
        "feishu[main]: vc invited event rejected "
        "(dmPolicy=allowlist, inviter not in allowlist)",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:15:00",
    ),
    # decoy — must not match
    _make_log_line(
        "feishu[main]: bot open_id resolved: ou_some_bot",
        _PLUGIN_PATH_FEISHU_LARK, "2026-06-10T12:16:00",
    ),
]


def test_lark_log_signatures_verified_against_ts_source(tmp_path):
    """Every fixture line is a literal log emission from the lark TS
    source; each must produce a finding."""
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_LARK_FAKE_LOG_LINES) + "\n")
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep.log_findings}
    expected = {
        "LOG_WEBHOOK_MODE_NOT_IMPLEMENTED",
        "LOG_WS_STARTING",
        "LOG_GROUP_BLOCKED_BY_POLICY",
        "LOG_GROUP_DISABLED",
        "LOG_SENDER_NOT_ALLOWED_IN_GROUP",
        "LOG_GROUP_DID_NOT_MENTION_BOT",
        "LOG_BOT_SENDER_DISABLED",
        "LOG_BOT_SENDER_NOT_MENTIONED",
        "LOG_DM_DISABLED_BY_POLICY",
        "LOG_DM_SENDER_NOT_IN_ALLOWLIST",
        "LOG_DM_PAIRING_REQUEST_CREATED",
        "LOG_SKIPPING_DUPLICATE_MESSAGE",
        "LOG_MESSAGE_EXPIRED",
        "LOG_SKIPPING_EMPTY_MESSAGE",
        "LOG_SKIPPING_SELF_ECHO",
        "LOG_VC_DM_DISABLED",
        "LOG_VC_INVITER_NOT_IN_ALLOWLIST",
    }
    missing = expected - codes
    assert not missing, f"signatures missed: {missing}"


def test_lark_log_severity_classification(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_LARK_FAKE_LOG_LINES) + "\n")
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    by_code = {f.code: f.verdict for f in rep.log_findings}
    # Silent-NOOP is the worst — webhook config that never delivers
    # any event gets fail.
    assert by_code["LOG_WEBHOOK_MODE_NOT_IMPLEMENTED"] == "fail"
    # Gate decisions — warn (the operator usually wants to see these).
    assert by_code["LOG_DM_DISABLED_BY_POLICY"] == "warn"
    assert by_code["LOG_SENDER_NOT_ALLOWED_IN_GROUP"] == "warn"
    # Pairing request creation is normal user-onboarding flow — info.
    assert by_code["LOG_DM_PAIRING_REQUEST_CREATED"] == "ok"
    # Dedup / empty-message / self-echo — informational drop reasons.
    assert by_code["LOG_SKIPPING_DUPLICATE_MESSAGE"] == "ok"
    assert by_code["LOG_SKIPPING_EMPTY_MESSAGE"] == "ok"


def test_lark_probe_uses_lark_domain(monkeypatch):
    """Probe path must hand ``domain="lark"`` to feishu_token_probe so
    the request goes to open.larksuite.com — even when the lark account
    does not declare ``domain`` explicitly."""
    captured = {}

    def fake_probe(*, app_id, app_secret, domain="feishu"):
        captured["domain"] = domain
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://open.larksuite.com",
        )

    monkeypatch.setattr(probe_mod, "feishu_token_probe", fake_probe)
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {
            "appId": "cli_xx", "appSecret": "literal",
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert captured["domain"] == "lark"
    assert len(rep.probe_findings) == 1
    assert rep.probe_findings[0].verdict == "ok"
    assert rep.probe_findings[0].code == "PROBE_VALID"


def test_lark_probe_honors_explicit_https_domain(monkeypatch):
    """A self-deployed lark domain (https://...) is passed through
    verbatim."""
    captured = {}

    def fake_probe(*, app_id, app_secret, domain="feishu"):
        captured["domain"] = domain
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain=domain,
        )

    monkeypatch.setattr(probe_mod, "feishu_token_probe", fake_probe)
    feishu_lark.diagnose(
        cfg={"channels": {"feishu": {
            "appId": "cli_xx", "appSecret": "literal",
            "domain": "https://lark.example.com",
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert captured["domain"] == "https://lark.example.com"


def test_lark_probe_skipped_secret_unresolved(monkeypatch):
    """Same skip-when-unresolved guard as bundled — no network call
    when the secret can't be resolved."""
    called = {"net": False}

    def fake_probe(**kw):
        called["net"] = True
        return probe_mod.ProbeResult(state="valid")

    monkeypatch.setattr(probe_mod, "feishu_token_probe", fake_probe)
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {
            "appId": "cli_xx",
            "appSecret": {
                "source": "file", "provider": "missing-prov", "id": "/x",
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert called["net"] is False
    codes = [f.code for f in rep.probe_findings]
    assert "SECRET_UNRESOLVED" in codes


def test_lark_collector_runs_via_config_when_no_package(tmp_path):
    """The lark variant has no host install. With config-only
    detection (channels.feishu present, no package), the collector
    should still produce both feishu-bundled AND feishu-lark sections —
    each ambiguous — and the lark variant must finish its diagnosis
    without crashing."""
    registry.discover()
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18790},
        "agents": {"defaults": {}, "list": []},
        "channels": {"feishu": {
            "appId": "cli_xx", "appSecret": "literal",
            "domain": "lark",
        }},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home,
        config_path=cfg_path,
        log_dir=log_dir,
        sessions_base=home / "agents",
    )
    coll = registry.get("channel")
    report = coll.collect(ctx)
    titles = [s.title for s in report.sections]
    assert any("feishu-bundled · L1" in t for t in titles)
    assert any("feishu-lark · L1" in t for t in titles)


def test_lark_account_inheritance():
    """Account-level overrides merge over top-level — same shape as
    bundled. Verifies the merge function doesn't drop top-level creds
    when a sub-account doesn't redeclare them."""
    rep = _diagnose_lark_with({
        "appId": "cli_top",
        "appSecret": "s_top",
        "accounts": {
            "primary": {"dmPolicy": "open", "allowFrom": ["*"]},
            "secondary": {"appId": "cli_sec", "appSecret": "s_sec"},
        },
    })
    by_id = {a.account_id: a for a in rep.accounts}
    assert "primary" in by_id and "secondary" in by_id
    # primary inherits creds → no CRED_MISSING
    assert "CRED_MISSING" not in [
        f.code for f in by_id["primary"].findings
    ]
    # primary has allowFrom=["*"] so DM_POLICY_OPEN_NO_WILDCARD must NOT fire
    assert "DM_POLICY_OPEN_NO_WILDCARD" not in [
        f.code for f in by_id["primary"].findings
    ]


def test_lark_no_log_files_silent():
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[], detect_basis="test",
    )
    assert rep.log_findings == []


# ─── dingtalk variant ────────────────────────────────────────────────
#
# The dingtalk plugin is not installed on this host. Every assertion
# below is anchored on a literal string from
# repos/channel-src/dingtalk-openclaw-connector/src/. A regression in
# upstream's log strings will fail these tests.


def _diagnose_dingtalk_with(channels_dingtalk, key="dingtalk-connector",
                            **kwargs):
    cfg = {"channels": {key: channels_dingtalk}}
    return variant_dingtalk.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", **kwargs,
    )


def test_dingtalk_l1_cred_missing():
    rep = _diagnose_dingtalk_with({})
    assert len(rep.accounts) == 1
    codes = [f.code for f in rep.accounts[0].findings]
    assert "CRED_MISSING" in codes


def test_dingtalk_l1_accepts_legacy_short_key():
    """``channels.dingtalk`` (short form) AND
    ``channels.dingtalk-connector`` (canonical) both reach diagnose."""
    rep_short = _diagnose_dingtalk_with(
        {"clientId": "ding_x", "clientSecret": "s"}, key="dingtalk",
    )
    rep_canonical = _diagnose_dingtalk_with(
        {"clientId": "ding_x", "clientSecret": "s"},
        key="dingtalk-connector",
    )
    assert "CRED_MISSING" not in [
        f.code for f in rep_short.accounts[0].findings
    ]
    assert "CRED_MISSING" not in [
        f.code for f in rep_canonical.accounts[0].findings
    ]


def test_dingtalk_l1_clientid_numeric_is_present():
    """Schema permits numeric clientId; treat any non-empty number as
    present so a numeric clientId doesn't false-positive CRED_MISSING."""
    rep = _diagnose_dingtalk_with({
        "clientId": 123456789, "clientSecret": "s",
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "CRED_MISSING" not in codes


def test_dingtalk_l1_dm_policy_pairing_warns():
    """dmPolicy=pairing degrades to open at runtime — surface as warn.
    Source: core/message-handler.ts:1004."""
    rep = _diagnose_dingtalk_with({
        "clientId": "ding_x", "clientSecret": "s", "dmPolicy": "pairing",
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_POLICY_PAIRING_UNSUPPORTED" in codes
    by_code = {f.code: f for f in rep.accounts[0].findings}
    assert by_code["DM_POLICY_PAIRING_UNSUPPORTED"].verdict == "warn"


def test_dingtalk_l1_dm_allowlist_empty_warns():
    rep = _diagnose_dingtalk_with({
        "clientId": "ding_x", "clientSecret": "s",
        "dmPolicy": "allowlist", "allowFrom": [],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_ALLOWLIST_EMPTY" in codes


def test_dingtalk_l1_group_allowlist_empty_warns():
    rep = _diagnose_dingtalk_with({
        "clientId": "ding_x", "clientSecret": "s",
        "groupPolicy": "allowlist", "groupAllowFrom": [],
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GROUP_ALLOWLIST_EMPTY" in codes


def test_dingtalk_l1_gate_sender_not_in_allowlist():
    rep = _diagnose_dingtalk_with(
        {
            "clientId": "ding_x", "clientSecret": "s",
            "dmPolicy": "allowlist",
            "allowFrom": ["someone_else"],
        },
        sender_open_id="outsider",
    )
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in codes


def test_dingtalk_l1_account_inheritance():
    rep = _diagnose_dingtalk_with({
        "clientId": "ding_top", "clientSecret": "s_top",
        "accounts": {
            "a1": {"dmPolicy": "pairing"},  # inherits creds
            "a2": {"clientId": "ding_a2", "clientSecret": "s2"},
        },
    })
    by_id = {a.account_id: a for a in rep.accounts}
    assert "CRED_MISSING" not in [f.code for f in by_id["a1"].findings]
    assert "DM_POLICY_PAIRING_UNSUPPORTED" in [
        f.code for f in by_id["a1"].findings
    ]


# Real dingtalk log lines — every literal grep-verified in source. The
# upstream logger (createLogger with prefix=DingTalk:<accountId>) wraps
# bare messages as ``[DingTalk:<acct>] <msg>``; gate path is in
# core/message-handler.ts and connection in core/connection.ts.
_DINGTALK_FAKE_LOG_LINES = [
    # message-handler.ts:1021 (literal `[DingTalk]` inside the msg
    # PLUS the wrapper prefix the logger adds)
    _make_log_line(
        "[DingTalk:main] [DingTalk] DM 被拦截: allowFrom 白名单为空，"
        "拒绝所有请求",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:00:00",
    ),
    # message-handler.ts:1038
    _make_log_line(
        "[DingTalk:main] DM 被拦截: senderId=ding_user1 (Alice) 不在白名单中",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:01:00",
    ),
    # message-handler.ts:1011
    _make_log_line(
        "[DingTalk:main] DM 被拦截: senderId 为空",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:02:00",
    ),
    # message-handler.ts:1108
    _make_log_line(
        "[DingTalk:main] 群聊被拦截: conversationId=convX 不在 "
        "groupAllowFrom 白名单中",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:03:00",
    ),
    # message-handler.ts:1091
    _make_log_line(
        "[DingTalk:main] 群聊被拦截: groupAllowFrom 白名单为空，拒绝所有请求",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:04:00",
    ),
    # message-handler.ts:1063
    _make_log_line(
        "[DingTalk:main] 群聊被拦截: groupPolicy=disabled",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:05:00",
    ),
    # message-handler.ts:1004 — quoted string inside the message body.
    # Match the openclaw logger's JSON-encoding form (quotes become
    # \") because that is exactly the text variant regexes expect.
    _make_log_line(
        '[DingTalk:main] dmPolicy=\\"pairing\\" 暂不支持，将按 '
        '\\"open\\" 策略处理',
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:06:00",
    ),
    # channel.ts:444 — bare prefix from gateway-supplied logger
    _make_log_line(
        "dingtalk-connector[acct1] is disabled, skipping startup",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:07:00",
    ),
    # connection.ts:324
    _make_log_line(
        "[DingTalk:main] 连接建立失败: socket hang up",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:08:00",
    ),
    # connection.ts:730
    _make_log_line(
        "[DingTalk:main] 连接失败，错误详情：",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:09:00",
    ),
    # connection.ts:348
    _make_log_line(
        "[DingTalk:main] 重连失败：ETIMEDOUT (尝试 3)",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:10:00",
    ),
    # connection.ts:376 — bracketed-acct form
    _make_log_line(
        "[DingTalk:main] [acct2] 重连失败：ECONNREFUSED",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:11:00",
    ),
    # connection.ts:344
    _make_log_line(
        "[DingTalk:main] ✅ 重连成功 (socket 状态=1)",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:12:00",
    ),
    # connection.ts:372
    _make_log_line(
        "[DingTalk:main] 收到服务端 disconnect topic，即将重连",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:13:00",
    ),
    # connection.ts:783
    _make_log_line(
        "[DingTalk:main] SDK reconnecting...",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:14:00",
    ),
    # connection.ts:787
    _make_log_line(
        "[DingTalk:main] ✅ SDK reconnected successfully",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:15:00",
    ),
    # channel.ts:98
    _make_log_line(
        "[DingTalk:Pairing] Pairing approved for user: ding_user2",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:16:00",
    ),
    # decoy — must not match
    _make_log_line(
        "[DingTalk:main] ✅ 消息处理完成 (1/1)",
        _PLUGIN_PATH_DINGTALK, "2026-06-10T12:17:00",
    ),
]


def test_dingtalk_log_signatures_against_ts_source(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_DINGTALK_FAKE_LOG_LINES) + "\n")
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "x", "clientSecret": "y",
        }}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep.log_findings}
    expected = {
        "LOG_DM_ALLOWLIST_EMPTY",
        "LOG_DM_SENDER_NOT_IN_ALLOWLIST",
        "LOG_DM_SENDER_EMPTY",
        "LOG_GROUP_NOT_IN_ALLOWLIST",
        "LOG_GROUP_ALLOWLIST_EMPTY",
        "LOG_GROUP_DISABLED",
        "LOG_DM_POLICY_PAIRING_UNSUPPORTED",
        "LOG_ACCOUNT_DISABLED_SKIPPING_STARTUP",
        "LOG_CONNECT_FAILED",
        "LOG_CONNECT_ERROR_DETAIL",
        "LOG_RECONNECT_FAILED",
        "LOG_RECONNECT_SUCCEEDED",
        "LOG_DISCONNECT_TOPIC",
        "LOG_SDK_RECONNECTING",
        "LOG_SDK_RECONNECTED",
        "LOG_PAIRING_APPROVED",
    }
    missing = expected - codes
    assert not missing, f"signatures missed: {missing}"


def test_dingtalk_log_severity_classification(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_DINGTALK_FAKE_LOG_LINES) + "\n")
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "x", "clientSecret": "y",
        }}},
        log_files=[str(log)],
        detect_basis="test",
    )
    by_code = {f.code: f.verdict for f in rep.log_findings}
    assert by_code["LOG_CONNECT_FAILED"] == "fail"
    assert by_code["LOG_CONNECT_ERROR_DETAIL"] == "fail"
    assert by_code["LOG_DM_ALLOWLIST_EMPTY"] == "warn"
    assert by_code["LOG_DM_POLICY_PAIRING_UNSUPPORTED"] == "warn"
    assert by_code["LOG_GROUP_DISABLED"] == "warn"
    assert by_code["LOG_RECONNECT_FAILED"] == "warn"
    # Lifecycle/info — ok.
    assert by_code["LOG_DISCONNECT_TOPIC"] == "ok"
    assert by_code["LOG_RECONNECT_SUCCEEDED"] == "ok"
    assert by_code["LOG_SDK_RECONNECTED"] == "ok"
    assert by_code["LOG_PAIRING_APPROVED"] == "ok"


def test_dingtalk_log_no_log_files_silent():
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "x", "clientSecret": "y",
        }}},
        log_files=[], detect_basis="test",
    )
    assert rep.log_findings == []


def test_dingtalk_probe_skips_when_secret_unresolved(monkeypatch):
    called = {"net": False}

    def fake_probe(*a, **kw):
        called["net"] = True
        return probe_mod.ProbeResult(state="valid")

    monkeypatch.setattr(probe_mod, "dingtalk_token_probe", fake_probe)
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "ding_x",
            "clientSecret": {
                "source": "file", "provider": "missing-prov", "id": "/x",
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert called["net"] is False
    codes = [f.code for f in rep.probe_findings]
    assert "SECRET_UNRESOLVED" in codes


def test_dingtalk_probe_classifies_invalid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "dingtalk_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=400, msg="InvalidAuthentication",
            domain="https://api.dingtalk.com",
        ),
    )
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "ding_x", "clientSecret": "literal",
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    fs = rep.probe_findings
    assert len(fs) == 1
    assert fs[0].verdict == "fail" and fs[0].code == "PROBE_INVALID"


def test_dingtalk_probe_classifies_unreachable(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "dingtalk_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg="timeout", domain="https://api.dingtalk.com",
        ),
    )
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "ding_x", "clientSecret": "literal",
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    fs = rep.probe_findings
    assert len(fs) == 1
    # NEVER promote unreachable → fail (would false-positive a network
    # outage as bad creds).
    assert fs[0].verdict == "warn" and fs[0].code == "PROBE_UNREACHABLE"


def test_dingtalk_probe_valid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "dingtalk_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://api.dingtalk.com",
            extra={"expire_s": 7200},
        ),
    )
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "ding_x", "clientSecret": "literal",
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert rep.probe_findings[0].verdict == "ok"
    assert rep.probe_findings[0].code == "PROBE_VALID"


def test_dingtalk_collector_via_config_when_no_package(tmp_path):
    """End-to-end collector: channels.dingtalk-connector present
    (canonical key) → dingtalk variant runs without crashing, even
    though no @dingtalk-real-ai package is installed on this host."""
    registry.discover()
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18791},
        "agents": {"defaults": {}, "list": []},
        "channels": {"dingtalk-connector": {
            "clientId": "ding_x", "clientSecret": "literal",
        }},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home,
        config_path=cfg_path,
        log_dir=log_dir,
        sessions_base=home / "agents",
    )
    coll = registry.get("channel")
    report = coll.collect(ctx)
    titles = [s.title for s in report.sections]
    assert any("dingtalk · L1" in t for t in titles)


def test_dingtalk_does_not_leak_secret(tmp_path, monkeypatch):
    registry.discover()
    SENTINEL = "DING-SENTINEL-SHOULD-NOT-LEAK-7c2e"
    seen = {"v": None}

    def fake_probe(*, client_id, client_secret):
        seen["v"] = client_secret
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://api.dingtalk.com",
        )

    monkeypatch.setattr(probe_mod, "dingtalk_token_probe", fake_probe)
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18792},
        "agents": {"defaults": {}, "list": []},
        "channels": {"dingtalk-connector": {
            "clientId": "ding_x", "clientSecret": SENTINEL,
        }},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg_path,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    ctx.probe = True
    coll = registry.get("channel")
    report = coll.collect(ctx)
    assert seen["v"] == SENTINEL
    from ocdiag.render.json_renderer import to_envelope
    envelope = to_envelope(report)
    blob = json.dumps(envelope, ensure_ascii=False)
    assert SENTINEL not in blob, "dingtalk plaintext secret leaked"


# ─── wecom variant ───────────────────────────────────────────────────
#
# Dual-mode: Bot (WS or webhook) + Agent (corp self-built app). Bot
# has no simple HTTP token-introspection endpoint we can call, so the
# L5 probe runs ONLY for Agent mode.


def _diagnose_wecom_with(channels_wecom, **kwargs):
    cfg = {"channels": {"wecom": channels_wecom}}
    return variant_wecom.diagnose(
        cfg=cfg, log_files=[], detect_basis="test", **kwargs,
    )


_VALID_AES_KEY = "x" * 43  # 43 chars → length-valid


def test_wecom_l1_no_mode_configured_fails():
    """Empty config — neither bot nor agent — is a silent dead-end."""
    rep = _diagnose_wecom_with({})
    codes = [f.code for f in rep.accounts[0].findings]
    assert "NO_MODE_CONFIGURED" in codes
    by_code = {f.code: f for f in rep.accounts[0].findings}
    assert by_code["NO_MODE_CONFIGURED"].verdict == "fail"


def test_wecom_l1_bot_cred_missing():
    rep = _diagnose_wecom_with({"botId": "b"})  # secret missing
    codes = [f.code for f in rep.accounts[0].findings]
    assert "BOT_CRED_MISSING" in codes


def test_wecom_l1_bot_full_no_findings():
    rep = _diagnose_wecom_with({"botId": "b", "secret": "s"})
    codes = [f.code for f in rep.accounts[0].findings if f.verdict == "fail"]
    assert codes == []


def test_wecom_l1_bot_webhook_inconsistent():
    """connectionMode=webhook requires token + encodingAESKey to verify
    + decrypt the callback payload (types/config.ts:36-38)."""
    rep = _diagnose_wecom_with({
        "botId": "b", "secret": "s",
        "connectionMode": "webhook",
        # token + encodingAESKey missing
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "BOT_WEBHOOK_INCONSISTENT" in codes


def test_wecom_l1_bot_webhook_complete_ok():
    rep = _diagnose_wecom_with({
        "botId": "b", "secret": "s",
        "connectionMode": "webhook",
        "token": "tok", "encodingAESKey": _VALID_AES_KEY,
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "BOT_WEBHOOK_INCONSISTENT" not in codes


def test_wecom_l1_agent_cred_missing():
    rep = _diagnose_wecom_with({
        "agent": {
            "corpId": "c",
            # corpSecret / token / encodingAESKey missing
        },
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "AGENT_CRED_MISSING" in codes


def test_wecom_l1_agent_aes_key_invalid_length():
    rep = _diagnose_wecom_with({
        "agent": {
            "corpId": "c", "corpSecret": "s", "token": "t",
            "encodingAESKey": "tooshort",  # not 43 chars
        },
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "AGENT_AES_KEY_INVALID" in codes


def test_wecom_l1_agent_aes_key_43_ok():
    rep = _diagnose_wecom_with({
        "agent": {
            "corpId": "c", "corpSecret": "s", "token": "t",
            "encodingAESKey": _VALID_AES_KEY,
        },
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "AGENT_AES_KEY_INVALID" not in codes
    assert "AGENT_CRED_MISSING" not in codes


def test_wecom_l1_dual_mode_both_configured():
    """Bot + Agent both configured → no NO_MODE_CONFIGURED."""
    rep = _diagnose_wecom_with({
        "botId": "b", "secret": "s",
        "agent": {
            "corpId": "c", "corpSecret": "cs", "token": "t",
            "encodingAESKey": _VALID_AES_KEY,
        },
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "NO_MODE_CONFIGURED" not in codes
    assert "BOT_CRED_MISSING" not in codes
    assert "AGENT_CRED_MISSING" not in codes


def test_wecom_l1_dm_policy_open_no_wildcard():
    rep = _diagnose_wecom_with({
        "botId": "b", "secret": "s",
        "dmPolicy": "open",
        "allowFrom": ["specific_user"],  # missing "*"
    })
    codes = [f.code for f in rep.accounts[0].findings]
    assert "DM_POLICY_OPEN_NO_WILDCARD" in codes


def test_wecom_l1_gate_sender_not_in_allowlist():
    rep = _diagnose_wecom_with(
        {
            "botId": "b", "secret": "s",
            "dmPolicy": "allowlist",
            "allowFrom": ["someone_else"],
        },
        sender_open_id="outsider",
    )
    codes = [f.code for f in rep.accounts[0].findings]
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in codes


# Real wecom log lines — every literal grep-verified in
# repos/channel-src/wecom-openclaw-plugin/src/ (citations beside each).
_WECOM_FAKE_LOG_LINES = [
    # dm-policy.ts:58
    _make_log_line(
        "[WeCom] Blocked DM from wuser1 (dmPolicy=disabled)",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:00:00",
    ),
    # dm-policy.ts:118
    _make_log_line(
        "[WeCom] Blocked unauthorized sender wuser2 "
        "(dmPolicy=allowlist)",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:01:00",
    ),
    # group-policy.ts:145
    _make_log_line(
        "[WeCom] Group chatX not allowed (groupPolicy=allowlist)",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:02:00",
    ),
    # group-policy.ts:158
    _make_log_line(
        "[WeCom] Sender wuser3 not in group chatY sender allowlist",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:03:00",
    ),
    # agent/handler.ts:247
    _make_log_line(
        "[wecom-agent] duplicate msgId=msg42 from=wuser4 "
        "chatId=chatZ type=text; skipped",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:04:00",
    ),
    # agent/handler.ts:271
    _make_log_line(
        "[wecom-agent] skip processing: type=event event=enter_chat "
        "from=wuser5 reason=non_command",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:05:00",
    ),
    # agent/handler.ts:495
    _make_log_line(
        "[wecom-agent] unauthorized command: replied via DM to wuser6",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:06:00",
    ),
    # webhook/handler.ts:335
    _make_log_line(
        "[wecom] inbound(http): reqId=req-99 skipped — no active "
        "targets",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:07:00",
    ),
    # media-uploader.ts:372
    _make_log_line(
        "[wecom] Media rejected: file too large",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:08:00",
    ),
    # monitor.ts:395
    _make_log_line(
        "[wecom] Media send failed: url=https://example/file.jpg, "
        "reason=upload_failed",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:09:00",
    ),
    # monitor.ts:967 (informational)
    _make_log_line(
        "[acct1] WebSocket connected",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:10:00",
    ),
    # monitor.ts:972
    _make_log_line(
        "[acct1] Authentication successful",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:11:00",
    ),
    # monitor.ts:978
    _make_log_line(
        "[acct1] WebSocket disconnected: server kicked",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:12:00",
    ),
    # monitor.ts:1012
    _make_log_line(
        "[acct1] Reconnecting attempt 3...",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:13:00",
    ),
    # monitor.ts:1017 (generic; the WSAuth*/WSReconnect* fixtures below
    # are the more-specific class-name promotions).
    _make_log_line(
        "[acct1] WebSocket error: connection refused",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:14:00",
    ),
    # monitor.ts:1033 — auth-failure terminal
    _make_log_line(
        "[acct1] Auth failure attempts exhausted (5 attempts). "
        "Please check botId/secret configuration.",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:15:00",
    ),
    # WSReconnectExhaustedError class name (monitor.ts:25)
    _make_log_line(
        "[acct1] WebSocket error: WSReconnectExhaustedError: max "
        "retries",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:16:00",
    ),
    # dm-policy.ts:95
    _make_log_line(
        "[WeCom] Pairing request created for sender=wuser7",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:17:00",
    ),
    # dm-policy.ts:112
    _make_log_line(
        "[WeCom] Pairing request already exists for sender=wuser8",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:18:00",
    ),
    # dm-policy.ts:109
    _make_log_line(
        "[WeCom] Failed to send pairing reply to wuser9: timeout",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:19:00",
    ),
    # mcp/interceptors/doc-auth-error.ts:105 — accountId quotes get
    # \"-encoded by the openclaw logger; we mirror that here so the
    # variant regex's optional ``\\?`` actually exercises both forms.
    _make_log_line(
        '[mcp] doc-auth-error: WSClient 未连接 '
        '(accountId=\\"acct1\\")，无法发送授权卡片',
        _PLUGIN_PATH_WECOM, "2026-06-10T12:20:00",
    ),
    # decoy
    _make_log_line(
        "[wecom] inbound(http): reqId=ok-1 path=/wecom method=POST",
        _PLUGIN_PATH_WECOM, "2026-06-10T12:21:00",
    ),
]


def test_wecom_log_signatures_against_ts_source(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_WECOM_FAKE_LOG_LINES) + "\n")
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {"botId": "b", "secret": "s"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    codes = {f.code for f in rep.log_findings}
    expected = {
        "LOG_BLOCKED_DM_DISABLED",
        "LOG_BLOCKED_UNAUTHORIZED_SENDER",
        "LOG_GROUP_NOT_ALLOWED",
        "LOG_SENDER_NOT_IN_GROUP_ALLOWLIST",
        "LOG_AGENT_DUPLICATE_MSGID",
        "LOG_AGENT_SKIP_PROCESSING",
        "LOG_AGENT_UNAUTHORIZED_COMMAND",
        "LOG_WEBHOOK_SKIPPED_NO_TARGETS",
        "LOG_MEDIA_REJECTED",
        "LOG_MEDIA_SEND_FAILED",
        "LOG_WS_CONNECTED",
        "LOG_WS_AUTHENTICATED",
        "LOG_WS_DISCONNECTED",
        "LOG_WS_RECONNECTING",
        "LOG_WS_ERROR",
        "LOG_WS_AUTH_FAILURE_EXHAUSTED",
        "LOG_WS_RECONNECT_EXHAUSTED_CLASS",
        "LOG_PAIRING_REQUEST_CREATED",
        "LOG_PAIRING_REQUEST_ALREADY_EXISTS",
        "LOG_PAIRING_REPLY_FAILED",
        "LOG_MCP_DOC_AUTH_NO_WS",
    }
    missing = expected - codes
    assert not missing, f"signatures missed: {missing}"


def test_wecom_log_severity_classification(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    log.write_text("\n".join(_WECOM_FAKE_LOG_LINES) + "\n")
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {"botId": "b", "secret": "s"}}},
        log_files=[str(log)],
        detect_basis="test",
    )
    by_code = {f.code: f.verdict for f in rep.log_findings}
    # SDK-terminal errors → fail.
    assert by_code["LOG_WS_AUTH_FAILURE_EXHAUSTED"] == "fail"
    assert by_code["LOG_WS_RECONNECT_EXHAUSTED_CLASS"] == "fail"
    # Gate decisions → warn.
    assert by_code["LOG_BLOCKED_DM_DISABLED"] == "warn"
    assert by_code["LOG_BLOCKED_UNAUTHORIZED_SENDER"] == "warn"
    assert by_code["LOG_GROUP_NOT_ALLOWED"] == "warn"
    assert by_code["LOG_WS_DISCONNECTED"] == "warn"
    assert by_code["LOG_WS_ERROR"] == "warn"
    assert by_code["LOG_MEDIA_REJECTED"] == "warn"
    assert by_code["LOG_PAIRING_REPLY_FAILED"] == "warn"
    assert by_code["LOG_MCP_DOC_AUTH_NO_WS"] == "warn"
    # Lifecycle & informational → ok.
    assert by_code["LOG_WS_CONNECTED"] == "ok"
    assert by_code["LOG_WS_AUTHENTICATED"] == "ok"
    assert by_code["LOG_WS_RECONNECTING"] == "ok"
    assert by_code["LOG_PAIRING_REQUEST_CREATED"] == "ok"
    assert by_code["LOG_AGENT_DUPLICATE_MSGID"] == "ok"


def test_wecom_log_no_log_files_silent():
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {"botId": "b", "secret": "s"}}},
        log_files=[], detect_basis="test",
    )
    assert rep.log_findings == []


def test_wecom_probe_bot_only_no_endpoint(monkeypatch):
    """Bot-only account (no agent block) → probe must NOT call out;
    surfaces ``WECOM_BOT_NO_PROBE_ENDPOINT`` info."""
    called = {"net": False}

    def fake_probe(*a, **kw):
        called["net"] = True
        return probe_mod.ProbeResult(state="valid")

    monkeypatch.setattr(probe_mod, "wecom_agent_token_probe", fake_probe)
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {"botId": "b", "secret": "s"}}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert called["net"] is False
    codes = {f.code for f in rep.probe_findings}
    assert "WECOM_BOT_NO_PROBE_ENDPOINT" in codes


def test_wecom_probe_agent_valid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "wecom_agent_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://qyapi.weixin.qq.com",
            extra={"expire_s": 7200},
        ),
    )
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {
            "agent": {
                "corpId": "corp1", "corpSecret": "literal",
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert rep.probe_findings[0].verdict == "ok"
    assert rep.probe_findings[0].code == "PROBE_VALID"


def test_wecom_probe_agent_invalid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "wecom_agent_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="invalid", code="CRED_REJECTED",
            api_code=40013, msg="invalid CorpID",
            domain="https://qyapi.weixin.qq.com",
        ),
    )
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {
            "agent": {
                "corpId": "corp1", "corpSecret": "literal",
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert rep.probe_findings[0].verdict == "fail"
    assert rep.probe_findings[0].code == "PROBE_INVALID"


def test_wecom_probe_agent_unreachable(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "wecom_agent_token_probe",
        lambda **kw: probe_mod.ProbeResult(
            state="unreachable", code="PROBE_UNREACHABLE",
            msg="timeout", domain="https://qyapi.weixin.qq.com",
        ),
    )
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {
            "agent": {
                "corpId": "corp1", "corpSecret": "literal",
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    # Unreachable must NEVER promote to fail.
    assert rep.probe_findings[0].verdict == "warn"
    assert rep.probe_findings[0].code == "PROBE_UNREACHABLE"


def test_wecom_probe_agent_skips_when_secret_unresolved(monkeypatch):
    called = {"net": False}

    def fake_probe(*a, **kw):
        called["net"] = True
        return probe_mod.ProbeResult(state="valid")

    monkeypatch.setattr(probe_mod, "wecom_agent_token_probe", fake_probe)
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {
            "agent": {
                "corpId": "corp1",
                "corpSecret": {
                    "source": "file", "provider": "missing-prov",
                    "id": "/x",
                },
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert called["net"] is False
    codes = [f.code for f in rep.probe_findings]
    assert "SECRET_UNRESOLVED" in codes


def test_wecom_probe_dual_mode_runs_agent_only(monkeypatch):
    """Bot + Agent both configured → only Agent probe runs (Bot has
    no HTTP token endpoint to probe)."""
    captured = {"agent_called": 0}

    def fake_probe(**kw):
        captured["agent_called"] += 1
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://qyapi.weixin.qq.com",
        )

    monkeypatch.setattr(probe_mod, "wecom_agent_token_probe", fake_probe)
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {
            "botId": "b", "secret": "s",
            "agent": {
                "corpId": "corp1", "corpSecret": "literal",
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }}},
        log_files=[], detect_basis="test", do_probe=True,
    )
    assert captured["agent_called"] == 1
    codes = [f.code for f in rep.probe_findings]
    # Exactly one finding (the agent one), no bot-skip alongside it.
    assert codes == ["PROBE_VALID"]


def test_wecom_collector_via_config_when_no_package(tmp_path):
    """End-to-end: channels.wecom present without @wecom package
    installed → wecom variant runs without crashing."""
    registry.discover()
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18793},
        "agents": {"defaults": {}, "list": []},
        "channels": {"wecom": {"botId": "b", "secret": "s"}},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home,
        config_path=cfg_path,
        log_dir=log_dir,
        sessions_base=home / "agents",
    )
    coll = registry.get("channel")
    report = coll.collect(ctx)
    titles = [s.title for s in report.sections]
    assert any("wecom · L1" in t for t in titles)


def test_wecom_account_inheritance():
    rep = _diagnose_wecom_with({
        "botId": "b_top", "secret": "s_top",
        "accounts": {
            "a1": {"dmPolicy": "open", "allowFrom": ["*"]},
            "a2": {"agent": {
                "corpId": "c2", "corpSecret": "cs2", "token": "t2",
                "encodingAESKey": _VALID_AES_KEY,
            }},
        },
    })
    by_id = {a.account_id: a for a in rep.accounts}
    assert "a1" in by_id and "a2" in by_id
    # a1 inherits bot creds → no BOT_CRED_MISSING
    assert "BOT_CRED_MISSING" not in [f.code for f in by_id["a1"].findings]
    # a2 inherits bot creds AND has its own agent block → both modes ok
    assert "BOT_CRED_MISSING" not in [f.code for f in by_id["a2"].findings]
    assert "AGENT_CRED_MISSING" not in [f.code for f in by_id["a2"].findings]


def test_wecom_does_not_leak_secret(tmp_path, monkeypatch):
    registry.discover()
    SENTINEL = "WECOM-SENTINEL-SHOULD-NOT-LEAK-3a51"
    seen = {"v": None}

    def fake_probe(*, corp_id, corp_secret):
        seen["v"] = corp_secret
        return probe_mod.ProbeResult(
            state="valid", api_code=0,
            domain="https://qyapi.weixin.qq.com",
        )

    monkeypatch.setattr(probe_mod, "wecom_agent_token_probe", fake_probe)
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18794},
        "agents": {"defaults": {}, "list": []},
        "channels": {"wecom": {
            "agent": {
                "corpId": "corp1", "corpSecret": SENTINEL,
                "token": "t", "encodingAESKey": _VALID_AES_KEY,
            },
        }},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    ctx = DiagContext(
        openclaw_home=home, config_path=cfg_path,
        log_dir=log_dir, sessions_base=home / "agents",
    )
    ctx.probe = True
    coll = registry.get("channel")
    report = coll.collect(ctx)
    assert seen["v"] == SENTINEL
    from ocdiag.render.json_renderer import to_envelope
    envelope = to_envelope(report)
    blob = json.dumps(envelope, ensure_ascii=False)
    assert SENTINEL not in blob, "wecom plaintext secret leaked"


# ─── probe.py — wecom_agent_token_probe + dingtalk_token_probe ───────


def test_dingtalk_token_probe_url_shape(monkeypatch):
    """The probe must POST to api.dingtalk.com/v1.0/oauth2/accessToken
    with {appKey, appSecret} (verified literal at upstream
    probe.ts:101-104)."""
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return 200, {"accessToken": "tok123", "expireIn": 7200}, ""

    monkeypatch.setattr(probe_mod, "_post_json", fake_post)
    res = probe_mod.dingtalk_token_probe(
        client_id="ding_x", client_secret="literal",
    )
    assert captured["url"] == (
        "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    )
    assert captured["payload"] == {
        "appKey": "ding_x", "appSecret": "literal",
    }
    assert res.state == "valid"
    assert res.extra.get("expire_s") == 7200


def test_wecom_agent_token_probe_url_shape(monkeypatch):
    """The probe must GET qyapi.weixin.qq.com/cgi-bin/gettoken with
    corpid+corpsecret in the query string (const.ts:165 +
    agent/api-client.ts:103)."""
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return 200, {
            "errcode": 0, "errmsg": "ok",
            "access_token": "tok123", "expires_in": 7200,
        }, ""

    monkeypatch.setattr(probe_mod, "_get_json", fake_get)
    res = probe_mod.wecom_agent_token_probe(
        corp_id="corpA", corp_secret="literal",
    )
    assert captured["url"].startswith(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken?"
    )
    assert "corpid=corpA" in captured["url"]
    assert "corpsecret=literal" in captured["url"]
    assert res.state == "valid"
    assert res.extra.get("expire_s") == 7200


def test_wecom_agent_token_probe_invalid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "_get_json",
        lambda url: (
            200,
            {"errcode": 40013, "errmsg": "invalid CorpID"},
            "",
        ),
    )
    res = probe_mod.wecom_agent_token_probe(
        corp_id="bad", corp_secret="bad",
    )
    assert res.state == "invalid"
    assert res.api_code == 40013
    assert "invalid CorpID" in (res.msg or "")


def test_wecom_agent_token_probe_unreachable(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "_get_json",
        lambda url: (0, {}, "timeout"),
    )
    res = probe_mod.wecom_agent_token_probe(
        corp_id="x", corp_secret="y",
    )
    assert res.state == "unreachable"
    assert "timeout" in (res.msg or "")


def test_dingtalk_token_probe_invalid(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "_post_json",
        lambda url, payload: (
            401,
            {"code": "InvalidAuthentication",
             "message": "AppKey is invalid"},
            "",
        ),
    )
    res = probe_mod.dingtalk_token_probe(
        client_id="bad", client_secret="bad",
    )
    assert res.state == "invalid"
    # api_code falls back to status when the platform code is a string
    # (DingTalk returns a string code field, not an integer).
    assert res.extra.get("raw_code") == "InvalidAuthentication"


def test_dingtalk_token_probe_unreachable(monkeypatch):
    monkeypatch.setattr(
        probe_mod, "_post_json",
        lambda url, payload: (0, {}, "timeout"),
    )
    res = probe_mod.dingtalk_token_probe(
        client_id="x", client_secret="y",
    )
    assert res.state == "unreachable"


# ─── self-pollution reverse tests ────────────────────────────────────
#
# The openclaw gateway's console relay (``dist/console-*.js``) captures
# the assistant's own outbound chat — including any text that mentions
# a literal signature string from a prior diagnostic report. Without
# the path-filter + ``^``-anchor defences, those captured messages
# would be re-matched on the NEXT run as if the plugin itself had
# emitted them. Each reverse fixture below packs a complete signature
# string verbatim into a relay-path log line; the variant scan MUST
# return zero log findings.


def test_feishu_bundled_self_pollution_console_relay_ignored(tmp_path):
    """A prior report's text echoed by the gateway console relay must
    not produce findings — even though every signature string the
    bundled variant knows about appears verbatim in the message."""
    log = tmp_path / "openclaw-2026-06-10.log"
    poison = (
        # Concatenated verbatim signatures the assistant might quote
        # in a report or chat — every one is a real plugin literal.
        "feishu[main]: WebSocket connection ended, recreating client "
        "in 5000ms: WebSocket connect failed and autoReconnect is "
        "disabled. feishu[main]: bot identity background retry "
        "exhausted; requireMention group messages may be skipped "
        "until restart. feishu[main]: blocked unauthorized sender "
        "ou_xyz (dmPolicy=allowlist). 我抓到了 feishu[main]: webhook "
        "anomaly path=/feishu/events status=403 count=5"
    )
    log.write_text(
        _make_log_line(
            poison, _GATEWAY_CONSOLE_RELAY_PATH,
            "2026-06-10T17:00:00",
        ) + "\n",
    )
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)], detect_basis="test",
    )
    assert rep.log_findings == [], (
        f"self-pollution leaked through filter: "
        f"{[f.code for f in rep.log_findings]}"
    )


def test_feishu_lark_self_pollution_console_relay_ignored(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    poison = (
        "feishu[main]: webhook mode not implemented in monitor. "
        "feishu[main]: DM disabled by policy, rejecting sender ou_bob. "
        "feishu[main]: drop bot sender ou_bot1 in oc_chat5 "
        "(allowBots=false). 这是诊断器自己说的话被 console relay 抓到了"
    )
    log.write_text(
        _make_log_line(
            poison, _GATEWAY_CONSOLE_RELAY_PATH,
            "2026-06-10T17:01:00",
        ) + "\n",
    )
    rep = feishu_lark.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)], detect_basis="test",
    )
    assert rep.log_findings == [], (
        f"self-pollution leaked through filter: "
        f"{[f.code for f in rep.log_findings]}"
    )


def test_dingtalk_self_pollution_console_relay_ignored(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    poison = (
        "[DingTalk:main] DM 被拦截: senderId=ding_user1 (Alice) "
        "不在白名单中. [DingTalk:main] 连接建立失败: socket hang up. "
        "dingtalk-connector[acct1] is disabled, skipping startup. "
        '[DingTalk:main] dmPolicy=\\"pairing\\" 暂不支持，将按 '
        '\\"open\\" 策略处理'
    )
    log.write_text(
        _make_log_line(
            poison, _GATEWAY_CONSOLE_RELAY_PATH,
            "2026-06-10T17:02:00",
        ) + "\n",
    )
    rep = variant_dingtalk.diagnose(
        cfg={"channels": {"dingtalk-connector": {
            "clientId": "x", "clientSecret": "y",
        }}},
        log_files=[str(log)], detect_basis="test",
    )
    assert rep.log_findings == [], (
        f"self-pollution leaked through filter: "
        f"{[f.code for f in rep.log_findings]}"
    )


def test_wecom_self_pollution_console_relay_ignored(tmp_path):
    log = tmp_path / "openclaw-2026-06-10.log"
    poison = (
        "[WeCom] Blocked DM from wuser1 (dmPolicy=disabled). "
        "[acct1] WebSocket error: WSReconnectExhaustedError: max "
        "retries. [acct1] Auth failure attempts exhausted (5 "
        "attempts). Please check botId/secret configuration. "
        "[wecom-agent] unauthorized command: replied via DM to wuser6"
    )
    log.write_text(
        _make_log_line(
            poison, _GATEWAY_CONSOLE_RELAY_PATH,
            "2026-06-10T17:03:00",
        ) + "\n",
    )
    rep = variant_wecom.diagnose(
        cfg={"channels": {"wecom": {"botId": "b", "secret": "s"}}},
        log_files=[str(log)], detect_basis="test",
    )
    assert rep.log_findings == [], (
        f"self-pollution leaked through filter: "
        f"{[f.code for f in rep.log_findings]}"
    )


# ─── --account scoping ──────────────────────────────────────────────
#
# `--account <id>` narrows ``channel`` diagnostics to a single account.
# The motivating bug: GATE_SENDER_NOT_IN_ALLOWLIST runs against EVERY
# account when --sender is set, since a sender belongs to one account
# the others all emit a spurious warn. Scoping kills that noise.


def test_account_filter_narrows_to_single_account():
    """``account_filter='a1'`` → only that AccountReport is returned;
    other configured accounts are skipped entirely."""
    rep = _diagnose_with(
        {
            "appId": "cli_top", "appSecret": "s_top",
            "accounts": {
                "a1": {"appId": "cli_a1", "appSecret": "s1"},
                "a2": {"appId": "cli_a2", "appSecret": "s2"},
                "a3": {"appId": "cli_a3", "appSecret": "s3"},
            },
        },
        account_filter="a1",
    )
    ids = sorted(a.account_id for a in rep.accounts)
    assert ids == ["a1"]


def test_account_filter_unknown_id_emits_note_and_no_accounts():
    """Filter set to an id that doesn't exist → zero accounts iterated
    AND a note listing the available ids so the user gets actionable
    feedback rather than a silent-empty diagnosis."""
    rep = _diagnose_with(
        {
            "accounts": {
                "a1": {"appId": "cli_a1", "appSecret": "s1"},
                "a2": {"appId": "cli_a2", "appSecret": "s2"},
            },
        },
        account_filter="nonexistent",
    )
    assert rep.accounts == []
    note_blob = " ".join(rep.notes)
    assert "nonexistent" in note_blob
    assert "a1" in note_blob and "a2" in note_blob


def test_account_filter_none_preserves_all_accounts():
    """Regression guard: ``account_filter=None`` (default) must keep
    today's all-accounts behavior."""
    rep = _diagnose_with({
        "accounts": {
            "a1": {"appId": "cli_a1", "appSecret": "s1"},
            "a2": {"appId": "cli_a2", "appSecret": "s2"},
        },
    })
    ids = sorted(a.account_id for a in rep.accounts)
    assert ids == ["a1", "a2"]


def test_account_filter_silences_cross_account_sender_gate_noise():
    """The core value test. Two accounts; --sender is in account A's
    allowFrom only. Without --account → account B emits a spurious
    GATE_SENDER_NOT_IN_ALLOWLIST. With ``account_filter='A'`` →
    only A is diagnosed, so the cross-account warn is silenced."""
    cfg = {
        "accounts": {
            "A": {
                "appId": "cli_A", "appSecret": "sA",
                "dmPolicy": "allowlist",
                "allowFrom": ["ou_alice"],
            },
            "B": {
                "appId": "cli_B", "appSecret": "sB",
                "dmPolicy": "allowlist",
                "allowFrom": ["ou_bob"],  # alice NOT here
            },
        },
    }
    # Without --account: alice fires the warn against B.
    rep_all = _diagnose_with(cfg, sender_open_id="ou_alice")
    by_id_all = {a.account_id: a for a in rep_all.accounts}
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" in [
        f.code for f in by_id_all["B"].findings
    ]
    # With --account=A: only A diagnosed, no spurious B warn.
    rep_scoped = _diagnose_with(
        cfg, sender_open_id="ou_alice", account_filter="A",
    )
    ids_scoped = [a.account_id for a in rep_scoped.accounts]
    assert ids_scoped == ["A"]
    a_codes = [f.code for f in rep_scoped.accounts[0].findings]
    # alice IS in A's allowFrom so the gate doesn't fire on A either —
    # net result: zero GATE_SENDER findings, which is the whole point.
    assert "GATE_SENDER_NOT_IN_ALLOWLIST" not in a_codes


def test_lark_account_filter_narrows():
    rep = _diagnose_lark_with(
        {
            "accounts": {
                "primary": {"appId": "cli_p", "appSecret": "sp"},
                "secondary": {"appId": "cli_s", "appSecret": "ss"},
            },
        },
        account_filter="secondary",
    )
    ids = [a.account_id for a in rep.accounts]
    assert ids == ["secondary"]


def test_lark_account_filter_unknown_id_emits_note():
    rep = _diagnose_lark_with(
        {
            "accounts": {
                "primary": {"appId": "cli_p", "appSecret": "sp"},
            },
        },
        account_filter="ghost",
    )
    assert rep.accounts == []
    note_blob = " ".join(rep.notes)
    assert "ghost" in note_blob and "primary" in note_blob


def test_dingtalk_account_filter_narrows():
    rep = _diagnose_dingtalk_with(
        {
            "accounts": {
                "a1": {"clientId": "ding_a1", "clientSecret": "s1"},
                "a2": {"clientId": "ding_a2", "clientSecret": "s2"},
            },
        },
        account_filter="a2",
    )
    ids = [a.account_id for a in rep.accounts]
    assert ids == ["a2"]


def test_wecom_account_filter_narrows():
    rep = _diagnose_wecom_with(
        {
            "accounts": {
                "team1": {"botId": "b1", "secret": "s1"},
                "team2": {"botId": "b2", "secret": "s2"},
            },
        },
        account_filter="team1",
    )
    ids = [a.account_id for a in rep.accounts]
    assert ids == ["team1"]


def test_main_build_context_populates_account_id():
    """``main._build_context(args)`` must lift ``args.account`` (the
    --account CLI flag) into ``ctx.account_id`` so the channel
    collector can reach it without a kwargs hack."""
    from argparse import Namespace
    from ocdiag.main import _build_context

    args = Namespace(
        config="/tmp/cfg.json", log_dir="/tmp/logs",
        sessions_base="/tmp/sessions", openclaw_home="/tmp/home",
        format=None, json=False, no_color=False, unmask=False,
        probe=False, sender=None, account="prod-account-1",
    )
    ctx = _build_context(args)
    assert ctx.account_id == "prod-account-1"

    args.account = None
    ctx = _build_context(args)
    assert ctx.account_id is None


def test_collector_account_filter_via_ctx_scopes_diagnosis(tmp_path):
    """End-to-end: setting ``ctx.account_id`` (the path the CLI's
    ``--account`` flag uses) must scope the channel diagnosis to a
    single account in the rendered Report sections."""
    registry.discover()
    home = tmp_path
    (home / "agents").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "openclaw.json"
    cfg_path.write_text(json.dumps({
        "gateway": {"port": 18795},
        "agents": {"defaults": {}, "list": []},
        "channels": {"feishu": {
            "accounts": {
                "alpha": {"appId": "cli_a", "appSecret": "sa"},
                "beta": {"appId": "cli_b", "appSecret": "sb"},
            },
        }},
    }))
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    pkg_dir = (
        home / "npm" / "projects" / "openclaw-feishu-test"
        / "node_modules" / "@openclaw" / "feishu"
    )
    pkg_dir.mkdir(parents=True)
    ctx = DiagContext(
        openclaw_home=home,
        config_path=cfg_path,
        log_dir=log_dir,
        sessions_base=home / "agents",
    )
    ctx.account_id = "alpha"
    coll = registry.get("channel")
    report = coll.collect(ctx)
    assert report.data.get("account_filter") == "alpha"
    # Per-account header check is named
    # ``channel.account.<account_id>`` — only alpha's header should
    # appear in the rendered sections.
    names = [c.name for s in report.sections for c in s.checks]
    assert "channel.account.alpha" in names
    assert "channel.account.beta" not in names


def test_self_pollution_anchor_blocks_embedded_signature(tmp_path):
    """Anchor defence (independent of path filter): even on an
    unknown path (no _meta.path field at all), a signature string
    embedded mid-sentence — not at the START of the message — must
    NOT match. This is the second layer that catches console-relay
    text pasted into a non-relay sink, or older log formats with no
    path metadata.
    """
    log = tmp_path / "openclaw-2026-06-10.log"
    # Note: no ``_meta.path`` field. Helper falls through, and the
    # signature must be ignored because it's mid-sentence.
    line = json.dumps({
        "time": "2026-06-10T17:04:00",
        "message": (
            "earlier the report said feishu[main]: blocked "
            "unauthorized sender ou_xyz (dmPolicy=allowlist) — "
            "see attached"
        ),
    }, ensure_ascii=False)
    log.write_text(line + "\n")
    rep = feishu_bundled.diagnose(
        cfg={"channels": {"feishu": {"appId": "x", "appSecret": "y"}}},
        log_files=[str(log)], detect_basis="test",
    )
    assert rep.log_findings == [], (
        f"anchor failed to block embedded signature: "
        f"{[f.code for f in rep.log_findings]}"
    )
