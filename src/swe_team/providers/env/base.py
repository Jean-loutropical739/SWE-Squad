"""
EnvProvider interface — pluggable scoped credential injection.

Implement this to swap between flat .env files, HashiCorp Vault,
AWS Secrets Manager, K8s Secrets, or any other secret store
without touching core agent code.

Roles control which environment variables an agent sees.
Blocked variables are never exposed unless the role explicitly
allowlists them (e.g. claude_cli needs ANTHROPIC_API_KEY).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


BLOCKED_ENV_VARS: frozenset[str] = frozenset({
    "SUPABASE_ANON_KEY",
    "BASE_LLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WEBHOOK_SECRET",
    "ANTHROPIC_API_KEY",
    "PROXMOXAI_API_KEY",
})

DEFAULT_ALLOWLISTS: dict[str, list[str]] = {
    "investigator": [
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
        "SWE_TEAM_ID", "SWE_REPO_PATH", "SWE_TEAM_CONFIG",
    ],
    "developer": [
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
        "SWE_TEAM_ID", "SWE_GITHUB_REPO", "SWE_GITHUB_ACCOUNT",
        "GH_TOKEN", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ],
    "test_runner": [
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
        "SWE_TEAM_ID",
        # Python
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "VIRTUAL_ENV",
        "PYTEST_CURRENT_TEST",
        # Node.js
        "NODE_ENV", "NODE_PATH", "NODE_OPTIONS", "NPM_CONFIG_PREFIX",
        # General CI
        "CI", "TERM", "TZ", "TMPDIR", "TEMP", "TMP",
        # Go
        "GOPATH", "GOROOT", "GOCACHE",
        # Test databases
        "DATABASE_URL", "TEST_DATABASE_URL", "REDIS_URL",
        # Coverage
        "COVERAGE_FILE", "COVERAGE_RCFILE",
    ],
    "reviewer": [
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
        "SWE_TEAM_ID", "SWE_GITHUB_REPO", "GH_TOKEN",
    ],
    "claude_cli": [
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
        "ANTHROPIC_API_KEY",   # Claude CLI needs this
        "SWE_TEAM_ID",
    ],
}


@dataclass
class EnvSpec:
    """Specification for building a scoped environment."""

    role: str                                                   # "investigator", "developer", "reviewer", "test_runner", "claude_cli"
    overrides: dict[str, str] = field(default_factory=dict)     # per-execution extras
    strip_blocked: bool = True                                  # remove BLOCKED_ENV_VARS from output


@runtime_checkable
class EnvProvider(Protocol):
    """
    Interface all environment/secret providers must implement.

    Providers are registered in config/swe_team.yaml under providers.env.
    The active provider is loaded by name — no core code changes required
    when switching backends.
    """

    def build_env(self, spec: EnvSpec) -> dict[str, str]:
        """Build a complete environment dict scoped to the given role and overrides."""
        ...

    def allowed_keys(self, role: str) -> list[str]:
        """Return the list of env var keys that the given role is permitted to access."""
        ...

    def is_blocked(self, key: str) -> bool:
        """Return True if the key is in the global block list and must never be exposed."""
        ...

    def health_check(self) -> bool:
        """Return True if the secret backend is reachable and properly configured."""
        ...
