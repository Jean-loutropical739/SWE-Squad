"""Tests for ClaudeCodeEngine JSON output parsing (--output-format json)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import EngineResult
from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine


# ---------------------------------------------------------------------------
# EngineResult new fields
# ---------------------------------------------------------------------------

class TestEngineResultFields:
    """Verify new telemetry fields on EngineResult default to None."""

    def test_defaults(self):
        r = EngineResult(stdout="hi", stderr="", returncode=0)
        assert r.input_tokens is None
        assert r.output_tokens is None
        assert r.cache_read_tokens is None
        assert r.cache_creation_tokens is None
        assert r.num_turns is None
        assert r.duration_api_ms is None
        assert r.session_id is None

    def test_explicit(self):
        r = EngineResult(
            stdout="ok", stderr="", returncode=0,
            input_tokens=100, output_tokens=50,
            cache_read_tokens=20, cache_creation_tokens=10,
            num_turns=3, duration_api_ms=1200, session_id="abc",
        )
        assert r.input_tokens == 100
        assert r.output_tokens == 50
        assert r.cache_read_tokens == 20
        assert r.cache_creation_tokens == 10
        assert r.num_turns == 3
        assert r.duration_api_ms == 1200
        assert r.session_id == "abc"


# ---------------------------------------------------------------------------
# _build_cmd: --output-format json replaces --verbose
# ---------------------------------------------------------------------------

class TestBuildCmd:
    def test_output_format_json_in_cmd(self):
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        cmd = engine._build_cmd("sonnet")
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"
        assert "--verbose" not in cmd

    def test_session_and_resume(self):
        import uuid
        valid_sid = str(uuid.uuid4())
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        cmd = engine._build_cmd("sonnet", session_id=valid_sid, resume=True)
        assert "--resume" in cmd
        assert valid_sid in cmd

    def test_dev_engine_has_edit_and_write_tools(self):
        """Dev engine constructed with full tool list exposes tools via property and includes
        --allowedTools with Edit/Write/Bash in the spawned subprocess args."""
        dev_tools = "Read,Edit,Write,Bash(git:*),Bash(pytest:*),Bash(python3:*),Grep,Glob"
        engine = ClaudeCodeEngine(binary="/usr/bin/claude", allowed_tools=dev_tools)

        # allowed_tools property must expose the configured tool string
        assert engine.allowed_tools == dev_tools

        # _build_cmd() must include --allowedTools so the subprocess gets tool access
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        tool_str = cmd[idx + 1]
        assert "Edit" in tool_str
        assert "Write" in tool_str
        assert "Bash(git:*)" in tool_str

    def test_engine_without_tools_has_none_property(self):
        """Investigation engine constructed without allowed_tools returns None."""
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        assert engine.allowed_tools is None
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" not in cmd


# ---------------------------------------------------------------------------
# _parse_json_output
# ---------------------------------------------------------------------------

class TestParseJsonOutput:
    def test_valid_json(self):
        payload = json.dumps({
            "type": "result",
            "subtype": "success",
            "cost_usd": 0.065,
            "duration_ms": 2380,
            "duration_api_ms": 2300,
            "num_turns": 1,
            "result": "Hello!",
            "session_id": "sess-123",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 30,
            },
        })
        data = ClaudeCodeEngine._parse_json_output(payload)
        assert data["result"] == "Hello!"
        assert data["cost_usd"] == 0.065
        assert data["usage"]["input_tokens"] == 500

    def test_plain_text_fallback(self):
        data = ClaudeCodeEngine._parse_json_output("just plain text")
        assert data["result"] == "just plain text"

    def test_empty_string(self):
        data = ClaudeCodeEngine._parse_json_output("")
        assert data["result"] == ""

    def test_non_dict_json(self):
        data = ClaudeCodeEngine._parse_json_output("[1,2,3]")
        assert data["result"] == "[1,2,3]"


# ---------------------------------------------------------------------------
# _build_engine_result
# ---------------------------------------------------------------------------

class TestBuildEngineResult:
    def test_full_json(self):
        data = {
            "result": "fixed the bug",
            "cost_usd": 0.12,
            "num_turns": 2,
            "duration_api_ms": 5000,
            "session_id": "sess-abc",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 400,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
            },
        }
        r = ClaudeCodeEngine._build_engine_result(data, "{}", "", 0, "sonnet")
        assert r.stdout == "fixed the bug"
        assert r.cost_usd == 0.12
        assert r.input_tokens == 1000
        assert r.output_tokens == 400
        assert r.cache_read_tokens == 100
        assert r.cache_creation_tokens == 50
        assert r.num_turns == 2
        assert r.duration_api_ms == 5000
        assert r.session_id == "sess-abc"
        assert r.returncode == 0
        assert r.model == "sonnet"

    def test_missing_usage(self):
        data = {"result": "hi"}
        r = ClaudeCodeEngine._build_engine_result(data, "{}", "", 0, "haiku")
        assert r.stdout == "hi"
        assert r.input_tokens is None
        assert r.output_tokens is None
        assert r.cache_read_tokens is None

    def test_fallback_cost_from_stderr(self):
        data = {"result": "ok"}
        r = ClaudeCodeEngine._build_engine_result(data, "{}", "Total cost: $0.42", 0, "sonnet")
        assert r.cost_usd == 0.42


# ---------------------------------------------------------------------------
# Integration: run() parses JSON stdout
# ---------------------------------------------------------------------------

class TestRunJsonIntegration:
    def _mock_subprocess_json(self, json_data, returncode=0):
        """Create a mock CompletedProcess with JSON stdout."""
        return subprocess.CompletedProcess(
            args=[], returncode=returncode,
            stdout=json.dumps(json_data), stderr="",
        )

    @patch("subprocess.run")
    def test_run_parses_json(self, mock_run):
        mock_run.return_value = self._mock_subprocess_json({
            "type": "result",
            "subtype": "success",
            "cost_usd": 0.05,
            "duration_api_ms": 1500,
            "num_turns": 1,
            "result": "Investigation complete.",
            "session_id": "s-42",
            "usage": {
                "input_tokens": 800,
                "output_tokens": 300,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 20,
            },
        })
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        r = engine.run("test prompt", model="sonnet", timeout=60)
        assert r.stdout == "Investigation complete."
        assert r.cost_usd == 0.05
        assert r.input_tokens == 800
        assert r.output_tokens == 300
        assert r.cache_read_tokens == 60
        assert r.cache_creation_tokens == 20
        assert r.num_turns == 1
        assert r.duration_api_ms == 1500
        assert r.session_id == "s-42"
        assert r.success

    @patch("subprocess.run")
    def test_resume_parses_json(self, mock_run):
        mock_run.return_value = self._mock_subprocess_json({
            "type": "result",
            "result": "Resumed.",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        r = engine.resume("sess-1", "continue", model="sonnet", timeout=30)
        assert r.stdout == "Resumed."
        assert r.input_tokens == 100

    @patch("subprocess.run")
    def test_run_non_json_fallback(self, mock_run):
        """If stdout is not JSON (e.g. old CLI version), treat as plain text."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="plain text result", stderr="Total cost: $0.10",
        )
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        r = engine.run("prompt", model="sonnet", timeout=60)
        assert r.stdout == "plain text result"
        assert r.cost_usd == 0.10
        assert r.input_tokens is None

    @patch("subprocess.run")
    def test_timeout_raises_by_default(self, mock_run):
        """TimeoutExpired is re-raised by default (raise_on_timeout=True)."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt", model="sonnet", timeout=30)

    @patch("subprocess.run")
    def test_timeout_returns_empty_result_legacy(self, mock_run):
        """With raise_on_timeout=False the legacy EngineResult(-1) is returned."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        engine = ClaudeCodeEngine(binary="/usr/bin/claude")
        r = engine.run("prompt", model="sonnet", timeout=30, raise_on_timeout=False)
        assert r.returncode == -1
        assert not r.success
        assert r.input_tokens is None
        assert r.metadata.get("error_type") == "timeout"


# ---------------------------------------------------------------------------
# _parse_cost_legacy (backwards compat)
# ---------------------------------------------------------------------------

class TestParseCostLegacy:
    def test_parse_cost_still_works(self):
        assert ClaudeCodeEngine._parse_cost("Total cost: $1.23") == 1.23

    def test_parse_cost_legacy(self):
        assert ClaudeCodeEngine._parse_cost_legacy("cost: $0.42") == 0.42

    def test_parse_cost_none(self):
        assert ClaudeCodeEngine._parse_cost_legacy("no cost here") is None
