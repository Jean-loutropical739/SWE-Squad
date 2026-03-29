"""Tests for UsageScheduler and TimeWindow."""

from __future__ import annotations

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.swe_team.providers.usage_governor.schedule import TimeWindow, UsageScheduler


class TestTimeWindow:
    def test_normal_window_match(self):
        w = TimeWindow("peak", 0.6, ["mon"], start_hour=9, end_hour=18)
        # Monday 10:00
        dt = datetime(2026, 3, 23, 10, 0, tzinfo=ZoneInfo("UTC"))  # Monday
        assert w.matches(dt) is True

    def test_normal_window_no_match_wrong_hour(self):
        w = TimeWindow("peak", 0.6, ["mon"], start_hour=9, end_hour=18)
        dt = datetime(2026, 3, 23, 20, 0, tzinfo=ZoneInfo("UTC"))
        assert w.matches(dt) is False

    def test_normal_window_no_match_wrong_day(self):
        w = TimeWindow("peak", 0.6, ["tue"], start_hour=9, end_hour=18)
        dt = datetime(2026, 3, 23, 10, 0, tzinfo=ZoneInfo("UTC"))  # Monday
        assert w.matches(dt) is False

    def test_overnight_window_late_night(self):
        w = TimeWindow("offpeak", 1.0, ["mon"], start_hour=18, end_hour=9)
        dt = datetime(2026, 3, 23, 22, 0, tzinfo=ZoneInfo("UTC"))  # Monday 22:00
        assert w.matches(dt) is True

    def test_overnight_window_early_morning(self):
        w = TimeWindow("offpeak", 1.0, ["mon"], start_hour=18, end_hour=9)
        dt = datetime(2026, 3, 23, 3, 0, tzinfo=ZoneInfo("UTC"))  # Monday 03:00
        assert w.matches(dt) is True

    def test_overnight_window_midday_no_match(self):
        w = TimeWindow("offpeak", 1.0, ["mon"], start_hour=18, end_hour=9)
        dt = datetime(2026, 3, 23, 12, 0, tzinfo=ZoneInfo("UTC"))  # Monday 12:00
        assert w.matches(dt) is False

    def test_all_day_window(self):
        w = TimeWindow("weekend", 1.5, ["sat", "sun"], start_hour=0, end_hour=24)
        dt = datetime(2026, 3, 28, 14, 0, tzinfo=ZoneInfo("UTC"))  # Saturday
        assert w.matches(dt) is True

    def test_all_day_window_hour_zero(self):
        w = TimeWindow("weekend", 1.5, ["sat"], start_hour=0, end_hour=24)
        dt = datetime(2026, 3, 28, 0, 0, tzinfo=ZoneInfo("UTC"))  # Saturday 00:00
        assert w.matches(dt) is True

    def test_all_day_window_hour_23(self):
        w = TimeWindow("weekend", 1.5, ["sat"], start_hour=0, end_hour=24)
        dt = datetime(2026, 3, 28, 23, 0, tzinfo=ZoneInfo("UTC"))
        assert w.matches(dt) is True

    def test_boundary_start_inclusive(self):
        w = TimeWindow("peak", 0.6, ["mon"], start_hour=9, end_hour=18)
        dt = datetime(2026, 3, 23, 9, 0, tzinfo=ZoneInfo("UTC"))
        assert w.matches(dt) is True

    def test_boundary_end_exclusive(self):
        w = TimeWindow("peak", 0.6, ["mon"], start_hour=9, end_hour=18)
        dt = datetime(2026, 3, 23, 18, 0, tzinfo=ZoneInfo("UTC"))
        assert w.matches(dt) is False


class TestUsageScheduler:
    def test_default_windows(self):
        s = UsageScheduler(timezone_name="UTC")
        window = s.get_current_window()
        assert window.name in ("weekday_peak", "weekday_offpeak", "weekend", "default")

    def test_multiplier_returns_float(self):
        s = UsageScheduler(timezone_name="UTC")
        m = s.get_concurrency_multiplier()
        assert isinstance(m, float)
        assert m > 0

    def test_is_peak_hours(self):
        s = UsageScheduler(timezone_name="UTC")
        # Just check it returns a bool
        assert isinstance(s.is_peak_hours(), bool)

    def test_is_weekend(self):
        s = UsageScheduler(timezone_name="UTC")
        assert isinstance(s.is_weekend(), bool)

    def test_timezone_handling(self):
        s = UsageScheduler(timezone_name="America/Toronto")
        window = s.get_current_window()
        assert window is not None

    def test_custom_windows(self):
        windows = [
            TimeWindow("always", 2.0, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                       start_hour=0, end_hour=24),
        ]
        s = UsageScheduler(timezone_name="UTC", windows=windows)
        assert s.get_concurrency_multiplier() == 2.0

    def test_fallback_default_window(self):
        # No windows match -> fallback
        windows = [
            TimeWindow("never", 0.5, [], start_hour=0, end_hour=24),
        ]
        s = UsageScheduler(timezone_name="UTC", windows=windows)
        window = s.get_current_window()
        assert window.name == "default"
        assert window.concurrency_multiplier == 1.0
