"""Pre-commit credential scanning helpers.

Extracts pattern-matching logic so it can be tested independently
of the git hook (which is a bash script calling this module).
"""

from __future__ import annotations

import re
from typing import List

CREDENTIAL_PATTERNS: List[str] = [
    r'GH_TOKEN\s*=\s*\S{10,}',
    r'SUPABASE_ANON_KEY\s*=\s*\S{20,}',
    r'TELEGRAM_BOT_TOKEN\s*=\s*\d+:[A-Za-z0-9_-]{20,}',
    r'sk-ant-[A-Za-z0-9_-]{20,}',
    r'BASE_LLM_API_KEY\s*=\s*\S{10,}',
    r'ANTHROPIC_API_KEY\s*=\s*\S{10,}',
]

_COMPILED = [re.compile(p) for p in CREDENTIAL_PATTERNS]


def scan_text(text: str) -> List[str]:
    """Return list of matched credential patterns found in *text*.

    Each element is the substring that matched. An empty list means
    the text is clean.
    """
    matches: List[str] = []
    for pattern in _COMPILED:
        for m in pattern.finditer(text):
            matches.append(m.group(0))
    return matches


def scan_lines(lines: List[str]) -> List[tuple[int, str, str]]:
    """Scan lines and return (line_number, line, match) tuples.

    Line numbers are 1-based.
    """
    results: List[tuple[int, str, str]] = []
    for idx, line in enumerate(lines, start=1):
        for pattern in _COMPILED:
            m = pattern.search(line)
            if m:
                results.append((idx, line.rstrip(), m.group(0)))
    return results
