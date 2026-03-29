"""Hierarchical governance rule engine with precedence-based composition."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class GovernanceRule:
    """A single governance rule with a multiplier and precedence level."""

    name: str
    description: str
    multiplier: float          # e.g., 2.0 for boost, 0.1 for reduce
    precedence: int            # 1=hard_limit, 2=operator_override, 3=bonus, 4=schedule, 5=base
    active: bool
    source: str                # "config", "detected", "base"

    # Optional schedule (for operator overrides)
    schedule_days: list[str] | None = None
    schedule_start_hour: int | None = None
    schedule_end_hour: int | None = None
    schedule_timezone: str | None = None

    def matches_time(self, now: datetime) -> bool:
        """Check if this rule matches the given datetime.

        Rules without schedule fields always match (if active).
        """
        if not self.active:
            return False

        # No schedule constraint — always matches
        if self.schedule_days is None:
            return True

        # Convert now to the rule's timezone
        if self.schedule_timezone:
            tz = ZoneInfo(self.schedule_timezone)
            now = now.astimezone(tz)

        day_name = now.strftime("%a").lower()
        if day_name not in self.schedule_days:
            return False

        hour = now.hour
        start = self.schedule_start_hour
        end = self.schedule_end_hour

        if start is None or end is None:
            return True

        # All-day: start==0 and end==24
        if start == 0 and end == 24:
            return True

        if start <= end:
            # Normal window (e.g. 9-18)
            return start <= hour < end
        else:
            # Overnight window (e.g. 18-9): matches if hour >= start OR hour < end
            return hour >= start or hour < end


@dataclass
class RuleResult:
    """Result of evaluating governance rules."""

    effective_agents: int
    base_agents: int
    combined_multiplier: float
    applied_rules: list[GovernanceRule]
    audit_trail: str


class RuleEngine:
    """Evaluates governance rules with precedence and multiplicative composition."""

    def __init__(self, rules: list[GovernanceRule], hard_limits: dict | None = None) -> None:
        self._rules = sorted(rules, key=lambda r: r.precedence)
        limits = hard_limits or {}
        self._max_absolute = limits.get("max_agents_absolute", 10)
        self._min_absolute = limits.get("min_agents_absolute", 1)

    def evaluate(self, base_agents: int, now: datetime | None = None) -> RuleResult:
        """Apply all active, currently-matching rules multiplicatively.

        Returns RuleResult with effective_agents, applied_rules, and audit trail.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        active_rules = self.get_active_rules(now)

        if not active_rules:
            effective = max(self._min_absolute, min(base_agents, self._max_absolute))
            return RuleResult(
                effective_agents=effective,
                base_agents=base_agents,
                combined_multiplier=1.0,
                applied_rules=[],
                audit_trail=f"effective={effective} | base={base_agents} | no rules | clamped=[{self._min_absolute},{self._max_absolute}]",
            )

        # Resolve conflicts at same precedence: group by precedence
        resolved = self._resolve_conflicts(active_rules)

        combined = 1.0
        for rule in resolved:
            combined *= rule.multiplier

        effective_raw = base_agents * combined
        effective = round(effective_raw)
        effective = max(self._min_absolute, min(effective, self._max_absolute))

        # Build audit trail
        rule_parts = " × ".join(
            f"{r.name}({r.multiplier}x,P{r.precedence})" for r in resolved
        )
        audit = (
            f"effective={effective} | base={base_agents} × {rule_parts} "
            f"| clamped=[{self._min_absolute},{self._max_absolute}]"
        )

        return RuleResult(
            effective_agents=effective,
            base_agents=base_agents,
            combined_multiplier=round(combined, 6),
            applied_rules=resolved,
            audit_trail=audit,
        )

    def get_active_rules(self, now: datetime | None = None) -> list[GovernanceRule]:
        """Return rules that are active and match the current time."""
        if now is None:
            now = datetime.now(timezone.utc)
        return [r for r in self._rules if r.matches_time(now)]

    @staticmethod
    def _resolve_conflicts(rules: list[GovernanceRule]) -> list[GovernanceRule]:
        """Resolve conflicts at same precedence level.

        If rules at the same precedence conflict (one >1.0, one <1.0),
        take the more conservative (lower) multiplier.
        """
        by_precedence: dict[int, list[GovernanceRule]] = {}
        for rule in rules:
            by_precedence.setdefault(rule.precedence, []).append(rule)

        resolved: list[GovernanceRule] = []
        for prec in sorted(by_precedence):
            group = by_precedence[prec]
            if len(group) == 1:
                resolved.append(group[0])
                continue

            # Check for conflict: mixed boost (>1) and reduce (<1)
            has_boost = any(r.multiplier > 1.0 for r in group)
            has_reduce = any(r.multiplier < 1.0 for r in group)

            if has_boost and has_reduce:
                # Conflict: take the most conservative (lowest multiplier)
                most_conservative = min(group, key=lambda r: r.multiplier)
                resolved.append(most_conservative)
            else:
                # No conflict: apply all multiplicatively
                resolved.extend(group)

        return sorted(resolved, key=lambda r: r.precedence)
