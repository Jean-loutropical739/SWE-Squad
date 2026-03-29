"""Issue tracker provider registry.

Resolves provider name → IssueTracker instance from config, so the core
agents never hardcode ``GitHubIssueTracker`` directly.

Usage::

    tracker = create_issue_tracker("github", config_dict)
    tracker = create_issue_tracker("jira", config_dict)  # future
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.issue_tracker.base import IssueTracker

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns an IssueTracker.
_REGISTRY: Dict[str, Any] = {}


def register_issue_tracker(name: str, factory: Any) -> None:
    """Register an issue tracker factory by name."""
    _REGISTRY[name] = factory


def _github_factory(config: Dict[str, Any]) -> IssueTracker:
    """Build a GitHubIssueTracker from config dict."""
    from src.swe_team.providers.issue_tracker.github_provider import (
        GitHubIssueTracker,
    )

    return GitHubIssueTracker(
        repo=config.get("repo", ""),
        token=config.get("token", ""),
    )


# Register built-in providers
register_issue_tracker("github", _github_factory)


def create_issue_tracker(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> IssueTracker:
    """Resolve an issue tracker provider by name.

    Args:
        provider_name: Provider name (e.g. 'github', 'jira', 'linear').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.issue_tracker``).

    Returns:
        A configured IssueTracker instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown issue tracker provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving issue tracker provider: %s", provider_name)
    return factory(config)


def list_issue_trackers() -> list[str]:
    """Return sorted list of registered issue tracker provider names."""
    return sorted(_REGISTRY.keys())
