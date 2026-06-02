"""ANSI color primitives. Minimal, no deps."""
from __future__ import annotations

import re

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
BOLD_WHITE = "\x1b[1;37m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def colorize(text: str, code: str, use_color: bool = True) -> str:
    if not use_color:
        return text
    return f"{code}{text}{RESET}"


def bold(text: str, use_color: bool = True) -> str:
    return colorize(text, BOLD, use_color)


def dim(text: str, use_color: bool = True) -> str:
    return colorize(text, DIM, use_color)


def red(text: str, use_color: bool = True) -> str:
    return colorize(text, RED, use_color)


def green(text: str, use_color: bool = True) -> str:
    return colorize(text, GREEN, use_color)


def yellow(text: str, use_color: bool = True) -> str:
    return colorize(text, YELLOW, use_color)


def cyan(text: str, use_color: bool = True) -> str:
    return colorize(text, CYAN, use_color)


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)
