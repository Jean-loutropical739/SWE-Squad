"""Protocol and dataclasses for the UsageGovernor provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class QuotaStatus:
    """Current quota usage status."""

    total_tokens_used: int
    quota_limit: int
    remaining_pct: float
    burn_rate_tokens_per_hour: float
    estimated_hours_until_exhaustion: float | None
    current_period: str


@dataclass
class ConcurrencyDecision:
    """Decision about how many agents can run concurrently."""

    max_parallel_agents: int
    reason: str
    priority_floor: str  # "low", "medium", "high", "critical"
    allow_new_work: bool
    applied_rules: list[str] = field(default_factory=list)  # rule names
    audit_trail: str = ""


@runtime_checkable
class UsageGovernorProvider(Protocol):
    """Interface for usage governance and concurrency control."""

    def get_quota_status(self) -> QuotaStatus: ...

    def get_max_concurrency(self) -> int: ...

    def should_launch_new_agent(self, priority: str) -> bool: ...

    def get_concurrency_decision(self) -> ConcurrencyDecision: ...

    def get_daily_summary(self) -> str: ...

    def health_check(self) -> bool: ...
