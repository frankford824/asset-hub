from __future__ import annotations

import fnmatch
from pathlib import Path


DEFAULT_IGNORES = ("Thumbs.db", "desktop.ini", "._*")


def should_ignore(name: str, patterns: list[str] | tuple[str, ...] | None = None) -> bool:
    pats = patterns or DEFAULT_IGNORES
    base = Path(name).name
    for pat in pats:
        if fnmatch.fnmatch(base, pat):
            return True
        # AppleDouble prefix
        if pat == "._*" and base.startswith("._"):
            return True
    return False
