"""Provider authentication manager — per-provider auth state tracking."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable, Optional
import time


@dataclass
class AuthState:
    """Authentication state for a single provider."""

    provider_name: str
    is_authenticated: bool = False
    auth_method: str = "api_key"  # "api_key" | "oauth" | "none"
    key_last_rotated: Optional[float] = None  # monotonic timestamp
    key_expires_at: Optional[float] = None    # monotonic timestamp
    consecutive_auth_failures: int = 0
    last_auth_error: Optional[str] = None
    # Revocation fields — populated by AuthProvider.revoke()
    revocation_reason: Optional[str] = None
    revoked_at: Optional[float] = None        # monotonic timestamp

    def is_expired(self) -> bool:
        """Return True if the key has expired based on monotonic time."""
        if self.key_expires_at is None:
            return False
        return time.monotonic() > self.key_expires_at

    def is_revoked(self) -> bool:
        """Return True if this provider has been explicitly revoked."""
        return self.revoked_at is not None

    def is_healthy(self) -> bool:
        """Return True if the provider is authenticated, not expired, and below failure threshold."""
        return (
            self.is_authenticated
            and not self.is_expired()
            and not self.is_revoked()
            and self.consecutive_auth_failures < 3
        )


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for provider authentication managers."""

    def get_state(self, provider_name: str) -> AuthState: ...
    def record_auth_success(self, provider_name: str) -> None: ...
    def record_auth_failure(self, provider_name: str, error: str) -> None: ...
    def rotate_key(self, provider_name: str, new_key: str) -> None: ...
    def list_states(self) -> list[AuthState]: ...
    def health_check(self) -> bool: ...
    def revoke(self, provider_name: str, reason: str) -> None: ...
    def unrevoke(self, provider_name: str) -> None: ...
    def is_revoked(self, provider_name: str) -> bool: ...
