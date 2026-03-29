"""Ctags-based repo-map provider.

Uses universal-ctags (``ctags`` CLI) to generate a compact structural map
of a Python repository.  Falls back to a simple file listing when ctags
is not installed.  Zero extra Python dependencies.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.swe_team.providers.repomap.base import RepoMap, RepoMapEntry

logger = logging.getLogger(__name__)


class CtagsRepoMapProvider:
    """Repo-map provider backed by universal-ctags.

    Parameters
    ----------
    config:
        Optional provider config dict (currently unused, reserved for future
        options like language filters or custom ctags flags).
    """

    _IGNORE_DEFAULTS = [
        "*.pyc",
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        "*.egg-info",
    ]

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}

    def is_available(self) -> bool:
        """Check if ctags binary is available on PATH."""
        try:
            result = subprocess.run(
                ["ctags", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def health_check(self) -> bool:
        """Alias for is_available — satisfies the Protocol."""
        return self.is_available()

    def generate(
        self,
        repo_path: Path,
        max_tokens: int = 2000,
        ignore: list[str] | None = None,
    ) -> RepoMap:
        """Generate a repo map from *repo_path*.

        1. Run ``ctags --output-format=json -R --languages=Python``
        2. Parse JSON output into RepoMapEntry objects
        3. Group by file, sort by line number
        4. If ctags is unavailable, fall back to file listing
        5. Truncate to *max_tokens* (approx 4 chars per token)
        """
        effective_ignore = list(self._IGNORE_DEFAULTS)
        if ignore:
            effective_ignore.extend(ignore)

        if not self.is_available():
            return self._fallback_file_listing(repo_path, effective_ignore)

        try:
            result = subprocess.run(
                [
                    "ctags",
                    "--output-format=json",
                    "-R",
                    "--languages=Python",
                    str(repo_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(repo_path),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return self._fallback_file_listing(repo_path, effective_ignore)

        if result.returncode != 0:
            logger.warning("ctags failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return self._fallback_file_listing(repo_path, effective_ignore)

        entries: list[RepoMapEntry] = []
        repo_str = str(repo_path)

        for raw_line in result.stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                tag = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if tag.get("_type") != "tag":
                continue

            filepath = tag.get("path", "")
            # Make relative to repo_path
            if filepath.startswith(repo_str):
                filepath = filepath[len(repo_str):].lstrip("/")

            # Apply ignore patterns
            if self._should_ignore(filepath, effective_ignore):
                continue

            kind = tag.get("kind", "")
            symbol_type = self._map_kind(kind)
            name = tag.get("name", "")
            signature = tag.get("signature", "")
            line = tag.get("line", None)

            if name.startswith("_") and symbol_type == "variable":
                continue  # skip private variables to reduce noise

            full_sig = f"{name}{signature}" if signature else None

            entries.append(RepoMapEntry(
                file=filepath,
                symbol_type=symbol_type,
                name=name,
                signature=full_sig,
                line=line,
            ))

        now = datetime.now(timezone.utc).isoformat()
        repo_map = RepoMap(
            entries=entries,
            repo_path=str(repo_path),
            generated_at=now,
        )

        # Truncate to max_tokens (approx 4 chars per token)
        max_chars = max_tokens * 4
        repo_map.to_prompt_string(max_chars=max_chars)

        return repo_map

    def _fallback_file_listing(
        self, repo_path: Path, ignore: list[str]
    ) -> RepoMap:
        """When ctags is not available: return a list of .py files with sizes."""
        entries: list[RepoMapEntry] = []
        repo_str = str(repo_path)

        try:
            for root, dirs, files in os.walk(repo_path):
                # Prune ignored directories in-place
                dirs[:] = [
                    d for d in dirs
                    if not any(fnmatch.fnmatch(d, pat) for pat in ignore)
                ]

                for fname in sorted(files):
                    if not fname.endswith(".py"):
                        continue
                    full = os.path.join(root, fname)
                    rel = full[len(repo_str):].lstrip("/")

                    if any(fnmatch.fnmatch(rel, pat) for pat in ignore):
                        continue
                    if any(fnmatch.fnmatch(fname, pat) for pat in ignore):
                        continue

                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0

                    entries.append(RepoMapEntry(
                        file=rel,
                        symbol_type="variable",
                        name=f"{fname} ({size} bytes)",
                        line=None,
                    ))
        except OSError:
            logger.warning("Failed to walk repo path %s", repo_path, exc_info=True)

        now = datetime.now(timezone.utc).isoformat()
        return RepoMap(
            entries=entries,
            repo_path=str(repo_path),
            generated_at=now,
        )

    @staticmethod
    def _should_ignore(filepath: str, patterns: list[str]) -> bool:
        """Check if *filepath* matches any ignore pattern."""
        parts = filepath.split("/")
        for pat in patterns:
            if fnmatch.fnmatch(filepath, pat):
                return True
            for part in parts:
                if fnmatch.fnmatch(part, pat):
                    return True
        return False

    @staticmethod
    def _map_kind(kind: str) -> str:
        """Map ctags kind to our symbol_type vocabulary."""
        mapping = {
            "class": "class",
            "function": "function",
            "method": "method",
            "member": "method",
            "variable": "variable",
            "import": "variable",
            "namespace": "variable",
            "module": "variable",
        }
        return mapping.get(kind, "variable")
