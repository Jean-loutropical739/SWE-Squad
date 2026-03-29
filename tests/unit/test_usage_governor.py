"""Tests for the AdaptiveUsageGovernor and base protocol."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.swe_team.providers.usage_governor.base import (
    ConcurrencyDecision,
    QuotaStatus,
    UsageGovernorProvider,
)
from src.swe_team.providers.usage_governor.adaptive import (
    AdaptiveUsageGovernor,
    _PRIORITY_RANK,
)


class TestQuotaStatus:
    def test_dataclass_fields(self):
        qs = QuotaStatus(
            total_tokens_used=100_000,
            quota_limit=500_000,
            remaining_pct=80.0,
            burn_rate_tokens_per_hour=5000.0,
            estimated_hours_until_exhaustion=80.0,
            current_period="2026-03-23",
        )
        assert qs.total_tokens_used == 100_000
        assert qs.remaining_pct == 80.0

    def test_exhaustion_none(self):
        qs = QuotaStatus(0, 500_000, 100.0, 0.0, None, "2026-03-23")
        assert qs.estimated_hours_until_exhaustion is None


class TestConcurrencyDecision:
    def test_dataclass_fields(self):
        cd = ConcurrencyDecision(
            max_parallel_agents=3,
            reason="test",
            priority_floor="medium",
            allow_new_work=True,
        )
        assert cd.max_parallel_agents == 3
        assert cd.allow_new_work is True


class TestProtocol:
    def test_adaptive_implements_protocol(self):
        gov = AdaptiveUsageGovernor()
        assert isinstance(gov, UsageGovernorProvider)


class TestAdaptiveGovernorNoTracker:
    """When no TokenTracker is attached, the governor must fail-closed."""

    def test_quota_status_no_tracker(self):
        gov = AdaptiveUsageGovernor(quota_limit=1_000_000)
        status = gov.get_quota_status()
        # _get_usage_data still returns 0,0 — but concurrency decision is fail-closed
        assert status.total_tokens_used == 0
        assert status.remaining_pct == 100.0
        assert status.estimated_hours_until_exhaustion is None

    def test_fail_closed_max_concurrency(self):
        """No tracker -> max 1 agent (fail-closed)."""
        gov = AdaptiveUsageGovernor(max_agents=5)
        assert gov.get_max_concurrency() == 1

    def test_fail_closed_allow_new_work_false(self):
        """No tracker -> allow_new_work must be False."""
        gov = AdaptiveUsageGovernor()
        decision = gov.get_concurrency_decision()
        assert decision.allow_new_work is False

    def test_fail_closed_priority_floor_critical(self):
        """No tracker -> priority floor is critical."""
        gov = AdaptiveUsageGovernor()
        decision = gov.get_concurrency_decision()
        assert decision.priority_floor == "critical"

    def test_fail_closed_blocks_all_launches(self):
        """No tracker -> should_launch_new_agent returns False for all priorities."""
        gov = AdaptiveUsageGovernor()
        assert gov.should_launch_new_agent("low") is False
        assert gov.should_launch_new_agent("medium") is False
        assert gov.should_launch_new_agent("high") is False
        assert gov.should_launch_new_agent("critical") is False

    def test_fail_closed_reason(self):
        """No tracker -> decision reason mentions fail-closed."""
        gov = AdaptiveUsageGovernor()
        decision = gov.get_concurrency_decision()
        assert "fail-closed" in decision.reason

    def test_fail_closed_logs_warning(self, caplog):
        """No tracker -> WARNING logged on init and on concurrency check."""
        import logging
        with caplog.at_level(logging.WARNING):
            gov = AdaptiveUsageGovernor()
            gov.get_concurrency_decision()
        assert any("no TokenTracker" in r.message for r in caplog.records)

    def test_health_check(self):
        gov = AdaptiveUsageGovernor()
        assert gov.health_check() is True

    def test_daily_summary_contains_tokens(self):
        gov = AdaptiveUsageGovernor(quota_limit=500_000)
        summary = gov.get_daily_summary()
        assert "500,000" in summary
        assert "Usage Governor" in summary

    def test_attaching_tracker_restores_normal_behavior(self):
        """After attaching a tracker, governor should no longer be fail-closed."""
        gov = AdaptiveUsageGovernor(quota_limit=100_000, max_agents=5)
        # Initially fail-closed
        assert gov.get_max_concurrency() == 1
        # Attach tracker with low usage -> should open up
        tracker = MagicMock()
        tracker.by_hour.return_value = [
            {"input_tokens": 5000, "output_tokens": 5000, "period": "2026-03-23T10"},
        ]
        gov.set_token_tracker(tracker)
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 5
        assert decision.allow_new_work is True


class TestAdaptiveGovernorWithTracker:
    @staticmethod
    def _make_tracker(hourly_data):
        tracker = MagicMock()
        tracker.by_hour.return_value = hourly_data
        return tracker

    def test_burn_rate_calculation(self):
        data = [
            {"input_tokens": 5000, "output_tokens": 5000, "period": "2026-03-23T10"},
            {"input_tokens": 5000, "output_tokens": 5000, "period": "2026-03-23T11"},
        ]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        status = gov.get_quota_status()
        assert status.total_tokens_used == 20_000
        assert status.burn_rate_tokens_per_hour == 10_000.0

    def test_tier_selection_high_remaining(self):
        # 80% remaining -> tier with remaining_pct=70 -> 5 agents
        data = [{"input_tokens": 10_000, "output_tokens": 10_000, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 5
        assert decision.priority_floor == "low"

    def test_tier_selection_low_remaining(self):
        # Use 95% of quota -> 5% remaining -> tier with remaining_pct=10
        data = [{"input_tokens": 47500, "output_tokens": 47500, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        status = gov.get_quota_status()
        assert status.remaining_pct == 5.0
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 1
        assert decision.allow_new_work is False

    def test_should_launch_blocked_by_priority_floor(self):
        # 5% remaining -> critical floor
        data = [{"input_tokens": 47500, "output_tokens": 47500, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        # allow_new_work is False, so even critical is blocked
        assert gov.should_launch_new_agent("critical") is False

    def test_should_launch_medium_blocked_when_floor_high(self):
        # 40% remaining -> tier with remaining_pct=30 -> priority_floor=high
        data = [{"input_tokens": 30000, "output_tokens": 30000, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        assert gov.should_launch_new_agent("medium") is False
        assert gov.should_launch_new_agent("high") is True

    def test_estimated_exhaustion(self):
        data = [{"input_tokens": 25000, "output_tokens": 25000, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        status = gov.get_quota_status()
        # 50k used, 50k remaining, burn rate 50k/h -> 1 hour
        assert status.estimated_hours_until_exhaustion == 1.0

    def test_minimum_1_agent(self):
        """Even with low multipliers, never go below 1 agent."""
        tracker = MagicMock()
        tracker.by_hour.return_value = []
        gov = AdaptiveUsageGovernor(max_agents=1, token_tracker=tracker)
        assert gov.get_max_concurrency() >= 1

    def test_zero_quota_limit(self):
        gov = AdaptiveUsageGovernor(quota_limit=0)
        status = gov.get_quota_status()
        assert status.remaining_pct == 0.0


class TestAlerts:
    @staticmethod
    def _make_tracker(hourly_data):
        tracker = MagicMock()
        tracker.by_hour.return_value = hourly_data
        return tracker

    def test_warning_alert(self):
        # 15% remaining -> warning
        data = [{"input_tokens": 42500, "output_tokens": 42500, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        alerts = gov.check_alerts()
        assert any("WARNING" in a for a in alerts)

    def test_critical_alert(self):
        # 5% remaining -> critical
        data = [{"input_tokens": 47500, "output_tokens": 47500, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
        )
        alerts = gov.check_alerts()
        assert any("CRITICAL" in a for a in alerts)

    def test_alert_throttling(self):
        data = [{"input_tokens": 47500, "output_tokens": 47500, "period": "2026-03-23T10"}]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=self._make_tracker(data),
            alert_throttle_minutes=60,
        )
        alerts1 = gov.check_alerts()
        alerts2 = gov.check_alerts()
        # Second call should be throttled
        assert len(alerts1) > 0
        assert len(alerts2) == 0


class TestConfig:
    def test_custom_tiers(self):
        tiers = [
            {"remaining_pct": 50, "max_agents": 10, "priority_floor": "low", "allow_new_work": True},
        ]
        tracker = MagicMock()
        tracker.by_hour.return_value = []
        gov = AdaptiveUsageGovernor(tiers=tiers, max_agents=10, token_tracker=tracker)
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 10

    def test_set_token_tracker(self):
        gov = AdaptiveUsageGovernor()
        tracker = MagicMock()
        tracker.by_hour.return_value = []
        gov.set_token_tracker(tracker)
        gov.get_quota_status()
        tracker.by_hour.assert_called_once()
