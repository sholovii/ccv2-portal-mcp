"""Build log handling: the portal returns a zip of plain-text logs; agents want lines."""

from __future__ import annotations

import io
import re
import zipfile

ERROR_PATTERNS = [
    re.compile(p)
    for p in (
        r"BUILD FAILED",
        r"\[ERROR\]",
        r"\bERROR\b",
        r"error: ",
        r"Exception\b",
        r"FAILED\b",
        r"Tests run: .*Failures: [1-9]",
        r"Compilation failed",
    )
]


def extract_entries(zip_bytes: bytes) -> list[tuple[str, str]]:
    """Return (name, text) for every text entry, largest first."""
    entries: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw = zf.read(info)
            entries.append((info.filename, raw.decode("utf-8", errors="replace")))
    entries.sort(key=lambda e: len(e[1]), reverse=True)
    return entries


def select_lines(
    text: str,
    *,
    grep: str | None = None,
    head: int | None = None,
    tail: int | None = None,
    context: int = 0,
    max_lines: int = 400,
) -> tuple[list[str], int, bool]:
    """Pick lines from `text`. Returns (lines, total_lines, truncated)."""
    lines = text.splitlines()
    total = len(lines)
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        keep: set[int] = set()
        for i, line in enumerate(lines):
            if rx.search(line):
                keep.update(range(max(0, i - context), min(total, i + context + 1)))
        chosen = [f"{i + 1}: {lines[i]}" for i in sorted(keep)]
    else:
        chosen = [f"{i + 1}: {line}" for i, line in enumerate(lines)]
    if head is not None:
        chosen = chosen[:head]
    if tail is not None:
        chosen = chosen[-tail:]
    truncated = len(chosen) > max_lines
    if truncated:
        chosen = chosen[-max_lines:] if tail is not None else chosen[:max_lines]
    return chosen, total, truncated


def first_error(text: str, *, before: int = 5, after: int = 40) -> list[str] | None:
    """A window of lines around the first line that looks like a build error."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(p.search(line) for p in ERROR_PATTERNS):
            lo, hi = max(0, i - before), min(len(lines), i + after + 1)
            return [f"{j + 1}: {lines[j]}" for j in range(lo, hi)]
    return None
