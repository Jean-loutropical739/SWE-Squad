"""Auth provider — per-provider authentication state tracking."""

from src.swe_team.providers.auth.base import AuthProvider, AuthState
from src.swe_team.providers.auth.inmemory_provider import InMemoryAuthProvider
from src.swe_team.providers.auth.github_oauth import GitHubOAuthProvider

__all__ = ["AuthProvider", "AuthState", "InMemoryAuthProvider", "GitHubOAuthProvider"]
