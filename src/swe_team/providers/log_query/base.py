"""
LogQueryProvider interface — pluggable log querying and search.

Implement this to add support for any log backend
(local files, Elasticsearch, Loki, CloudWatch, etc.)
without touching core agent code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class LogEntry:
    """A single parsed log entry."""

    timestamp: str
    level: str
    message: str
    source: str  # filename or service name
    metadata: dict = field(default_factory=dict)  # extra fields


@runtime_checkable
class LogQueryProvider(Protocol):
    """
    Interface all log query providers must implement.

    Providers are registered in config/swe_team.yaml under providers.log_query.
    The active provider is loaded by name — no core code changes required
    when switching backends.
    """

    def query_logs(
        self,
        service: Optional[str] = None,
        level: Optional[str] = None,
        since_minutes: int = 60,
        limit: int = 500,
    ) -> List[LogEntry]:
        """Query log entries filtered by service and/or level.

        Args:
            service: Filter to logs from this service/source (None = all).
            level: Filter to this log level, e.g. "ERROR" (None = all).
            since_minutes: Only return entries from the last N minutes.
            limit: Maximum number of entries to return.

        Returns:
            List of matching LogEntry objects, newest first.
        """
        ...

    def search_logs(
        self,
        pattern: str,
        service: Optional[str] = None,
        since_minutes: int = 60,
    ) -> List[LogEntry]:
        """Search log entries by regex or substring pattern.

        Args:
            pattern: Regex pattern to match against log messages.
            service: Filter to logs from this service/source (None = all).
            since_minutes: Only return entries from the last N minutes.

        Returns:
            List of matching LogEntry objects, newest first.
        """
        ...

    def health_check(self) -> bool:
        """Return True if the log backend is reachable and properly configured."""
        ...
