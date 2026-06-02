#!/usr/bin/env python3
"""Install openclaw-diag skill to supported agent frameworks.

Drops a copy of SKILL.md into each detected framework's skill/instructions
directory. Installation is best-effort: missing frameworks are silently
skipped. The script never overwrites or deletes anything outside the
framework-specific install paths.

Install paths:
    OpenClaw:    ~/.openclaw/skills/openclaw-diag/SKILL.md
    Claude Code: ~/.claude/commands/openclaw-diag.md
    Codex:       ~/.codex/instructions/openclaw-diag.md
    Cursor:      ~/.cursor/rules/openclaw-diag.mdc
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent  # skill/openclaw-diag/
SKILL_MD = SKILL_DIR / "SKILL.md"


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter for frameworks that don't expect it."""
    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def install_openclaw() -> None:
    dest = Path.home() / ".openclaw" / "skills" / "openclaw-diag"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_MD, dest / "SKILL.md")
    print(f"  ok  OpenClaw: {dest / 'SKILL.md'}")


def install_claude_code() -> None:
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return
    dest_dir = claude_dir / "commands"
    dest_dir.mkdir(parents=True, exist_ok=True)
    content = _strip_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    (dest_dir / "openclaw-diag.md").write_text(content, encoding="utf-8")
    print(f"  ok  Claude Code: {dest_dir / 'openclaw-diag.md'}")


def install_codex() -> None:
    codex_dir = Path.home() / ".codex"
    if not codex_dir.exists():
        return
    dest_dir = codex_dir / "instructions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_MD, dest_dir / "openclaw-diag.md")
    print(f"  ok  Codex: {dest_dir / 'openclaw-diag.md'}")


def install_cursor() -> None:
    cursor_dir = Path.home() / ".cursor"
    if not cursor_dir.exists():
        return
    rules_dir = cursor_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    content = _strip_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    (rules_dir / "openclaw-diag.mdc").write_text(content, encoding="utf-8")
    print(f"  ok  Cursor: {rules_dir / 'openclaw-diag.mdc'}")


def main() -> int:
    if not SKILL_MD.exists():
        print(f"Error: SKILL.md not found at {SKILL_MD}", file=sys.stderr)
        return 1
    print("Installing openclaw-diag skill...")
    installers = (
        install_openclaw,
        install_claude_code,
        install_codex,
        install_cursor,
    )
    for fn in installers:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  skip  {fn.__name__}: {exc}", file=sys.stderr)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
