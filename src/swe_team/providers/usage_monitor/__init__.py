"""UsageMonitorProvider — pluggable token usage tracking and cost estimation."""

from __future__ import annotations

from .base import UsageEntry, UsageMonitorProvider
from .jsonl import JSONLUsageMonitor

__all__ = ["UsageEntry", "UsageMonitorProvider", "JSONLUsageMonitor", "create_provider"]


def create_provider(config: dict | None = None) -> JSONLUsageMonitor:
    """Factory: build the default JSONL-based usage monitor.

    Parameters
    ----------
    config : dict, optional
        Keys: ``pricing_path`` (str | None), ``config_dir`` (str | None).
    """
    config = config or {}
    return JSONLUsageMonitor(
        pricing_path=config.get("pricing_path"),
        config_dir=config.get("config_dir"),
    )
