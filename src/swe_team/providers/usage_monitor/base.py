"""UsageMonitorProvider interface — pluggable token usage tracking.

Implement this to swap between local JSONL parsing, API-based usage
endpoints, or any other usage data source without touching core code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class UsageEntry:
    """A single usage record from one API call."""

    timestamp: str  # ISO-8601
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float  # estimated API-equivalent cost
    session_id: str
    project: str  # derived from directory path
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class UsageMonitorProvider(Protocol):
    """Interface all usage-monitoring providers must implement."""

    def load_usage(self, since_hours: int = 24) -> list[UsageEntry]:
        """Return usage entries from the last *since_hours* hours."""
        ...

    def aggregate_by(self, period: str, since_hours: int = 168) -> dict:
        """Aggregate usage by *period* (``hour``, ``day``, ``week``, ``month``).

        Returns a dict keyed by period bucket string with values being dicts
        of ``{input_tokens, output_tokens, cache_creation_tokens,
        cache_read_tokens, cost_usd, count}``.
        """
        ...

    def total_cost(self, since_hours: int = 24) -> float:
        """Return total estimated cost in USD over the last *since_hours*."""
        ...

    def health_check(self) -> bool:
        """Return True if the usage data source is accessible."""
        ...
