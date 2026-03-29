"""Tests for the credential scanner module."""

from __future__ import annotations

import pytest

from src.swe_team.credential_scanner import scan_text, scan_lines, CREDENTIAL_PATTERNS


class TestScanText:
    """Tests for scan_text()."""

    def test_clean_text_returns_empty(self):
        assert scan_text("This is perfectly clean code.") == []

    def test_empty_string(self):
        assert scan_text("") == []

    def test_detects_gh_token(self):
        text = "GH_TOKEN=ghp_1234567890abcdef1234"
        matches = scan_text(text)
        assert len(matches) == 1
        assert "GH_TOKEN" in matches[0]

    def test_detects_gh_token_with_spaces(self):
        text = "GH_TOKEN = ghp_1234567890abcdef1234"
        matches = scan_text(text)
        assert len(matches) == 1

    def test_detects_supabase_key(self):
        text = "SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefgh"
        matches = scan_text(text)
        assert len(matches) == 1
        assert "SUPABASE_ANON_KEY" in matches[0]

    def test_detects_telegram_token(self):
        text = "TELEGRAM_BOT_TOKEN=123456789:ABCDefgh_ijklmnop-qrstuvwx"
        matches = scan_text(text)
        assert len(matches) == 1
        assert "TELEGRAM_BOT_TOKEN" in matches[0]

    def test_detects_anthropic_sk_ant(self):
        text = "my key is sk-ant-abcdefghijklmnopqrstuvwxyz"
        matches = scan_text(text)
        assert len(matches) == 1
        assert "sk-ant-" in matches[0]

    def test_detects_base_llm_api_key(self):
        text = "BASE_LLM_API_KEY=some-really-long-api-key-value"
        matches = scan_text(text)
        assert len(matches) == 1
        assert "BASE_LLM_API_KEY" in matches[0]

    def test_detects_anthropic_api_key(self):
        text = "ANTHROPIC_API_KEY=sk-ant-api03-verylongkeyhere1234"
        matches = scan_text(text)
        assert len(matches) >= 1

    def test_short_gh_token_not_detected(self):
        """GH_TOKEN with < 10 chars value should not match."""
        text = "GH_TOKEN=short"
        assert scan_text(text) == []

    def test_short_supabase_key_not_detected(self):
        """SUPABASE_ANON_KEY with < 20 chars should not match."""
        text = "SUPABASE_ANON_KEY=short_key"
        assert scan_text(text) == []

    def test_multiple_credentials_detected(self):
        text = (
            "GH_TOKEN=ghp_1234567890abcdef1234\n"
            "ANTHROPIC_API_KEY=sk-ant-api03-verylongkeyhere1234\n"
        )
        matches = scan_text(text)
        assert len(matches) >= 2

    def test_env_example_format_detected(self):
        """Even .env.example with real-looking values should flag."""
        text = "GH_TOKEN=ghp_realLookingTokenValue1234567890"
        matches = scan_text(text)
        assert len(matches) == 1

    def test_comment_with_credential_detected(self):
        """Credentials in comments should still be detected."""
        text = "# GH_TOKEN=ghp_1234567890abcdef1234"
        matches = scan_text(text)
        assert len(matches) == 1

    def test_telegram_token_wrong_format_not_detected(self):
        """TELEGRAM_BOT_TOKEN without numeric prefix:alpha suffix should not match."""
        text = "TELEGRAM_BOT_TOKEN=not-a-valid-token"
        assert scan_text(text) == []

    def test_sk_ant_short_not_detected(self):
        """sk-ant- with fewer than 20 trailing chars should not match."""
        text = "sk-ant-short"
        assert scan_text(text) == []

    def test_multiline_text(self):
        text = "line1\nline2\nGH_TOKEN=ghp_abcdefghijklmnopqrst\nline4"
        matches = scan_text(text)
        assert len(matches) == 1

    def test_pattern_count(self):
        """Ensure we have at least 6 credential patterns."""
        assert len(CREDENTIAL_PATTERNS) >= 6


class TestScanLines:
    """Tests for scan_lines()."""

    def test_empty_list(self):
        assert scan_lines([]) == []

    def test_returns_line_numbers(self):
        lines = ["clean line", "GH_TOKEN=ghp_1234567890abcdef1234", "another clean"]
        results = scan_lines(lines)
        assert len(results) == 1
        line_no, line, match = results[0]
        assert line_no == 2
        assert "GH_TOKEN" in line

    def test_multiple_hits_different_lines(self):
        lines = [
            "GH_TOKEN=ghp_1234567890abcdef1234",
            "clean",
            "BASE_LLM_API_KEY=some-really-long-api-key-value",
        ]
        results = scan_lines(lines)
        assert len(results) == 2
        assert results[0][0] == 1
        assert results[1][0] == 3

    def test_clean_lines(self):
        lines = ["import os", "x = 42", "print('hello')"]
        assert scan_lines(lines) == []
