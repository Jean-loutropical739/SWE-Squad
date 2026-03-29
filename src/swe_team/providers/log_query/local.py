"""
LocalFileProvider — log query provider backed by local log files.

Scans local log directories and optionally uses remote_logs.py to pull
logs from remote workers via SSH.  Parses both plain-text and JSON log
formats into LogEntry objects.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider

logger = logging.getLogger(__name__)

# Regex for common text log formats:
#   2024-01-15 12:34:56,789 [ERROR] message
#   2024-01-15T12:34:56 ERROR message
#   [ERROR] message
#   ERROR: message
_TIMESTAMP_LEVEL_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[,.\d]*)\s+"
    r"(?:\[?(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]?\s+)"
    r"(?P<message>.+)$"
)
_BRACKET_LEVEL_RE = re.compile(
    r"^\[(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]\s+(?P<message>.+)$"
)
_COLON_LEVEL_RE = re.compile(
    r"^(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL):\s+(?P<message>.+)$"
)


class LocalFileProvider:
    """Log query provider that reads from local log files on disk.

    Config keys (passed via constructor):
        log_directories: list of directory paths to scan for *.log files.
        remote_collection: if True, also run collect_remote_logs() on query.
        remote_local_dir: local directory for rsync'd remote logs.
        file_pattern: glob pattern for log files (default: "*.log").
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._log_directories: List[str] = config.get("log_directories", [])
        self._remote_collection: bool = config.get("remote_collection", False)
        self._remote_local_dir: str = config.get("remote_local_dir", "logs/remote")
        self._file_pattern: str = config.get("file_pattern", "*.log")

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def query_logs(
        self,
        service: Optional[str] = None,
        level: Optional[str] = None,
        since_minutes: int = 60,
        limit: int = 500,
    ) -> List[LogEntry]:
        self._maybe_collect_remote()
        entries = self._read_all_entries(since_minutes)

        if service:
            entries = [e for e in entries if service.lower() in e.source.lower()]
        if level:
            level_upper = level.upper()
            entries = [e for e in entries if e.level.upper() == level_upper]

        # Sort newest first by timestamp string (ISO-sortable)
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def search_logs(
        self,
        pattern: str,
        service: Optional[str] = None,
        since_minutes: int = 60,
    ) -> List[LogEntry]:
        self._maybe_collect_remote()
        entries = self._read_all_entries(since_minutes)

        if service:
            entries = [e for e in entries if service.lower() in e.source.lower()]

        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Fall back to literal substring match
            compiled = None

        matched: List[LogEntry] = []
        for entry in entries:
            if compiled is not None:
                if compiled.search(entry.message):
                    matched.append(entry)
            elif pattern.lower() in entry.message.lower():
                matched.append(entry)

        matched.sort(key=lambda e: e.timestamp, reverse=True)
        return matched

    def health_check(self) -> bool:
        """Return True if at least one configured log directory exists."""
        for d in self._log_directories:
            if Path(d).is_dir():
                return True
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_collect_remote(self) -> None:
        """If remote collection is enabled, pull remote logs."""
        if not self._remote_collection:
            return
        try:
            from src.swe_team.remote_logs import collect_remote_logs

            extra_dirs = collect_remote_logs(local_dir=self._remote_local_dir)
            # Add any newly collected dirs to our scan list (deduplicated)
            existing = set(self._log_directories)
            for d in extra_dirs:
                if d not in existing:
                    self._log_directories.append(d)
        except Exception:
            logger.debug("Remote log collection skipped", exc_info=True)

    def _read_all_entries(self, since_minutes: int) -> List[LogEntry]:
        """Read and parse log entries from all configured directories."""
        cutoff = time.time() - since_minutes * 60
        entries: List[LogEntry] = []

        for dir_path in self._log_directories:
            p = Path(dir_path)
            if not p.is_dir():
                continue
            for log_file in p.glob(self._file_pattern):
                if not log_file.is_file():
                    continue
                # Skip files not modified within the time window
                try:
                    if log_file.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                entries.extend(self._parse_file(log_file))

        return entries

    def _parse_file(self, path: Path) -> List[LogEntry]:
        """Parse a single log file into LogEntry objects."""
        entries: List[LogEntry] = []
        source = path.stem  # filename without extension

        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            logger.debug("Cannot read %s", path, exc_info=True)
            return entries

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            entry = self._parse_line(stripped, source)
            if entry is not None:
                entries.append(entry)

        return entries

    @staticmethod
    def _parse_line(line: str, source: str) -> Optional[LogEntry]:
        """Parse a single log line (text or JSON) into a LogEntry."""
        # Try JSON format first
        if line.startswith("{"):
            try:
                data = json.loads(line)
                return LogEntry(
                    timestamp=str(data.get("timestamp", data.get("time", ""))),
                    level=str(data.get("level", data.get("severity", "INFO"))).upper(),
                    message=str(data.get("message", data.get("msg", ""))),
                    source=str(data.get("source", data.get("service", source))),
                    metadata={
                        k: v
                        for k, v in data.items()
                        if k not in ("timestamp", "time", "level", "severity", "message", "msg", "source", "service")
                    },
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # Try timestamp + level format
        m = _TIMESTAMP_LEVEL_RE.match(line)
        if m:
            return LogEntry(
                timestamp=m.group("timestamp"),
                level=m.group("level").upper(),
                message=m.group("message"),
                source=source,
            )

        # Try [LEVEL] message format
        m = _BRACKET_LEVEL_RE.match(line)
        if m:
            return LogEntry(
                timestamp="",
                level=m.group("level").upper(),
                message=m.group("message"),
                source=source,
            )

        # Try LEVEL: message format
        m = _COLON_LEVEL_RE.match(line)
        if m:
            return LogEntry(
                timestamp="",
                level=m.group("level").upper(),
                message=m.group("message"),
                source=source,
            )

        # Unparseable line — store as INFO with no timestamp
        return LogEntry(
            timestamp="",
            level="INFO",
            message=line,
            source=source,
        )
