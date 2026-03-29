"""Log query providers — pluggable log querying and search."""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider


def create_log_query_provider(config: Optional[Dict[str, Any]] = None) -> Optional[LogQueryProvider]:
    """Factory: create a LogQueryProvider from config.

    Config is expected to come from swe_team.yaml under providers.log_query.
    Returns None if no provider is configured (plugin-based: system works without one).

    Supported provider values:
        "local" — LocalFileProvider (default if log_directories are set)

    Example config::

        providers:
          log_query:
            provider: local
            log_directories:
              - logs/
              - logs/remote/
            remote_collection: false
            file_pattern: "*.log"
    """
    if config is None:
        return None

    provider_name = config.get("provider", "local")

    if provider_name == "local":
        from src.swe_team.providers.log_query.local import LocalFileProvider

        return LocalFileProvider(config)

    if provider_name == "loki":
        from src.swe_team.providers.log_query.loki import LokiProvider

        return LokiProvider(config)

    raise ValueError(f"Unknown log_query provider: {provider_name!r}")


__all__ = ["LogEntry", "LogQueryProvider", "create_log_query_provider"]
