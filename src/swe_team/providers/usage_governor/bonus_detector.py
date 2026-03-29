"""Bonus window detection — detects elevated API throughput periods."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BonusWindow:
    """Detected bonus throughput window."""

    detected_at: datetime
    multiplier: float
    confidence: float
    tokens_per_hour_actual: float
    tokens_per_hour_expected: float


class BonusDetector:
    """Detects bonus API throughput windows by comparing recent vs average usage."""

    def __init__(
        self,
        throughput_multiplier_threshold: float = 1.5,
        min_sustained_minutes: int = 15,
        log_path: Path | None = None,
    ) -> None:
        self._threshold = throughput_multiplier_threshold
        self._min_sustained_minutes = min_sustained_minutes
        self._log_path = log_path or Path("data/swe_team/bonus_detections.jsonl")
        self._current_bonus: BonusWindow | None = None
        self._bonus_start_time: float | None = None

    def detect(self, recent_hourly_usage: list[dict]) -> BonusWindow | None:
        """Detect a bonus window from hourly usage data.

        Parameters
        ----------
        recent_hourly_usage : list[dict]
            Output from TokenTracker.by_hour() — list of dicts with
            ``input_tokens``, ``output_tokens``, ``period`` keys.

        Returns
        -------
        BonusWindow | None
            Detected bonus window, or None if throughput is normal.
        """
        if not recent_hourly_usage or len(recent_hourly_usage) < 2:
            self._reset()
            return None

        # Recent 1h = last entry; 24h rolling average = all entries
        last_entry = recent_hourly_usage[-1]
        recent_tokens = last_entry.get("input_tokens", 0) + last_entry.get("output_tokens", 0)

        all_tokens = [
            e.get("input_tokens", 0) + e.get("output_tokens", 0)
            for e in recent_hourly_usage
        ]
        avg_tokens = sum(all_tokens) / len(all_tokens) if all_tokens else 0

        if avg_tokens <= 0:
            self._reset()
            return None

        ratio = recent_tokens / avg_tokens

        if ratio < self._threshold:
            self._reset()
            return None

        # Sustained check
        now = time.monotonic()
        if self._bonus_start_time is None:
            self._bonus_start_time = now

        elapsed_minutes = (now - self._bonus_start_time) / 60
        if elapsed_minutes < self._min_sustained_minutes:
            # Not sustained long enough yet — don't declare bonus
            return None

        # Determine multiplier and confidence
        if ratio >= 2.5:
            multiplier = 5.0
            confidence = min(1.0, (ratio - 2.5) / 2.5 + 0.7)
        elif ratio >= 1.5:
            multiplier = 2.0
            confidence = min(1.0, (ratio - 1.5) / 1.0 + 0.5)
        else:
            self._reset()
            return None

        bonus = BonusWindow(
            detected_at=datetime.now(timezone.utc),
            multiplier=multiplier,
            confidence=round(confidence, 2),
            tokens_per_hour_actual=float(recent_tokens),
            tokens_per_hour_expected=round(avg_tokens, 2),
        )
        self._current_bonus = bonus
        self._log_detection(bonus)
        return bonus

    def is_bonus_active(self) -> bool:
        """Return True if a bonus window is currently detected."""
        return self._current_bonus is not None

    def get_multiplier(self) -> float:
        """Return the current bonus multiplier (1.0 if no bonus)."""
        if self._current_bonus is not None:
            return self._current_bonus.multiplier
        return 1.0

    def _reset(self) -> None:
        self._current_bonus = None
        self._bonus_start_time = None

    def _log_detection(self, bonus: BonusWindow) -> None:
        """Append detection to JSONL log for pattern learning."""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "detected_at": bonus.detected_at.isoformat(),
                "multiplier": bonus.multiplier,
                "confidence": bonus.confidence,
                "tokens_per_hour_actual": bonus.tokens_per_hour_actual,
                "tokens_per_hour_expected": bonus.tokens_per_hour_expected,
            }
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.warning("Failed to log bonus detection", exc_info=True)
