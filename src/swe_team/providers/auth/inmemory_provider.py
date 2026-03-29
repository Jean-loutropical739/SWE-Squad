"""In-memory implementation of AuthProvider — thread-safe auth state tracking."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from src.swe_team.providers.auth.base import AuthProvider, AuthState

logger = logging.getLogger(__name__)

# Failure threshold: after this many consecutive auth failures the provider
# is marked as unauthenticated.
_FAILURE_THRESHOLD = 3


class InMemoryAuthProvider:
    """Thread-safe in-memory authentication state manager.

    Stores :class:`AuthState` per provider in a dict protected by a
    :class:`threading.Lock`.  Suitable for single-process deployments;
    state is lost on restart.
    """

    def __init__(self, provider_names: Optional[List[str]] = None) -> None:
        self._lock = threading.Lock()
        self._states: Dict[str, AuthState] = {}
        for name in (provider_names or []):
            self._states[name] = AuthState(provider_name=name)

    # -- Protocol methods ---------------------------------------------------

    def get_state(self, provider_name: str) -> AuthState:
        """Return the auth state for *provider_name*, creating one if absent."""
        with self._lock:
            if provider_name not in self._states:
                self._states[provider_name] = AuthState(provider_name=provider_name)
            return self._states[provider_name]

    def record_auth_success(self, provider_name: str) -> None:
        """Record a successful auth event: reset failures, mark authenticated.

        Note: revocation is NOT cleared by a success event.  Only an explicit
        call to :meth:`unrevoke` can lift a revocation.
        """
        with self._lock:
            state = self._states.setdefault(
                provider_name, AuthState(provider_name=provider_name)
            )
            # Revoked providers cannot be re-authenticated via success events.
            if state.is_revoked():
                logger.warning(
                    "Auth success ignored for revoked provider %s — call unrevoke() first",
                    provider_name,
                )
                return
            state.is_authenticated = True
            state.consecutive_auth_failures = 0
            state.last_auth_error = None
            logger.debug("Auth success recorded for %s", provider_name)

    def record_auth_failure(self, provider_name: str, error: str) -> None:
        """Record an auth failure.  After ``_FAILURE_THRESHOLD`` consecutive
        failures the provider is marked unauthenticated.
        """
        with self._lock:
            state = self._states.setdefault(
                provider_name, AuthState(provider_name=provider_name)
            )
            state.consecutive_auth_failures += 1
            state.last_auth_error = error
            if state.consecutive_auth_failures >= _FAILURE_THRESHOLD:
                state.is_authenticated = False
            logger.warning(
                "Auth failure #%d for %s: %s",
                state.consecutive_auth_failures,
                provider_name,
                error,
            )

    def rotate_key(self, provider_name: str, new_key: str) -> None:
        """Record a key rotation: reset failures, update rotation timestamp.

        The *new_key* value is intentionally **not** stored — this class only
        tracks state, not secrets.  The actual key must be injected into the
        provider's environment or config by the caller.
        """
        with self._lock:
            state = self._states.setdefault(
                provider_name, AuthState(provider_name=provider_name)
            )
            state.key_last_rotated = time.monotonic()
            state.consecutive_auth_failures = 0
            state.last_auth_error = None
            state.is_authenticated = True
            logger.info("Key rotated for provider %s", provider_name)

    def revoke(self, provider_name: str, reason: str) -> None:
        """Immediately revoke a provider's authentication.

        Sets ``is_authenticated=False``, saturates ``consecutive_auth_failures``
        to the failure threshold, and records ``revocation_reason`` /
        ``revoked_at``.  Once revoked, :meth:`record_auth_success` will NOT
        clear the revocation — only :meth:`unrevoke` can lift it.
        """
        with self._lock:
            state = self._states.setdefault(
                provider_name, AuthState(provider_name=provider_name)
            )
            state.is_authenticated = False
            state.consecutive_auth_failures = _FAILURE_THRESHOLD
            state.revocation_reason = reason
            state.revoked_at = time.monotonic()
            logger.warning(
                "Provider %s has been REVOKED: %s",
                provider_name,
                reason,
            )

    def unrevoke(self, provider_name: str) -> None:
        """Lift a revocation so the provider can be re-authenticated.

        Clears ``revocation_reason``, ``revoked_at``, and resets
        ``consecutive_auth_failures`` to zero.  Does NOT set
        ``is_authenticated`` — the provider must go through normal auth again.
        """
        with self._lock:
            if provider_name not in self._states:
                logger.debug("unrevoke called for unknown provider %s — no-op", provider_name)
                return
            state = self._states[provider_name]
            state.revocation_reason = None
            state.revoked_at = None
            state.consecutive_auth_failures = 0
            logger.info("Revocation lifted for provider %s", provider_name)

    def is_revoked(self, provider_name: str) -> bool:
        """Return True if the named provider has been explicitly revoked."""
        with self._lock:
            if provider_name not in self._states:
                return False
            return self._states[provider_name].is_revoked()

    def list_states(self) -> list[AuthState]:
        """Return all tracked states sorted by provider name."""
        with self._lock:
            return sorted(self._states.values(), key=lambda s: s.provider_name)

    def health_check(self) -> bool:
        """Return True if *all* tracked providers are healthy."""
        with self._lock:
            if not self._states:
                return True
            return all(s.is_healthy() for s in self._states.values())
