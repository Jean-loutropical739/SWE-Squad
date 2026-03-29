"""UsageGovernorProvider — adaptive token usage governance and concurrency control."""

from __future__ import annotations

from .base import QuotaStatus, ConcurrencyDecision, UsageGovernorProvider
from .adaptive import AdaptiveUsageGovernor
from .schedule import UsageScheduler, TimeWindow
from .bonus_detector import BonusDetector, BonusWindow
from .rules import GovernanceRule, RuleEngine, RuleResult

__all__ = [
    "QuotaStatus",
    "ConcurrencyDecision",
    "UsageGovernorProvider",
    "AdaptiveUsageGovernor",
    "UsageScheduler",
    "TimeWindow",
    "BonusDetector",
    "BonusWindow",
    "GovernanceRule",
    "RuleEngine",
    "RuleResult",
    "create_usage_governor",
]


def create_usage_governor(config: dict) -> AdaptiveUsageGovernor:
    """Factory: build an AdaptiveUsageGovernor from config dict.

    Parameters
    ----------
    config : dict
        The ``usage_governor`` section from swe_team.yaml.
    """
    scheduler = None
    bonus_detector = None

    schedule_cfg = config.get("schedule")
    if schedule_cfg:
        windows = []
        for w in schedule_cfg.get("windows", []):
            hours = w.get("hours", [0, 24])
            windows.append(TimeWindow(
                name=w["name"],
                concurrency_multiplier=w.get("concurrency_multiplier", 1.0),
                days=w.get("days", []),
                start_hour=hours[0],
                end_hour=hours[1],
            ))
        scheduler = UsageScheduler(
            timezone_name=schedule_cfg.get("timezone", "UTC"),
            windows=windows,
        )

    bonus_cfg = config.get("bonus_detection")
    if bonus_cfg and bonus_cfg.get("enabled", False):
        bonus_detector = BonusDetector(
            throughput_multiplier_threshold=bonus_cfg.get("throughput_multiplier_threshold", 1.5),
        )

    alerts_cfg = config.get("alerts", {})
    quota_cfg = config.get("quota", {})
    concurrency_cfg = config.get("concurrency", {})

    # Hierarchical governance: operator overrides and hard limits
    operator_overrides = config.get("operator_overrides", [])
    hard_limits = config.get("hard_limits", {})

    return AdaptiveUsageGovernor(
        quota_limit=quota_cfg.get("tokens_per_day", 2_400_000),
        tokens_per_5h_block=quota_cfg.get("tokens_per_5h_block", 500_000),
        max_agents=concurrency_cfg.get("max_agents", 5),
        tiers=concurrency_cfg.get("tiers", []),
        scheduler=scheduler,
        bonus_detector=bonus_detector,
        quota_warning_pct=alerts_cfg.get("quota_warning_pct", 20),
        quota_critical_pct=alerts_cfg.get("quota_critical_pct", 10),
        burn_rate_spike_multiplier=alerts_cfg.get("burn_rate_spike_multiplier", 2.0),
        alert_throttle_minutes=alerts_cfg.get("throttle_minutes", 60),
        operator_overrides=operator_overrides,
        hard_limits=hard_limits,
    )
