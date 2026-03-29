"""JSONL-based usage monitor — reads Claude Code's local JSONL conversation logs.

Globs ``~/.claude/projects/**/*.jsonl`` (and ``~/.config/claude/projects/``),
parses assistant messages with ``usage`` dicts, deduplicates, and calculates
estimated API-equivalent costs.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import UsageEntry
from .pricing import DEFAULT_PRICING, get_price, load_pricing


class JSONLUsageMonitor:
    """Concrete UsageMonitorProvider backed by Claude Code JSONL files."""

    def __init__(
        self,
        pricing_path: str | None = None,
        config_dir: str | None = None,
    ) -> None:
        self._pricing = load_pricing(pricing_path)
        self._config_dir = config_dir  # override for testing

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_usage(self, since_hours: int = 24) -> list[UsageEntry]:
        """Parse JSONL files and return usage entries from the last *since_hours*."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        entries: list[UsageEntry] = []
        seen: set[str] = set()

        for path in self._jsonl_paths():
            for entry in self._parse_file(path, cutoff):
                h = self._entry_hash(entry)
                if h not in seen:
                    seen.add(h)
                    entries.append(entry)

        entries.sort(key=lambda e: e.timestamp)
        return entries

    def aggregate_by(self, period: str, since_hours: int = 168) -> dict[str, dict[str, Any]]:
        """Group entries into buckets keyed by *period* (hour/day/week/month)."""
        entries = self.load_usage(since_hours=since_hours)
        buckets: dict[str, dict[str, Any]] = {}

        for e in entries:
            key = self._bucket_key(e.timestamp, period)
            if key not in buckets:
                buckets[key] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "count": 0,
                }
            b = buckets[key]
            b["input_tokens"] += e.input_tokens
            b["output_tokens"] += e.output_tokens
            b["cache_creation_tokens"] += e.cache_creation_tokens
            b["cache_read_tokens"] += e.cache_read_tokens
            b["cost_usd"] += e.cost_usd
            b["count"] += 1

        return buckets

    def total_cost(self, since_hours: int = 24) -> float:
        """Sum of estimated costs over the window."""
        return sum(e.cost_usd for e in self.load_usage(since_hours=since_hours))

    def health_check(self) -> bool:
        """True if at least one JSONL file is found."""
        return len(self._jsonl_paths()) > 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _jsonl_paths(self) -> list[Path]:
        """Discover all JSONL files under Claude config directories."""
        dirs: list[Path] = []

        if self._config_dir:
            dirs.append(Path(self._config_dir))
        else:
            env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            if env_dir:
                dirs.append(Path(env_dir) / "projects")
            else:
                home = Path.home()
                dirs.append(home / ".claude" / "projects")
                dirs.append(home / ".config" / "claude" / "projects")

        paths: list[Path] = []
        for d in dirs:
            if d.is_dir():
                paths.extend(d.glob("**/*.jsonl"))
        return paths

    def _parse_file(self, path: Path, cutoff: datetime) -> list[UsageEntry]:
        """Parse a single JSONL file, yielding entries newer than *cutoff*."""
        entries: list[UsageEntry] = []
        project = self._project_from_path(path)

        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    entry = self._extract_entry(obj, project)
                    if entry is None:
                        continue

                    # Filter by cutoff
                    try:
                        ts = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        continue

                    entries.append(entry)
        except OSError:
            pass

        return entries

    def _extract_entry(self, obj: dict, project: str) -> UsageEntry | None:
        """Extract a UsageEntry from a parsed JSONL object, or None."""
        if obj.get("type") != "assistant":
            return None

        msg = obj.get("message")
        if not isinstance(msg, dict):
            return None

        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None

        model = msg.get("model", "unknown")
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
        cache_read_tokens = usage.get("cache_read_input_tokens", 0)

        timestamp = obj.get("timestamp", "")
        session_id = obj.get("sessionId", "")

        cost = self._calculate_cost(
            model, input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens,
        )

        return UsageEntry(
            timestamp=timestamp,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost,
            session_id=session_id,
            project=project,
            metadata={
                k: obj[k]
                for k in ("requestId", "uuid", "version", "gitBranch", "cwd")
                if k in obj
            },
        )

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> float:
        """Estimate cost in USD."""
        p = self._pricing
        cost = (
            input_tokens * get_price(model, "input", p)
            + output_tokens * get_price(model, "output", p)
            + cache_creation_tokens * get_price(model, "cache_write", p)
            + cache_read_tokens * get_price(model, "cache_read", p)
        ) / 1_000_000
        return round(cost, 8)

    @staticmethod
    def _project_from_path(path: Path) -> str:
        """Derive a human-readable project name from the JSONL path."""
        # Path like ~/.claude/projects/-home-user-myproject/session.jsonl
        parent = path.parent.name
        # Convert dashes back to path separators for readability
        if parent.startswith("-"):
            return parent.lstrip("-").replace("-", "/")
        return parent

    @staticmethod
    def _entry_hash(entry: UsageEntry) -> str:
        """Deduplicate via a hash of timestamp + session + tokens."""
        raw = (
            f"{entry.timestamp}:{entry.session_id}:{entry.model}"
            f":{entry.input_tokens}:{entry.output_tokens}"
            f":{entry.cache_creation_tokens}:{entry.cache_read_tokens}"
        )
        return hashlib.md5(raw.encode()).hexdigest()

    @staticmethod
    def _bucket_key(timestamp: str, period: str) -> str:
        """Return a grouping key for the given period."""
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return "unknown"

        if period == "hour":
            return dt.strftime("%Y-%m-%d %H:00")
        elif period == "day":
            return dt.strftime("%Y-%m-%d")
        elif period == "week":
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        elif period == "month":
            return dt.strftime("%Y-%m")
        else:
            return dt.strftime("%Y-%m-%d")
