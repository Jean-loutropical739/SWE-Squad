"""Unit tests for src/swe_team/agent_registry.py."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.agent_registry import AgentRegistry, WELL_KNOWN_AGENT_CARD_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(name="gemini-cli", status="online", skills=None, priority=10, tags=None):
    if skills is None:
        default_tags = tags or ["investigate", "diagnose"]
        skills = [{"id": "investigate", "tags": default_tags}]
    return {
        "name": name,
        "url": f"http://localhost:9000/{name}",
        "skills": skills,
        "status": status,
        "priority": priority,
    }


def _fake_urlopen(data: dict, status: int = 200):
    """Return a mock context manager for urllib.request.urlopen."""
    body = json.dumps(data).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Basic registration / lookup
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_and_get(self):
        reg = AgentRegistry()
        card = _make_card()
        reg.register(card)
        assert reg.get("gemini-cli") is not None
        assert reg.get("gemini-cli")["name"] == "gemini-cli"

    def test_register_missing_name_raises(self):
        reg = AgentRegistry()
        with pytest.raises(ValueError, match="name"):
            reg.register({"url": "http://localhost"})

    def test_unregister_existing_agent(self):
        reg = AgentRegistry()
        reg.register(_make_card("agent-a"))
        result = reg.unregister("agent-a")
        assert result is True
        assert reg.get("agent-a") is None

    def test_unregister_nonexistent_returns_false(self):
        reg = AgentRegistry()
        assert reg.unregister("ghost") is False

    def test_overwrite_existing_registration(self):
        reg = AgentRegistry()
        reg.register(_make_card("agent-x", status="online"))
        reg.register(_make_card("agent-x", status="offline"))
        assert reg.get("agent-x")["status"] == "offline"


# ---------------------------------------------------------------------------
# select_agent
# ---------------------------------------------------------------------------

class TestSelectAgent:
    def test_selects_online_agent_with_matching_skill(self):
        reg = AgentRegistry()
        reg.register(_make_card("gemini-cli", status="online"))
        result = reg.select_agent("investigate")
        assert result is not None
        assert result["name"] == "gemini-cli"

    def test_skips_offline_agents(self):
        reg = AgentRegistry()
        reg.register(_make_card("agent-offline", status="offline"))
        result = reg.select_agent("investigate")
        assert result is None

    def test_returns_none_when_no_agents(self):
        reg = AgentRegistry()
        result = reg.select_agent("investigate")
        assert result is None

    def test_returns_none_when_skill_not_matched(self):
        reg = AgentRegistry()
        reg.register(_make_card("gemini-cli", skills=[{"id": "fix", "tags": ["fix"]}]))
        result = reg.select_agent("investigate")
        assert result is None

    def test_excludes_named_agents(self):
        reg = AgentRegistry()
        reg.register(_make_card("agent-a"))
        reg.register(_make_card("agent-b"))
        result = reg.select_agent("investigate", exclude=["agent-a"])
        assert result is not None
        assert result["name"] == "agent-b"

    def test_prefers_lower_priority_number(self):
        reg = AgentRegistry()
        reg.register(_make_card("slow-agent", priority=50))
        reg.register(_make_card("fast-agent", priority=5))
        result = reg.select_agent("investigate")
        assert result["name"] == "fast-agent"

    def test_prefers_heavy_agent_for_critical(self):
        reg = AgentRegistry()
        reg.register(_make_card("light", priority=1, tags=["investigate", "light"]))
        reg.register(_make_card(
            "heavy",
            priority=50,
            skills=[{"id": "investigate", "tags": ["investigate", "heavy"]}],
        ))
        result = reg.select_agent("investigate", severity="critical")
        assert result["name"] == "heavy"

    def test_skill_matched_by_tag(self):
        reg = AgentRegistry()
        card = {
            "name": "opencode",
            "url": "http://localhost",
            "skills": [{"id": "code-gen", "tags": ["fix", "implement"]}],
            "status": "online",
            "priority": 10,
        }
        reg.register(card)
        result = reg.select_agent("fix")
        assert result is not None
        assert result["name"] == "opencode"


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_stale_agents_expire(self):
        reg = AgentRegistry(ttl_seconds=1)
        reg.register(_make_card("temp-agent"))
        # Manually backdate the registration timestamp
        reg._registered_at["temp-agent"] = time.monotonic() - 5
        result = reg.select_agent("investigate")
        assert result is None

    def test_fresh_agents_do_not_expire(self):
        reg = AgentRegistry(ttl_seconds=300)
        reg.register(_make_card("fresh-agent"))
        result = reg.select_agent("investigate")
        assert result is not None


# ---------------------------------------------------------------------------
# list_agents / set_status
# ---------------------------------------------------------------------------

class TestListAndStatus:
    def test_list_all_agents(self):
        reg = AgentRegistry()
        reg.register(_make_card("a1"))
        reg.register(_make_card("a2", status="offline"))
        all_agents = reg.list_agents()
        assert len(all_agents) == 2

    def test_list_online_only(self):
        reg = AgentRegistry()
        reg.register(_make_card("a1"))
        reg.register(_make_card("a2", status="offline"))
        online = reg.list_agents(status="online")
        assert len(online) == 1
        assert online[0]["name"] == "a1"

    def test_set_status_updates_card(self):
        reg = AgentRegistry()
        reg.register(_make_card("agent-z"))
        reg.set_status("agent-z", "offline")
        assert reg.get("agent-z")["status"] == "offline"

    def test_set_status_nonexistent_returns_false(self):
        reg = AgentRegistry()
        assert reg.set_status("ghost", "online") is False


# ---------------------------------------------------------------------------
# Hub mode — _register_with_hub
# ---------------------------------------------------------------------------

class TestHubMode:
    def test_hub_mode_flag(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        assert reg.is_hub_mode is True

    def test_standalone_mode_flag(self):
        reg = AgentRegistry()
        assert reg.is_hub_mode is False

    def test_register_with_hub_success(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        resp = _fake_urlopen({}, status=200)
        with patch("src.swe_team.agent_registry.urllib.request.urlopen", return_value=resp):
            result = reg._register_with_hub(_make_card())
        assert result is True

    def test_register_with_hub_connection_error(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        with patch(
            "src.swe_team.agent_registry.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            result = reg._register_with_hub(_make_card())
        assert result is False

    def test_register_mirrors_to_hub(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        resp = _fake_urlopen({}, status=200)
        with patch("src.swe_team.agent_registry.urllib.request.urlopen", return_value=resp) as mock_open:
            reg.register(_make_card())
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# Hub discovery — _discover_from_hub
# ---------------------------------------------------------------------------

class TestHubDiscovery:
    def test_discover_from_hub_returns_agents(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        agents = [_make_card("swe-squad"), _make_card("gemini")]
        resp = _fake_urlopen(agents)
        with patch("src.swe_team.agent_registry.urllib.request.urlopen", return_value=resp):
            discovered = reg._discover_from_hub()
        assert len(discovered) == 2
        assert discovered[0]["name"] == "swe-squad"

    def test_discover_from_hub_handles_dict_response(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        resp = _fake_urlopen({"agents": [_make_card("swe-squad")]})
        with patch("src.swe_team.agent_registry.urllib.request.urlopen", return_value=resp):
            discovered = reg._discover_from_hub()
        assert len(discovered) == 1

    def test_discover_from_hub_network_failure_returns_empty(self):
        reg = AgentRegistry(hub_url="http://hub:18790")
        with patch(
            "src.swe_team.agent_registry.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            discovered = reg._discover_from_hub()
        assert discovered == []

    def test_discover_without_hub_returns_empty(self):
        reg = AgentRegistry()
        result = reg._discover_from_hub()
        assert result == []


# ---------------------------------------------------------------------------
# _fetch_agent_card
# ---------------------------------------------------------------------------

class TestFetchAgentCard:
    def test_fetches_valid_card(self):
        card = _make_card("remote-agent")
        resp = _fake_urlopen(card)
        with patch("src.swe_team.agent_registry.urllib.request.urlopen", return_value=resp):
            result = AgentRegistry._fetch_agent_card("http://agent/card")
        assert result is not None
        assert result["name"] == "remote-agent"

    def test_returns_none_on_network_error(self):
        with patch(
            "src.swe_team.agent_registry.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            result = AgentRegistry._fetch_agent_card("http://agent/card")
        assert result is None


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------

class TestCheckHealth:
    def test_health_check_nonexistent_returns_false(self):
        reg = AgentRegistry()
        assert reg.check_health("ghost") is False

    def test_health_check_uses_card_status_when_no_adapter_no_client(self):
        reg = AgentRegistry()
        reg.register(_make_card("healthy", status="online"))
        assert reg.check_health("healthy") is True

    def test_health_check_offline_card_returns_false(self):
        reg = AgentRegistry()
        reg.register(_make_card("sick", status="offline"))
        assert reg.check_health("sick") is False
