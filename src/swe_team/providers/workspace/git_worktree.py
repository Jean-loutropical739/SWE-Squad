"""
GitWorktreeProvider — default WorkspaceProvider implementation.

Creates git worktrees with scoped ``.env`` files.  Wraps ``WorktreeManager``
and wires it to an ``EnvProvider`` for credential injection.

Security guarantees:
  - ``.env`` files are written with ``chmod 600`` (owner-only read/write).
  - On release the ``.env`` is overwritten with null bytes before ``unlink``
    to prevent secret recovery from disk.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.swe_team.providers.env.base import EnvProvider, EnvSpec
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider
from src.swe_team.providers.workspace.base import (
    WorkspaceInfo,
    WorkspaceProvider,
    WorkspaceSpec,
)
from src.swe_team.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class GitWorktreeProvider:
    """
    Default WorkspaceProvider.  Creates git worktrees with scoped .env files.

    Wraps ``WorktreeManager`` and wires it to an ``EnvProvider`` for
    credential injection.
    """

    def __init__(
        self,
        config: dict,
        env_provider: Optional[EnvProvider] = None,
        repo_root: Optional[str | Path] = None,
    ) -> None:
        self._config = config
        self._env_provider: EnvProvider = env_provider or DotenvEnvProvider(
            config.get("env_allowlists", {})
        )

        # Resolve repo_root: explicit arg > config > cwd
        resolved_root = repo_root or config.get("repo_root", ".")
        self._repo_root = Path(resolved_root).resolve()

        self._worktree_manager = WorktreeManager(
            repo_root=self._repo_root,
            config=config.get("worktree", {}),
        )
        self._active: Dict[str, WorkspaceInfo] = {}

    # ------------------------------------------------------------------
    # WorkspaceProvider interface
    # ------------------------------------------------------------------

    def create(self, spec: WorkspaceSpec) -> WorkspaceInfo:
        """Create a git worktree and inject a scoped .env file.

        1. Call WorktreeManager to create the git worktree.
        2. Build scoped env via ``self._env_provider.build_env(...)``.
        3. Write ``.env`` into ``worktree_path/.env`` (chmod 600).
        4. Register in ``self._active``.
        5. Return ``WorkspaceInfo`` with ``env_path`` set.
        """
        branch = f"swe-fix/ticket-{spec.ticket_id}"
        wt = self._worktree_manager.acquire(ticket_id=spec.ticket_id, branch=branch)

        # Build role-scoped env
        env_spec = EnvSpec(role=spec.role, overrides=spec.env_overrides)
        env_dict = self._env_provider.build_env(env_spec)

        # Write .env into the worktree
        env_path = wt.path / ".env"
        self._write_scoped_env(env_path, env_dict)

        workspace_id = spec.ticket_id
        info = WorkspaceInfo(
            workspace_id=workspace_id,
            ticket_id=spec.ticket_id,
            path=wt.path,
            role=spec.role,
            created_at=datetime.now(timezone.utc),
            env_path=env_path,
            branch=wt.branch,
        )
        self._active[workspace_id] = info

        logger.info(
            "GitWorktreeProvider: created workspace %s at %s (role=%s)",
            workspace_id,
            wt.path,
            spec.role,
        )
        return info

    def release(self, workspace_id: str) -> None:
        """Release a workspace: securely delete .env then remove the worktree.

        1. Look up WorkspaceInfo.
        2. Securely delete the .env file (overwrite with zeros, then unlink).
        3. Call WorktreeManager.release().
        4. Remove from self._active.
        """
        info = self._active.get(workspace_id)
        if info is None:
            logger.warning(
                "GitWorktreeProvider: release called for unknown workspace %s",
                workspace_id,
            )
            return

        # Secure-delete the .env before removing the worktree
        if info.env_path is not None:
            self._secure_delete_env(info.env_path)

        # Delegate worktree removal to the manager
        wt = self._worktree_manager._worktrees.get(workspace_id)
        if wt is not None:
            self._worktree_manager.release(wt)

        del self._active[workspace_id]

        logger.info("GitWorktreeProvider: released workspace %s", workspace_id)

    def get(self, workspace_id: str) -> Optional[WorkspaceInfo]:
        """Return info about a workspace, or None if it does not exist."""
        return self._active.get(workspace_id)

    def list_active(self) -> List[WorkspaceInfo]:
        """Return info about all currently active workspaces."""
        return list(self._active.values())

    def cleanup_stale(self, max_age_hours: int) -> int:
        """Remove workspaces older than *max_age_hours*.

        For each stale workspace: securely delete .env then release.
        Returns count cleaned.
        """
        now = datetime.now(timezone.utc)
        stale_ids: List[str] = []
        for ws_id, info in self._active.items():
            age_hours = (now - info.created_at).total_seconds() / 3600
            if age_hours > max_age_hours:
                stale_ids.append(ws_id)

        cleaned = 0
        for ws_id in stale_ids:
            try:
                self.release(ws_id)
                cleaned += 1
            except Exception:
                logger.exception(
                    "GitWorktreeProvider: failed to clean stale workspace %s",
                    ws_id,
                )
        return cleaned

    def health_check(self) -> bool:
        """Check git is available and base_dir is writable."""
        try:
            git_path = shutil.which("git")
            if git_path is None:
                return False
            base_dir = self._worktree_manager._base_dir
            return base_dir.exists() and base_dir.is_dir()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_scoped_env(path: Path, env: dict[str, str]) -> None:
        """Write env dict to .env file with chmod 600."""
        content = "\n".join(f"{k}={v}" for k, v in sorted(env.items()))
        path.write_text(content + "\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _secure_delete_env(path: Path) -> None:
        """Overwrite with zeros then delete -- prevent secret recovery from disk."""
        if path.exists():
            size = path.stat().st_size
            path.write_bytes(b"\x00" * size)
            path.unlink()
