"""Time-window-based scheduling for usage governance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class TimeWindow:
    """A named time window with a concurrency multiplier."""

    name: str
    concurrency_multiplier: float
    days: list[str]
    start_hour: int
    end_hour: int  # use 24 for "all day"; overnight windows have start > end

    def matches(self, dt: datetime) -> bool:
        """Return True if the given datetime falls within this window."""
        day_name = dt.strftime("%a").lower()
        if day_name not in self.days:
            return False
        hour = dt.hour
        if self.start_hour <= self.end_hour:
            # Normal window (e.g. 9-18) or all-day (0-24)
            return self.start_hour <= hour < self.end_hour
        else:
            # Overnight window (e.g. 18-9): matches if hour >= start OR hour < end
            return hour >= self.start_hour or hour < self.end_hour


class UsageScheduler:
    """Time-window-based concurrency scheduler."""

    def __init__(
        self,
        timezone_name: str = "UTC",
        windows: list[TimeWindow] | None = None,
    ) -> None:
        self._tz = ZoneInfo(timezone_name)
        self._windows = windows or self._default_windows()

    @staticmethod
    def _default_windows() -> list[TimeWindow]:
        return [
            TimeWindow(
                name="weekday_peak",
                concurrency_multiplier=0.6,
                days=["mon", "tue", "wed", "thu", "fri"],
                start_hour=9,
                end_hour=18,
            ),
            TimeWindow(
                name="weekday_offpeak",
                concurrency_multiplier=1.0,
                days=["mon", "tue", "wed", "thu", "fri"],
                start_hour=18,
                end_hour=9,
            ),
            TimeWindow(
                name="weekend",
                concurrency_multiplier=1.5,
                days=["sat", "sun"],
                start_hour=0,
                end_hour=24,
            ),
        ]

    def _now(self) -> datetime:
        """Current time in the configured timezone."""
        return datetime.now(self._tz)

    def get_current_window(self) -> TimeWindow:
        """Return the first matching time window, or a default 1.0x window."""
        now = self._now()
        for window in self._windows:
            if window.matches(now):
                return window
        # Fallback — should not happen with default windows
        return TimeWindow(
            name="default",
            concurrency_multiplier=1.0,
            days=[],
            start_hour=0,
            end_hour=24,
        )

    def get_concurrency_multiplier(self) -> float:
        """Return the concurrency multiplier for the current time window."""
        return self.get_current_window().concurrency_multiplier

    def is_peak_hours(self) -> bool:
        """Return True if currently in a peak-hours window."""
        window = self.get_current_window()
        return "peak" in window.name and "off" not in window.name

    def is_weekend(self) -> bool:
        """Return True if today is Saturday or Sunday."""
        now = self._now()
        return now.weekday() >= 5
