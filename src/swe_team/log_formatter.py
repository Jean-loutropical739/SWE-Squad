"""Structured logging formatters for SWE-Squad agents.

Provides a JSON formatter for machine-readable log output and a text
formatter that preserves the original ``[LEVEL] message`` style.  The
active formatter is selected via config key ``logging.format`` in
``swe_team.yaml`` or the ``SWE_LOG_FORMAT`` environment variable.

Default is ``"text"`` — zero behaviour change unless opted in.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "timestamp": ts,
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
            "ticket_id": getattr(record, "ticket_id", ""),
            "agent": getattr(record, "agent", ""),
        }
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Preserve the existing ``%(asctime)s [%(levelname)s] %(name)s: %(message)s`` format."""

    _FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT)


def get_formatter(format: str = "text") -> logging.Formatter:
    """Return the appropriate formatter for *format* (``"json"`` or ``"text"``)."""
    if format == "json":
        return JsonFormatter()
    return TextFormatter()


def resolve_log_format(config: Optional[dict] = None) -> str:
    """Determine the log format from env var or config dict.

    Priority:
      1. ``SWE_LOG_FORMAT`` environment variable
      2. ``config["logging"]["format"]`` from *config* dict
      3. ``"text"`` (default)
    """
    env = os.environ.get("SWE_LOG_FORMAT", "").strip().lower()
    if env in ("json", "text"):
        return env

    if config is not None:
        try:
            val = config["logging"]["format"]
            if isinstance(val, str) and val.strip().lower() in ("json", "text"):
                return val.strip().lower()
        except (KeyError, TypeError):
            pass

    return "text"
