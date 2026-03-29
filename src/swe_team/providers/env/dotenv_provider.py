"""
DotenvEnvProvider — default EnvProvider implementation.

Reads from ``os.environ`` (already loaded from ``.env`` by python-dotenv).
Applies role-based allowlists from config.  Always strips BLOCKED_ENV_VARS
unless the role explicitly allowlists them (e.g. ``claude_cli`` needs
``ANTHROPIC_API_KEY``).
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from src.swe_team.providers.env.base import (
    BLOCKED_ENV_VARS,
    DEFAULT_ALLOWLISTS,
    EnvProvider,
    EnvSpec,
)


class DotenvEnvProvider:
    """
    Default EnvProvider.  Reads from os.environ (already loaded from .env
    by python-dotenv).  Applies role-based allowlists from config.  Always
    strips BLOCKED_ENV_VARS unless the role explicitly allowlists them.
    """

    def __init__(
        self,
        config_allowlists: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        # Merge DEFAULT_ALLOWLISTS with any config overrides
        self._allowlists: Dict[str, List[str]] = {}
        for role, keys in DEFAULT_ALLOWLISTS.items():
            self._allowlists[role] = list(keys)

        if config_allowlists:
            for role, keys in config_allowlists.items():
                if role in self._allowlists:
                    # Merge: config extends (not replaces) the defaults
                    existing = set(self._allowlists[role])
                    for k in keys:
                        if k not in existing:
                            self._allowlists[role].append(k)
                            existing.add(k)
                else:
                    self._allowlists[role] = list(keys)

    def build_env(self, spec: EnvSpec) -> dict[str, str]:
        """
        Build a minimal env dict for a subprocess.

        1. Start with allowed keys for the role from os.environ
        2. Apply overrides (per-execution extras)
        3. Strip any BLOCKED_ENV_VARS that are not in the role's allowlist
        4. Always include PATH and HOME minimally
        """
        role_keys = set(self._allowlists.get(spec.role, []))

        # Unknown roles get minimal PATH + HOME only
        if not role_keys:
            role_keys = {"PATH", "HOME"}

        env: dict[str, str] = {}

        # Step 1: pull allowed keys from os.environ
        for key in role_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        # Step 2: apply per-execution overrides
        if spec.overrides:
            env.update(spec.overrides)

        # Step 3: strip blocked vars that are NOT in the role's explicit allowlist
        if spec.strip_blocked:
            for blocked_key in BLOCKED_ENV_VARS:
                if blocked_key in env and blocked_key not in role_keys:
                    del env[blocked_key]

        # Step 4: ensure PATH and HOME are always present
        if "PATH" not in env:
            env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        if "HOME" not in env:
            env["HOME"] = os.environ.get("HOME", "/tmp")

        return env

    def allowed_keys(self, role: str) -> list[str]:
        """Return the list of env var keys that the given role is permitted to access."""
        return list(self._allowlists.get(role, ["PATH", "HOME"]))

    def is_blocked(self, key: str) -> bool:
        """Return True if the key is in the global block list."""
        return key in BLOCKED_ENV_VARS

    def health_check(self) -> bool:
        """Return True — dotenv provider is always available (reads os.environ)."""
        return True
