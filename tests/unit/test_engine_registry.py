"""Tests for the coding engine provider registry."""
from __future__ import annotations

import pytest

from src.swe_team.providers.coding_engine import (
    resolve_engine,
    register_engine,
    list_engines,
    _REGISTRY,
)
from src.swe_team.providers.coding_engine.base import CodingEngine


class TestResolveEngine:
    def test_resolve_claude_default(self):
        engine = resolve_engine("claude", {"timeout_seconds": 60})
        assert engine.name == "claude"

    def test_resolve_claude_with_binary(self):
        engine = resolve_engine("claude", {
            "claude_path": "/usr/local/bin/claude",
            "timeout_seconds": 120,
        })
        assert engine.name == "claude"

    def test_resolve_claude_with_tools(self):
        engine = resolve_engine("claude", {
            "allowed_tools": "Read,Write,Edit",
        })
        assert engine.allowed_tools == "Read,Write,Edit"

    def test_resolve_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown coding engine provider"):
            resolve_engine("nonexistent-engine")

    def test_resolve_unknown_shows_available(self):
        with pytest.raises(ValueError, match="claude"):
            resolve_engine("nonexistent-engine")

    def test_resolve_empty_config(self):
        engine = resolve_engine("claude")
        assert engine.name == "claude"


class TestRegisterEngine:
    def test_register_custom_engine(self):
        class MockEngine:
            @property
            def name(self):
                return "mock"
            def run(self, prompt, *, model, timeout, cwd=None):
                pass
            def health_check(self):
                return True

        register_engine("mock", lambda cfg: MockEngine())
        engine = resolve_engine("mock")
        assert engine.name == "mock"
        # Cleanup
        del _REGISTRY["mock"]

    def test_register_overwrites(self):
        original = _REGISTRY.get("claude")
        register_engine("claude", lambda cfg: "replaced")
        assert _REGISTRY["claude"]({}) == "replaced"
        # Restore
        _REGISTRY["claude"] = original


class TestListEngines:
    def test_list_contains_claude(self):
        engines = list_engines()
        assert "claude" in engines

    def test_list_sorted(self):
        engines = list_engines()
        assert engines == sorted(engines)
