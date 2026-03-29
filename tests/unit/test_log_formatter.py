"""Tests for src.swe_team.log_formatter — structured JSON logging."""

from __future__ import annotations

import json
import logging
import os

import pytest

from src.swe_team.log_formatter import (
    JsonFormatter,
    TextFormatter,
    get_formatter,
    resolve_log_format,
)


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------

class TestJsonFormatter:
    def _make_record(self, msg: str = "hello", **kwargs):
        record = logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test_module.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in kwargs.items():
            setattr(record, k, v)
        return record

    def test_output_is_valid_json(self):
        fmt = JsonFormatter()
        output = fmt.format(self._make_record())
        data = json.loads(output)
        assert isinstance(data, dict)

    def test_required_fields_present(self):
        fmt = JsonFormatter()
        record = self._make_record("test msg", ticket_id="T-42", agent="developer")
        data = json.loads(fmt.format(record))
        assert data["level"] == "INFO"
        assert data["message"] == "test msg"
        assert data["ticket_id"] == "T-42"
        assert data["agent"] == "developer"
        assert "timestamp" in data
        assert "module" in data

    def test_missing_extras_default_empty(self):
        fmt = JsonFormatter()
        data = json.loads(fmt.format(self._make_record()))
        assert data["ticket_id"] == ""
        assert data["agent"] == ""

    def test_single_line(self):
        fmt = JsonFormatter()
        output = fmt.format(self._make_record("multi\nline"))
        assert "\n" not in output


# ---------------------------------------------------------------------------
# TextFormatter
# ---------------------------------------------------------------------------

class TestTextFormatter:
    def test_matches_original_format(self):
        fmt = TextFormatter()
        record = logging.LogRecord(
            name="swe_team.monitor",
            level=logging.WARNING,
            pathname="monitor.py",
            lineno=10,
            msg="disk full",
            args=(),
            exc_info=None,
        )
        output = fmt.format(record)
        # Original format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        assert "[WARNING]" in output
        assert "swe_team.monitor:" in output
        assert "disk full" in output


# ---------------------------------------------------------------------------
# get_formatter factory
# ---------------------------------------------------------------------------

class TestGetFormatter:
    def test_default_is_text(self):
        assert isinstance(get_formatter(), TextFormatter)

    def test_text_explicit(self):
        assert isinstance(get_formatter("text"), TextFormatter)

    def test_json_explicit(self):
        assert isinstance(get_formatter("json"), JsonFormatter)

    def test_unknown_falls_back_to_text(self):
        assert isinstance(get_formatter("xml"), TextFormatter)


# ---------------------------------------------------------------------------
# resolve_log_format — config / env selection
# ---------------------------------------------------------------------------

class TestResolveLogFormat:
    def test_default_is_text(self):
        assert resolve_log_format() == "text"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("SWE_LOG_FORMAT", "json")
        cfg = {"logging": {"format": "text"}}
        assert resolve_log_format(cfg) == "json"

    def test_config_used_when_no_env(self, monkeypatch):
        monkeypatch.delenv("SWE_LOG_FORMAT", raising=False)
        cfg = {"logging": {"format": "json"}}
        assert resolve_log_format(cfg) == "json"

    def test_invalid_env_ignored(self, monkeypatch):
        monkeypatch.setenv("SWE_LOG_FORMAT", "xml")
        assert resolve_log_format() == "text"

    def test_missing_config_key(self, monkeypatch):
        monkeypatch.delenv("SWE_LOG_FORMAT", raising=False)
        assert resolve_log_format({"logging": {}}) == "text"
        assert resolve_log_format({}) == "text"
        assert resolve_log_format(None) == "text"
