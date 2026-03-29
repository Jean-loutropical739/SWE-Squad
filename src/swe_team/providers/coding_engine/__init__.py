"""Coding engine provider registry.

Resolves engine name → CodingEngine instance from config, so the runner
never hardcodes ``ClaudeCodeEngine`` directly.

Usage::

    engine = resolve_engine("claude", config_dict)
    engine = resolve_engine("gemini", config_dict)  # future
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a CodingEngine.
_REGISTRY: Dict[str, Any] = {}


def register_engine(name: str, factory: Any) -> None:
    """Register a coding engine factory by name."""
    _REGISTRY[name] = factory


def _claude_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a ClaudeCodeEngine from config dict."""
    from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

    return ClaudeCodeEngine(
        default_model=config.get("default_model", "sonnet"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        binary=config.get("claude_path") or None,
        allowed_tools=config.get("allowed_tools") or None,
        dangerously_skip_permissions=config.get("skip_permissions", True),
    )


# Register built-in engines
register_engine("claude", _claude_factory)


def resolve_engine(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> CodingEngine:
    """Resolve a coding engine by provider name.

    Args:
        provider_name: Engine name (e.g. 'claude', 'gemini', 'opencode').
                       Must be registered in the engine registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.coding_engine``).

    Returns:
        A configured CodingEngine instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown coding engine provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving coding engine: %s", provider_name)
    return factory(config)


def list_engines() -> list[str]:
    """Return sorted list of registered engine provider names."""
    return sorted(_REGISTRY.keys())
