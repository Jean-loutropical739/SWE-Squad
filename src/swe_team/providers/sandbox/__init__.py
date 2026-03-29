"""Sandbox provider registry.

Resolves provider name → SandboxProvider instance from config, so core
agents never hardcode a specific sandbox backend directly.

Usage::

    sandbox = create_sandbox_provider("local", config_dict)
    sandbox = create_sandbox_provider("docker", config_dict)
    sandbox = create_sandbox_provider("proxmox", config_dict)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.sandbox.base import SandboxProvider

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a SandboxProvider.
_REGISTRY: Dict[str, Any] = {}


def register_sandbox_provider(name: str, factory: Any) -> None:
    """Register a sandbox provider factory by name."""
    _REGISTRY[name] = factory


def _local_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a LocalSandbox from config dict."""
    from src.swe_team.providers.sandbox.local import from_config

    return from_config(config)


def _docker_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a DockerSandbox from config dict."""
    from src.swe_team.providers.sandbox.docker import from_config

    return from_config(config)


def _proxmox_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a ProxmoxSandbox from config dict."""
    from src.swe_team.providers.sandbox.proxmox import from_config

    return from_config(config)


# Register built-in providers
register_sandbox_provider("local", _local_factory)
register_sandbox_provider("docker", _docker_factory)
register_sandbox_provider("proxmox", _proxmox_factory)


def create_sandbox_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> SandboxProvider:
    """Resolve a sandbox provider by name.

    Args:
        provider_name: Provider name (e.g. 'local', 'docker', 'proxmox').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.sandbox``).

    Returns:
        A configured SandboxProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown sandbox provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving sandbox provider: %s", provider_name)
    return factory(config)


def list_sandbox_providers() -> list[str]:
    """Return sorted list of registered sandbox provider names."""
    return sorted(_REGISTRY.keys())
