"""Workspace provider registry.

Resolves provider name → WorkspaceProvider instance from config, so core
agents never hardcode ``GitWorktreeProvider`` directly.

Usage::

    workspace = create_workspace_provider("git-worktree", config_dict)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.workspace.base import WorkspaceProvider
from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a WorkspaceProvider.
_REGISTRY: Dict[str, Any] = {}


def register_workspace_provider(name: str, factory: Any) -> None:
    """Register a workspace provider factory by name."""
    _REGISTRY[name] = factory


def _git_worktree_factory(config: Dict[str, Any]) -> WorkspaceProvider:
    """Build a GitWorktreeProvider from config dict."""
    return GitWorktreeProvider(config=config)


# Register built-in providers
register_workspace_provider("git-worktree", _git_worktree_factory)


def create_workspace_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> WorkspaceProvider:
    """Resolve a workspace provider by name.

    Args:
        provider_name: Provider name (e.g. 'git-worktree').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.workspace``).

    Returns:
        A configured WorkspaceProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown workspace provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving workspace provider: %s", provider_name)
    return factory(config)


def list_workspace_providers() -> list[str]:
    """Return sorted list of registered workspace provider names."""
    return sorted(_REGISTRY.keys())


__all__ = [
    "GitWorktreeProvider",
    "create_workspace_provider",
    "list_workspace_providers",
    "register_workspace_provider",
]
