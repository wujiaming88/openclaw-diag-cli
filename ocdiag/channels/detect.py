"""Variant detection — work out which IM-channel plugin variant is installed.

Detection priority follows the design doc §1:
  1. Installed npm package under ``~/.openclaw/npm/projects/*/node_modules/``.
     This is the strongest signal because each variant ships under a unique
     scoped name (``@openclaw/feishu`` vs ``@larksuite/openclaw-lark``).
  2. ``channels.<provider>`` config key as a fallback. This catches
     setups where the package was installed system-wide (outside of
     OpenClaw's per-project npm tree) but the config still points at it.

When neither source identifies a variant we return an empty list and the
collector renders a single ``NO_CHANNEL_DETECTED`` info — the design rule
is "no evidence ⇒ no diagnosis", never guess.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# Mapping: package name → variant id. Multiple package names may resolve to
# the same variant when an upstream fork ships a renamed publish (see
# m1heng-clawd/feishu, which is bundled-feishu's community fork).
_PACKAGE_TO_VARIANT: Dict[str, str] = {
    "@openclaw/feishu": "feishu-bundled",
    "@m1heng-clawd/feishu": "feishu-bundled",
    "@larksuite/openclaw-lark": "feishu-lark",
    "@dingtalk-real-ai/dingtalk-connector": "dingtalk",
    "@wecom/wecom-openclaw-plugin": "wecom",
}

# Mapping: config key → variant id. Note: ``channels.feishu`` is shared
# between feishu-bundled and feishu-lark. We only fall through to this map
# when no package was found, so the ambiguity is unresolved → we record
# both candidates for the human and let them pick.
_CONFIG_KEY_TO_VARIANTS: Dict[str, List[str]] = {
    "feishu": ["feishu-bundled", "feishu-lark"],
    "wecom": ["wecom"],
    # ``dingtalk-connector`` is upstream's canonical key (CHANNEL_ID =
    # "dingtalk-connector" in dingtalk-openclaw-connector/src/channel.ts:35);
    # ``dingtalk`` is the shorter form some configs use. Both flow to
    # the same variant module.
    "dingtalk-connector": ["dingtalk"],
    "dingtalk": ["dingtalk"],
}


@dataclass
class DetectedVariant:
    variant: str
    detect_basis: str          # human-readable evidence string
    package_name: Optional[str] = None
    package_path: Optional[str] = None
    config_key: Optional[str] = None
    ambiguous: bool = False    # True when only config-key resolved it


def _scan_npm_packages(npm_projects_root: str) -> List[DetectedVariant]:
    """Walk ``~/.openclaw/npm/projects/*/node_modules`` for known packages.

    OpenClaw 2026.6.x stores per-project plugin trees under hashed dir
    names like ``openclaw-feishu-dc69f44688``; the installed packages live
    inside their respective ``node_modules`` subtree. We don't care about
    the project hash — we just want to know which packages exist.
    """
    found: List[DetectedVariant] = []
    if not os.path.isdir(npm_projects_root):
        return found
    for project_dir in sorted(glob.glob(os.path.join(npm_projects_root, "*"))):
        node_modules = os.path.join(project_dir, "node_modules")
        if not os.path.isdir(node_modules):
            continue
        for pkg_name, variant in _PACKAGE_TO_VARIANT.items():
            # @scope/name → scope/name on disk
            disk_path = os.path.join(node_modules, *pkg_name.split("/"))
            if os.path.isdir(disk_path):
                found.append(DetectedVariant(
                    variant=variant,
                    detect_basis=f"pkg:{pkg_name}",
                    package_name=pkg_name,
                    package_path=disk_path,
                ))
    # Dedup: same variant reported by multiple project trees → keep the
    # first we saw (already sorted, so deterministic).
    seen = set()
    deduped: List[DetectedVariant] = []
    for d in found:
        if d.variant in seen:
            continue
        seen.add(d.variant)
        deduped.append(d)
    return deduped


def _scan_config(cfg: Dict[str, Any]) -> List[DetectedVariant]:
    """Look at ``channels.<provider>`` keys for variant hints.

    Only used as a fallback when package scanning yields nothing.
    Ambiguous keys (``channels.feishu`` could be either bundled or lark)
    surface every candidate — the collector will show all of them with
    ``ambiguous=true`` so the user can disambiguate.
    """
    out: List[DetectedVariant] = []
    channels = cfg.get("channels") if isinstance(cfg, dict) else None
    if not isinstance(channels, dict):
        return out
    for key, variants in _CONFIG_KEY_TO_VARIANTS.items():
        if key not in channels:
            continue
        ambiguous = len(variants) > 1
        for v in variants:
            out.append(DetectedVariant(
                variant=v,
                detect_basis=f"config:channels.{key}",
                config_key=key,
                ambiguous=ambiguous,
            ))
    return out


def detect_variants(
    npm_projects_root: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[DetectedVariant]:
    """Detect every channel variant we can identify on this host.

    Strategy:
      1. Package-based detection — strongest evidence (a real install
         tree, not just a config dangling at a removed plugin).
      2. If (1) finds nothing AND ``cfg`` carries channel keys, fall
         back to config-based detection (recorded as ``ambiguous`` when
         the key maps to multiple candidate variants).

    Multiple variants on the same host are valid (e.g. a user testing
    bundled and lark side-by-side); the collector diagnoses each
    independently.
    """
    found = _scan_npm_packages(npm_projects_root)
    if found:
        return found
    if cfg:
        return _scan_config(cfg)
    return []


def npm_projects_root(openclaw_home: str) -> str:
    """Resolve the canonical npm-projects root for this OpenClaw home.

    Centralized so tests and the collector agree on the path; gateway /
    plugin tooling stores per-project node_modules trees here regardless
    of the gateway port or profile.
    """
    return os.path.join(openclaw_home, "npm", "projects")
