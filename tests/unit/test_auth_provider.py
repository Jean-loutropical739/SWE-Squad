"""Tests for the Provider Authentication Manager (Issue #130)."""
from __future__ import annotations

import json
import threading
import time
from unittest import mock

import pytest

from src.swe_team.providers.auth.base import AuthState, AuthProvider
from src.swe_team.providers.auth.inmemory_provider import InMemoryAuthProvider


# ═══════════════════════════════════════════════════════════════════════
# AuthState tests
# ═══════════════════════════════════════════════════════════════════════


class TestAuthState:
    """Tests for the AuthState dataclass."""

    def test_default_values(self):
        state = AuthState(provider_name="test")
        assert state.provider_name == "test"
        assert state.is_authenticated is False
        assert state.auth_method == "api_key"
        assert state.key_last_rotated is None
        assert state.key_expires_at is None
        assert state.consecutive_auth_failures == 0
        assert state.last_auth_error is None

    def test_is_expired_no_expiry(self):
        state = AuthState(provider_name="test")
        assert state.is_expired() is False

    def test_is_expired_future(self):
        state = AuthState(provider_name="test", key_expires_at=time.monotonic() + 3600)
        assert state.is_expired() is False

    def test_is_expired_past(self):
        state = AuthState(provider_name="test", key_expires_at=time.monotonic() - 1)
        assert state.is_expired() is True

    def test_is_healthy_default(self):
        """Default state is not healthy (is_authenticated=False)."""
        state = AuthState(provider_name="test")
        assert state.is_healthy() is False

    def test_is_healthy_authenticated(self):
        state = AuthState(provider_name="test", is_authenticated=True)
        assert state.is_healthy() is True

    def test_is_healthy_expired(self):
        state = AuthState(
            provider_name="test",
            is_authenticated=True,
            key_expires_at=time.monotonic() - 1,
        )
        assert state.is_healthy() is False

    def test_is_healthy_too_many_failures(self):
        state = AuthState(
            provider_name="test",
            is_authenticated=True,
            consecutive_auth_failures=3,
        )
        assert state.is_healthy() is False

    def test_is_healthy_below_threshold(self):
        state = AuthState(
            provider_name="test",
            is_authenticated=True,
            consecutive_auth_failures=2,
        )
        assert state.is_healthy() is True


# ═══════════════════════════════════════════════════════════════════════
# InMemoryAuthProvider tests
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryAuthProvider:
    """Tests for the InMemoryAuthProvider implementation."""

    def test_init_empty(self):
        provider = InMemoryAuthProvider()
        assert provider.list_states() == []

    def test_init_with_names(self):
        provider = InMemoryAuthProvider(["github", "base_llm", "telegram"])
        states = provider.list_states()
        assert len(states) == 3
        assert [s.provider_name for s in states] == ["base_llm", "github", "telegram"]

    def test_get_state_creates_missing(self):
        provider = InMemoryAuthProvider()
        state = provider.get_state("new_provider")
        assert state.provider_name == "new_provider"
        assert len(provider.list_states()) == 1

    def test_get_state_returns_existing(self):
        provider = InMemoryAuthProvider(["github"])
        state1 = provider.get_state("github")
        state2 = provider.get_state("github")
        assert state1 is state2

    def test_record_auth_success(self):
        provider = InMemoryAuthProvider(["github"])
        provider.record_auth_failure("github", "401")
        provider.record_auth_success("github")
        state = provider.get_state("github")
        assert state.is_authenticated is True
        assert state.consecutive_auth_failures == 0
        assert state.last_auth_error is None

    def test_record_auth_failure_increments(self):
        provider = InMemoryAuthProvider(["github"])
        provider.record_auth_success("github")  # start authenticated
        provider.record_auth_failure("github", "401 Unauthorized")
        state = provider.get_state("github")
        assert state.consecutive_auth_failures == 1
        assert state.last_auth_error == "401 Unauthorized"
        # Still authenticated after 1 failure (threshold is 3)
        assert state.is_authenticated is True

    def test_record_auth_failure_threshold_revokes(self):
        provider = InMemoryAuthProvider(["github"])
        provider.record_auth_success("github")
        for i in range(3):
            provider.record_auth_failure("github", f"error {i}")
        state = provider.get_state("github")
        assert state.is_authenticated is False
        assert state.consecutive_auth_failures == 3

    def test_record_auth_failure_creates_state(self):
        provider = InMemoryAuthProvider()
        provider.record_auth_failure("unknown", "err")
        assert len(provider.list_states()) == 1
        assert provider.get_state("unknown").consecutive_auth_failures == 1

    def test_rotate_key(self):
        provider = InMemoryAuthProvider(["github"])
        provider.record_auth_failure("github", "err")
        provider.record_auth_failure("github", "err")
        provider.rotate_key("github", "new-secret-key")
        state = provider.get_state("github")
        assert state.is_authenticated is True
        assert state.consecutive_auth_failures == 0
        assert state.last_auth_error is None
        assert state.key_last_rotated is not None
        assert state.key_last_rotated <= time.monotonic()

    def test_rotate_key_creates_state(self):
        provider = InMemoryAuthProvider()
        provider.rotate_key("new", "key")
        assert len(provider.list_states()) == 1

    def test_list_states_sorted(self):
        provider = InMemoryAuthProvider(["z_provider", "a_provider", "m_provider"])
        states = provider.list_states()
        names = [s.provider_name for s in states]
        assert names == ["a_provider", "m_provider", "z_provider"]

    def test_health_check_all_healthy(self):
        provider = InMemoryAuthProvider(["a", "b"])
        provider.record_auth_success("a")
        provider.record_auth_success("b")
        assert provider.health_check() is True

    def test_health_check_one_unhealthy(self):
        provider = InMemoryAuthProvider(["a", "b"])
        provider.record_auth_success("a")
        # b is not authenticated -> not healthy
        assert provider.health_check() is False

    def test_health_check_empty(self):
        provider = InMemoryAuthProvider()
        assert provider.health_check() is True

    def test_protocol_compliance(self):
        """InMemoryAuthProvider satisfies the AuthProvider protocol."""
        provider = InMemoryAuthProvider()
        assert isinstance(provider, AuthProvider)


# ═══════════════════════════════════════════════════════════════════════
# Thread safety tests
# ═══════════════════════════════════════════════════════════════════════


class TestInMemoryAuthProviderThreadSafety:
    """Verify concurrent access does not corrupt state."""

    def test_concurrent_failures(self):
        provider = InMemoryAuthProvider(["github"])
        n_threads = 10
        n_calls = 50
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(n_calls):
                provider.record_auth_failure("github", "concurrent-error")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = provider.get_state("github")
        assert state.consecutive_auth_failures == n_threads * n_calls

    def test_concurrent_mixed_operations(self):
        provider = InMemoryAuthProvider(["svc"])
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def success_worker():
            barrier.wait()
            for _ in range(20):
                provider.record_auth_success("svc")

        def failure_worker():
            barrier.wait()
            for _ in range(20):
                provider.record_auth_failure("svc", "err")

        threads = []
        for i in range(n_threads):
            fn = success_worker if i % 2 == 0 else failure_worker
            threads.append(threading.Thread(target=fn))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # State should be consistent (no exceptions, valid values)
        state = provider.get_state("svc")
        assert isinstance(state.consecutive_auth_failures, int)
        assert state.consecutive_auth_failures >= 0


# ═══════════════════════════════════════════════════════════════════════
# Wiring tests (embeddings.py integration)
# ═══════════════════════════════════════════════════════════════════════


class TestEmbeddingsAuthWiring:
    """Test that embeddings.py auth provider wiring works."""

    def test_set_auth_provider(self):
        from src.swe_team import embeddings
        provider = InMemoryAuthProvider(["base_llm"])
        old = embeddings._auth_provider
        try:
            embeddings.set_auth_provider(provider)
            assert embeddings._auth_provider is provider
        finally:
            embeddings._auth_provider = old

    def test_disable_base_llm_records_failure(self):
        from src.swe_team import embeddings
        provider = InMemoryAuthProvider(["base_llm"])
        old = embeddings._auth_provider
        try:
            embeddings.set_auth_provider(provider)
            embeddings._disable_base_llm()
            state = provider.get_state("base_llm")
            assert state.consecutive_auth_failures == 1
            assert "AUTH_ERROR" in (state.last_auth_error or "")
        finally:
            embeddings._auth_provider = old
            embeddings._BASE_LLM_DISABLED = False
            embeddings._BASE_LLM_DISABLED_UNTIL = 0.0

    def test_disable_base_llm_no_provider_no_error(self):
        from src.swe_team import embeddings
        old = embeddings._auth_provider
        try:
            embeddings._auth_provider = None
            # Should not raise
            embeddings._disable_base_llm()
        finally:
            embeddings._auth_provider = old
            embeddings._BASE_LLM_DISABLED = False
            embeddings._BASE_LLM_DISABLED_UNTIL = 0.0


# ═══════════════════════════════════════════════════════════════════════
# Dashboard API tests
# ═══════════════════════════════════════════════════════════════════════


class TestDashboardAuthAPI:
    """Test /api/auth/status endpoint response structure."""

    def test_auth_status_no_provider(self):
        """When no auth_provider is set, return empty providers list plus session info."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = mock.MagicMock(spec=DashboardHandler)
        handler.auth_provider = None
        DashboardHandler._handle_api_auth_status(handler)
        call_args = handler._json_response.call_args[0][0]
        assert call_args["providers"] == []
        # session key is always present
        assert "session" in call_args

    def test_auth_status_with_provider(self):
        """When auth_provider is set, return provider states."""
        from scripts.ops.dashboard_server import DashboardHandler
        provider = InMemoryAuthProvider(["base_llm", "github"])
        provider.record_auth_success("base_llm")
        provider.record_auth_failure("github", "401 Unauthorized")

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.auth_provider = provider
        DashboardHandler._handle_api_auth_status(handler)

        call_args = handler._json_response.call_args[0][0]
        providers = call_args["providers"]
        assert len(providers) == 2

        base_llm = next(p for p in providers if p["name"] == "base_llm")
        assert base_llm["is_authenticated"] is True
        assert base_llm["is_healthy"] is True
        assert base_llm["consecutive_failures"] == 0

        github = next(p for p in providers if p["name"] == "github")
        assert github["consecutive_failures"] == 1
        assert github["last_error"] == "401 Unauthorized"


# ═══════════════════════════════════════════════════════════════════════
# CLI tests
# ═══════════════════════════════════════════════════════════════════════


class TestCLIAuthCommand:
    """Test that the auth subcommand is registered and parseable."""

    def test_auth_subcommand_registered(self):
        from scripts.ops.swe_cli import build_parser
        parser = build_parser()
        # Should parse without error
        args = parser.parse_args(["auth", "status"])
        assert args.command == "auth"
        assert hasattr(args, "func")

    def test_auth_subcommand_json_flag(self):
        from scripts.ops.swe_cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["auth", "status", "--json"])
        assert args.json is True
