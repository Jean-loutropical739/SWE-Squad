"""
LokiProvider — log query provider backed by Grafana Loki.

Queries Loki via its HTTP API using only stdlib (urllib.request).
Graceful: if Loki is unreachable, logs a warning and returns empty results.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from src.swe_team.providers.log_query.base import LogEntry

logger = logging.getLogger(__name__)


class LokiProvider:
    """Log query provider that queries Grafana Loki via HTTP API.

    Config keys (passed via constructor):
        loki_url: Base URL of the Loki instance (e.g. http://localhost:3100).
        timeout_seconds: HTTP request timeout in seconds (default: 10).
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._loki_url: str = config.get("loki_url", "http://localhost:3100").rstrip("/")
        self._timeout: int = int(config.get("timeout_seconds", 10))

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
        query = self._build_query(service=service, level=level)
        return self._execute_query(query, since_minutes=since_minutes, limit=limit)

    def search_logs(
        self,
        pattern: str,
        service: Optional[str] = None,
        since_minutes: int = 60,
    ) -> List[LogEntry]:
        query = self._build_query(service=service, pattern=pattern)
        return self._execute_query(query, since_minutes=since_minutes, limit=500)

    def health_check(self) -> bool:
        """Return True if Loki is reachable and ready."""
        try:
            url = f"{self._loki_url}/loki/api/v1/ready"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status == 200
        except Exception:
            logger.warning("Loki health check failed at %s", self._loki_url, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # LogQL query building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_query(
        service: Optional[str] = None,
        level: Optional[str] = None,
        pattern: Optional[str] = None,
    ) -> str:
        """Build a LogQL query from filter parameters.

        Examples:
            _build_query() -> '{job=~".+"}'
            _build_query(service="api") -> '{service="api"}'
            _build_query(service="api", level="ERROR") -> '{service="api"} |= `ERROR`'
            _build_query(pattern="timeout") -> '{job=~".+"} |~ `timeout`'
        """
        # Stream selector
        if service:
            selector = f'{{service="{service}"}}'
        else:
            selector = '{job=~".+"}'

        # Pipeline stages
        stages: List[str] = []
        if level:
            stages.append(f"|= `{level.upper()}`")
        if pattern:
            stages.append(f"|~ `{pattern}`")

        if stages:
            return selector + " " + " ".join(stages)
        return selector

    # ------------------------------------------------------------------
    # HTTP execution and response parsing
    # ------------------------------------------------------------------

    def _execute_query(
        self,
        query: str,
        since_minutes: int,
        limit: int,
    ) -> List[LogEntry]:
        """Execute a LogQL query_range request and return parsed entries."""
        now_ns = int(time.time() * 1e9)
        start_ns = now_ns - since_minutes * 60 * int(1e9)

        params = urllib.parse.urlencode({
            "query": query,
            "start": str(start_ns),
            "end": str(now_ns),
            "limit": str(limit),
            "direction": "backward",
        })
        url = f"{self._loki_url}/loki/api/v1/query_range?{params}"

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            logger.warning("Loki query failed: %s", query, exc_info=True)
            return []

        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: Dict[str, Any]) -> List[LogEntry]:
        """Parse Loki JSON response into LogEntry objects.

        Loki returns:
        {
          "data": {
            "result": [
              {
                "stream": {"service": "api", "level": "error", ...},
                "values": [["<nanosecond_ts>", "<log line>"], ...]
              }
            ]
          }
        }
        """
        entries: List[LogEntry] = []
        results = data.get("data", {}).get("result", [])

        for stream in results:
            labels = stream.get("stream", {})
            source = labels.get("service", labels.get("job", "unknown"))
            stream_level = labels.get("level", "").upper()

            for value in stream.get("values", []):
                if len(value) < 2:
                    continue
                ts_nano, line = value[0], value[1]

                # Convert nanosecond timestamp to ISO format
                try:
                    ts_sec = int(ts_nano) / 1e9
                    from datetime import datetime, timezone
                    timestamp = datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    timestamp = ts_nano

                # Try to extract level from the line if not in labels
                level = stream_level or _extract_level(line)

                entries.append(LogEntry(
                    timestamp=timestamp,
                    level=level,
                    message=line,
                    source=source,
                    metadata=labels,
                ))

        return entries


def _extract_level(line: str) -> str:
    """Best-effort level extraction from a log line."""
    upper = line.upper()
    for lvl in ("CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if lvl in upper:
            return lvl if lvl != "WARN" else "WARNING"
    return "INFO"
