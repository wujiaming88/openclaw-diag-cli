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

Flags:
    --dry-run    Print every target path that WOULD be written to, without
                 creating directories or copying any file. No side effects.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "skill" / "openclaw-diag"  # skill/openclaw-diag/
SKILL_MD = SKILL_DIR / "SKILL.md"


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter for frameworks that don't expect it."""
    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def install_openclaw(dry_run: bool = False) -> None:
    dest = Path.home() / ".openclaw" / "skills" / "openclaw-diag"
    target = dest / "SKILL.md"
    if dry_run:
        print(f"  would write  OpenClaw: {target}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_MD, target)
    print(f"  ok  OpenClaw: {target}")


def install_claude_code(dry_run: bool = False) -> None:
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        # Real install silently skips when the framework isn't present;
        # mirror that for dry-run so the listing matches what would actually
        # happen.
        return
    dest_dir = claude_dir / "commands"
    target = dest_dir / "openclaw-diag.md"
    if dry_run:
        print(f"  would write  Claude Code: {target}")
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    content = _strip_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    target.write_text(content, encoding="utf-8")
    print(f"  ok  Claude Code: {target}")


def install_codex(dry_run: bool = False) -> None:
    codex_dir = Path.home() / ".codex"
    if not codex_dir.exists():
        return
    dest_dir = codex_dir / "instructions"
    target = dest_dir / "openclaw-diag.md"
    if dry_run:
        print(f"  would write  Codex: {target}")
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_MD, target)
    print(f"  ok  Codex: {target}")


def install_cursor(dry_run: bool = False) -> None:
    cursor_dir = Path.home() / ".cursor"
    if not cursor_dir.exists():
        return
    rules_dir = cursor_dir / "rules"
    target = rules_dir / "openclaw-diag.mdc"
    if dry_run:
        print(f"  would write  Cursor: {target}")
        return
    rules_dir.mkdir(parents=True, exist_ok=True)
    content = _strip_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    target.write_text(content, encoding="utf-8")
    print(f"  ok  Cursor: {target}")


def main() -> int:
    # Stdlib-only flag check — argparse would be overkill for one switch and
    # the wrapper (bin/openclaw-diag.js) already filters --help/-h before
    # spawning us, so we only need to recognize --dry-run here.
    dry_run = "--dry-run" in sys.argv[1:]

    if not SKILL_MD.exists():
        print(f"Error: SKILL.md not found at {SKILL_MD}", file=sys.stderr)
        return 1
    if dry_run:
        print("Dry-run: openclaw-diag skill install would write the following targets")
        print("(no files or directories will be created):")
    else:
        print("Installing openclaw-diag skill...")
    installers = (
        install_openclaw,
        install_claude_code,
        install_codex,
        install_cursor,
    )
    for fn in installers:
        try:
            fn(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip  {fn.__name__}: {exc}", file=sys.stderr)
    print("Done." if not dry_run else "Dry-run complete — no files written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
