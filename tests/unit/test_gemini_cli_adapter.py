"""Unit tests for src/swe_team/gemini_cli_adapter.py."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.swe_team.gemini_cli_adapter import (
    GeminiCLIAdapter,
    _UNSAFE_KEYWORDS,
    _RATE_LIMIT_KEYWORDS,
    _COOLDOWN_SECONDS,
    _COOLDOWN_FILE,
    _DEFAULT_MODELS,
)


def _make_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return a CompletedProcess-like object."""
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


class TestGeminiCLIAdapterInit(unittest.TestCase):
    def test_default_command(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_CLI_PATH", None)
            os.environ.pop("GEMINI_MODELS", None)
            adapter = GeminiCLIAdapter()
        assert adapter._command == "/usr/bin/gemini"

    def test_custom_command_via_arg(self):
        adapter = GeminiCLIAdapter(command="/usr/local/bin/gemini")
        assert adapter._command == "/usr/local/bin/gemini"

    def test_custom_command_via_env(self):
        with patch.dict(os.environ, {"GEMINI_CLI_PATH": "/opt/gemini", "GEMINI_MODELS": "null"}):
            adapter = GeminiCLIAdapter()
        assert adapter._command == "/opt/gemini"

    def test_default_skills(self):
        adapter = GeminiCLIAdapter()
        assert adapter.has_skill("investigate")
        assert adapter.has_skill("dashboard")
        assert adapter.has_skill("websearch")

    def test_custom_skills(self):
        adapter = GeminiCLIAdapter(skills=["investigate"])
        assert adapter.has_skill("investigate")
        assert not adapter.has_skill("dashboard")

    def test_build_failover_chain_starts_with_configured(self):
        adapter = GeminiCLIAdapter(model="gemini-3-pro-high", models=["gemini-2.5-pro", "gemini-2.5-flash"])
        chain = adapter._build_failover_chain()
        assert chain[0] == "gemini-3-pro-high"
        assert "gemini-2.5-pro" in chain
        assert "gemini-2.5-flash" in chain


class TestGeminiCLIAdapterIsAvailable(unittest.TestCase):
    @patch("src.swe_team.gemini_cli_adapter.shutil.which", return_value=None)
    @patch("src.swe_team.gemini_cli_adapter.os.path.isfile", return_value=False)
    def test_not_available_when_binary_missing(self, mock_isfile, mock_which):
        adapter = GeminiCLIAdapter(command="/nonexistent/gemini")
        assert adapter.is_available() is False

    @patch("src.swe_team.gemini_cli_adapter.shutil.which", return_value="/usr/bin/gemini")
    def test_available_when_binary_found(self, mock_which):
        # Patch _COOLDOWN_FILE at module level using a fake Path that doesn't exist
        mock_cooldown = MagicMock(spec=Path)
        mock_cooldown.exists.return_value = False
        with patch("src.swe_team.gemini_cli_adapter._COOLDOWN_FILE", mock_cooldown):
            adapter = GeminiCLIAdapter()
            assert adapter.is_available() is True

    @patch("src.swe_team.gemini_cli_adapter.shutil.which", return_value="/usr/bin/gemini")
    def test_not_available_during_cooldown(self, mock_which):
        mock_stat = MagicMock()
        mock_stat.st_mtime = time.time() - 60  # only 60s ago, within 900s cooldown
        mock_cooldown = MagicMock(spec=Path)
        mock_cooldown.exists.return_value = True
        mock_cooldown.stat.return_value = mock_stat
        with patch("src.swe_team.gemini_cli_adapter._COOLDOWN_FILE", mock_cooldown):
            adapter = GeminiCLIAdapter()
            assert adapter.is_available() is False


class TestGeminiCLIAdapterInvoke(unittest.TestCase):
    def _make_adapter_available(self, adapter):
        """Patch is_available to return True."""
        adapter.is_available = MagicMock(return_value=True)

    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_invoke_success(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0, stdout="root cause: memory leak")
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini")
        self._make_adapter_available(adapter)
        result = adapter.invoke("analyze this log")
        assert result == "root cause: memory leak"

    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_invoke_non_zero_exit_returns_none(self, mock_run):
        mock_run.return_value = _make_proc(returncode=1, stderr="unknown error")
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini", models=[])
        self._make_adapter_available(adapter)
        result = adapter.invoke("analyze this log")
        assert result is None

    def test_invoke_with_unsafe_keyword_returns_none(self):
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini")
        self._make_adapter_available(adapter)
        result = adapter.invoke("my password is abc123")
        assert result is None

    def test_invoke_with_token_keyword_returns_none(self):
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini")
        self._make_adapter_available(adapter)
        result = adapter.invoke("the api_key is 12345")
        assert result is None

    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_invoke_prompt_truncation(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0, stdout="truncated result")
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini", max_prompt_chars=10, models=[])
        self._make_adapter_available(adapter)
        long_prompt = "a" * 1000
        result = adapter.invoke(long_prompt)
        assert result == "truncated result"
        # Verify the truncated prompt was passed
        call_args = mock_run.call_args[0][0]
        prompt_arg = call_args[call_args.index("-p") + 1]
        assert "[... truncated for context limit ...]" in prompt_arg

    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_invoke_timeout_returns_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gemini", timeout=180)
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini", models=[])
        self._make_adapter_available(adapter)
        result = adapter.invoke("analyze", timeout=1)
        assert result is None

    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_invoke_empty_output_returns_none(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0, stdout="   ")
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini", models=[])
        self._make_adapter_available(adapter)
        result = adapter.invoke("analyze")
        assert result is None

    @patch("src.swe_team.gemini_cli_adapter.time.sleep")
    @patch("src.swe_team.gemini_cli_adapter.subprocess.run")
    def test_rate_limit_triggers_backoff_and_returns_none(self, mock_run, mock_sleep):
        """Rate-limit stderr causes all retries + model failover to be exhausted."""
        mock_run.return_value = _make_proc(returncode=1, stderr="429 rate limit exceeded")
        # Only one model in the chain so there's no failover
        adapter = GeminiCLIAdapter(command="/usr/bin/gemini", models=[])
        adapter._models = []  # no failover chain
        adapter._model = "gemini-3-pro-high"
        self._make_adapter_available(adapter)

        mock_cooldown = MagicMock(spec=Path)
        mock_cooldown.exists.return_value = False
        with patch("src.swe_team.gemini_cli_adapter._COOLDOWN_FILE", mock_cooldown):
            result = adapter.invoke("investigate this")

        assert result is None

    def test_not_available_returns_none_immediately(self):
        adapter = GeminiCLIAdapter(command="/nonexistent/gemini")
        with patch.object(adapter, "is_available", return_value=False):
            result = adapter.invoke("test prompt")
        assert result is None


if __name__ == "__main__":
    unittest.main()
