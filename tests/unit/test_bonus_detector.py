"""Tests for BonusDetector."""

from __future__ import annotations

import time
import pytest
from pathlib import Path
from unittest.mock import patch

from src.swe_team.providers.usage_governor.bonus_detector import BonusDetector, BonusWindow


class TestBonusDetection:
    def test_no_data(self):
        bd = BonusDetector()
        assert bd.detect([]) is None
        assert bd.is_bonus_active() is False
        assert bd.get_multiplier() == 1.0

    def test_single_entry(self):
        bd = BonusDetector()
        assert bd.detect([{"input_tokens": 1000, "output_tokens": 1000}]) is None

    def test_normal_throughput(self):
        """No bonus when throughput is near average."""
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        bd = BonusDetector()
        assert bd.detect(data) is None

    def test_2x_bonus_detection(self):
        """2x throughput triggers 2x bonus after sustained period."""
        # Average = 2000 tokens/hour, last hour = 4000 (2x)
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        data[-1] = {"input_tokens": 2000, "output_tokens": 2000, "period": "2026-03-23T09"}

        bd = BonusDetector(min_sustained_minutes=0)
        result = bd.detect(data)
        assert result is not None
        assert result.multiplier == 2.0
        assert result.confidence >= 0.5

    def test_5x_bonus_detection(self):
        """3x+ throughput triggers 5x bonus."""
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        data[-1] = {"input_tokens": 4000, "output_tokens": 4000, "period": "2026-03-23T09"}

        bd = BonusDetector(min_sustained_minutes=0)
        result = bd.detect(data)
        assert result is not None
        assert result.multiplier == 5.0

    def test_sustained_duration_requirement(self):
        """Bonus not declared until sustained for min_sustained_minutes."""
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        data[-1] = {"input_tokens": 2000, "output_tokens": 2000, "period": "2026-03-23T09"}

        bd = BonusDetector(min_sustained_minutes=15)
        # First call starts the timer
        result = bd.detect(data)
        assert result is None  # not sustained yet
        assert bd.is_bonus_active() is False

    def test_bonus_reset_on_normal(self):
        """Bonus resets when throughput returns to normal."""
        bd = BonusDetector(min_sustained_minutes=0)

        high_data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        high_data[-1] = {"input_tokens": 3000, "output_tokens": 3000, "period": "2026-03-23T09"}
        bd.detect(high_data)
        assert bd.is_bonus_active() is True

        # Normal data
        normal_data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        bd.detect(normal_data)
        assert bd.is_bonus_active() is False

    def test_confidence_range(self):
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        data[-1] = {"input_tokens": 2000, "output_tokens": 2000, "period": "2026-03-23T09"}

        bd = BonusDetector(min_sustained_minutes=0)
        result = bd.detect(data)
        assert 0.0 <= result.confidence <= 1.0

    def test_empty_tokens(self):
        """All-zero data should not crash."""
        data = [
            {"input_tokens": 0, "output_tokens": 0, "period": f"2026-03-23T{h:02d}"}
            for h in range(5)
        ]
        bd = BonusDetector()
        assert bd.detect(data) is None


class TestBonusLogging:
    def test_log_detection(self, tmp_path):
        log_file = tmp_path / "bonus.jsonl"
        data = [
            {"input_tokens": 1000, "output_tokens": 1000, "period": f"2026-03-23T{h:02d}"}
            for h in range(10)
        ]
        data[-1] = {"input_tokens": 3000, "output_tokens": 3000, "period": "2026-03-23T09"}

        bd = BonusDetector(min_sustained_minutes=0, log_path=log_file)
        bd.detect(data)
        assert log_file.exists()
        content = log_file.read_text()
        assert "multiplier" in content
