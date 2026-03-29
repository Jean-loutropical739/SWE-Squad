"""
WorkspaceProvider interface — pluggable isolated workspace management.

Implement this to swap between git worktrees, Docker volumes, cloud VMs,
or any other workspace isolation strategy without touching core agent code.

Wraps the full lifecycle: create, inject credentials, release, cleanup stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class WorkspaceSpec:
    """Specification for creating a new workspace."""

    ticket_id: str
    role: str = "developer"
    base_dir: Path | None = None                                # override default storage path
    ttl_hours: int = 48
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkspaceInfo:
    """Runtime info about a provisioned workspace."""

    workspace_id: str
    ticket_id: str
    path: Path
    role: str
    created_at: datetime
    env_path: Path | None = None                                # path to injected .env, None if not applicable
    branch: str | None = None


@runtime_checkable
class WorkspaceProvider(Protocol):
    """
    Interface all workspace providers must implement.

    Providers are registered in config/swe_team.yaml under providers.workspace.
    The active provider is loaded by name — no core code changes required
    when switching backends.
    """

    def create(self, spec: WorkspaceSpec) -> WorkspaceInfo:
        """Create and provision a new isolated workspace for the given ticket."""
        ...

    def release(self, workspace_id: str) -> None:
        """Release a workspace, removing its working tree and any injected credentials."""
        ...

    def get(self, workspace_id: str) -> WorkspaceInfo | None:
        """Return info about a workspace, or None if it does not exist."""
        ...

    def list_active(self) -> list[WorkspaceInfo]:
        """Return info about all currently active workspaces."""
        ...

    def cleanup_stale(self, max_age_hours: int) -> int:
        """Remove workspaces older than max_age_hours. Returns count cleaned."""
        ...

    def health_check(self) -> bool:
        """Return True if the workspace backend is reachable and properly configured."""
        ...
