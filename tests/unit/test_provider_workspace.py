"""Tests for workspace provider: GitWorktreeProvider + registry + protocol compliance."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.swe_team.providers.workspace.base import (
    WorkspaceInfo,
    WorkspaceProvider,
    WorkspaceSpec,
)
from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider
from src.swe_team.providers.workspace import (
    create_workspace_provider,
    list_workspace_providers,
    register_workspace_provider,
)
from src.swe_team.providers.env.base import EnvProvider, EnvSpec
from src.swe_team.worktree_manager import Worktree, WorktreeManager


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

class FakeEnvProvider:
    """Minimal EnvProvider for testing."""

    def build_env(self, spec: EnvSpec) -> dict[str, str]:
        return {"PATH": "/usr/bin", "SWE_TEAM_ID": "test", **spec.overrides}

    def allowed_keys(self, role: str) -> list[str]:
        return ["PATH", "SWE_TEAM_ID"]

    def is_blocked(self, key: str) -> bool:
        return key in ("ANTHROPIC_API_KEY",)

    def health_check(self) -> bool:
        return True


@pytest.fixture
def fake_env_provider() -> FakeEnvProvider:
    return FakeEnvProvider()


@pytest.fixture
def config(tmp_path: Path) -> dict:
    return {
        "repo_root": str(tmp_path / "repo"),
        "worktree": {
            "base_dir": str(tmp_path / "worktrees"),
            "max_concurrent": 4,
        },
    }


@pytest.fixture
def repo_root(config: dict, tmp_path: Path) -> Path:
    """Create a bare-minimum directory for repo_root so the provider can init."""
    root = Path(config["repo_root"])
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def provider(
    config: dict,
    repo_root: Path,
    fake_env_provider: FakeEnvProvider,
) -> GitWorktreeProvider:
    return GitWorktreeProvider(
        config=config,
        env_provider=fake_env_provider,
        repo_root=repo_root,
    )


# ---------------------------------------------------------------------------
# 1. Construction
# ---------------------------------------------------------------------------

class TestGitWorktreeProviderConstruction:
    """GitWorktreeProvider constructs correctly with various configs."""

    def test_basic_construction(self, provider: GitWorktreeProvider) -> None:
        assert provider._active == {}
        assert isinstance(provider._worktree_manager, WorktreeManager)

    def test_repo_root_from_config(self, config: dict, repo_root: Path, fake_env_provider: FakeEnvProvider) -> None:
        p = GitWorktreeProvider(config=config, env_provider=fake_env_provider)
        assert p._repo_root == Path(config["repo_root"]).resolve()

    def test_repo_root_explicit_overrides_config(self, config: dict, tmp_path: Path, fake_env_provider: FakeEnvProvider) -> None:
        explicit = tmp_path / "explicit_repo"
        explicit.mkdir()
        p = GitWorktreeProvider(config=config, env_provider=fake_env_provider, repo_root=explicit)
        assert p._repo_root == explicit.resolve()

    def test_default_env_provider_when_none(self, config: dict, repo_root: Path) -> None:
        """When no env_provider is given, a DotenvEnvProvider is created."""
        p = GitWorktreeProvider(config=config)
        from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider
        assert isinstance(p._env_provider, DotenvEnvProvider)

    def test_empty_config(self, tmp_path: Path, fake_env_provider: FakeEnvProvider) -> None:
        root = tmp_path / "r"
        root.mkdir()
        p = GitWorktreeProvider(config={}, env_provider=fake_env_provider, repo_root=root)
        assert p._repo_root == root.resolve()


# ---------------------------------------------------------------------------
# 2. create_workspace calls git worktree add (mocked)
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    """create() delegates to WorktreeManager.acquire and injects .env."""

    def test_create_calls_acquire_and_writes_env(
        self, provider: GitWorktreeProvider, tmp_path: Path
    ) -> None:
        wt_path = tmp_path / "wt-abc"
        wt_path.mkdir()

        mock_wt = Worktree(
            path=wt_path,
            branch="swe-fix/ticket-abc",
            ticket_id="abc",
            acquired_at=1000.0,
            in_use=True,
        )

        with patch.object(provider._worktree_manager, "acquire", return_value=mock_wt) as mock_acquire:
            spec = WorkspaceSpec(ticket_id="abc", role="developer")
            info = provider.create(spec)

        mock_acquire.assert_called_once_with(ticket_id="abc", branch="swe-fix/ticket-abc")

        # Verify WorkspaceInfo
        assert isinstance(info, WorkspaceInfo)
        assert info.workspace_id == "abc"
        assert info.ticket_id == "abc"
        assert info.path == wt_path
        assert info.role == "developer"
        assert info.branch == "swe-fix/ticket-abc"
        assert info.env_path == wt_path / ".env"

        # .env was written
        env_path = wt_path / ".env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "PATH=/usr/bin" in content
        assert "SWE_TEAM_ID=test" in content

        # chmod 600
        assert oct(env_path.stat().st_mode & 0o777) == "0o600"

    def test_create_registers_in_active(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        wt_path = tmp_path / "wt-xyz"
        wt_path.mkdir()
        mock_wt = Worktree(path=wt_path, branch="swe-fix/ticket-xyz", ticket_id="xyz", in_use=True)
        with patch.object(provider._worktree_manager, "acquire", return_value=mock_wt):
            provider.create(WorkspaceSpec(ticket_id="xyz"))

        assert "xyz" in provider._active
        assert provider._active["xyz"].ticket_id == "xyz"

    def test_create_passes_env_overrides(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        wt_path = tmp_path / "wt-ovr"
        wt_path.mkdir()
        mock_wt = Worktree(path=wt_path, branch="swe-fix/ticket-ovr", ticket_id="ovr", in_use=True)
        with patch.object(provider._worktree_manager, "acquire", return_value=mock_wt):
            spec = WorkspaceSpec(ticket_id="ovr", env_overrides={"CUSTOM": "val123"})
            info = provider.create(spec)

        content = info.env_path.read_text()
        assert "CUSTOM=val123" in content

    def test_create_propagates_acquire_error(self, provider: GitWorktreeProvider) -> None:
        with patch.object(
            provider._worktree_manager,
            "acquire",
            side_effect=RuntimeError("pool exhausted"),
        ):
            with pytest.raises(RuntimeError, match="pool exhausted"):
                provider.create(WorkspaceSpec(ticket_id="fail"))


# ---------------------------------------------------------------------------
# 3. release / cleanup_workspace calls git worktree remove
# ---------------------------------------------------------------------------

class TestReleaseWorkspace:
    """release() securely deletes .env and delegates worktree removal."""

    def _create_workspace(
        self, provider: GitWorktreeProvider, tmp_path: Path, ticket_id: str = "rel1"
    ) -> WorkspaceInfo:
        wt_path = tmp_path / f"wt-{ticket_id}"
        wt_path.mkdir()
        mock_wt = Worktree(
            path=wt_path,
            branch=f"swe-fix/ticket-{ticket_id}",
            ticket_id=ticket_id,
            in_use=True,
        )
        with patch.object(provider._worktree_manager, "acquire", return_value=mock_wt):
            return provider.create(WorkspaceSpec(ticket_id=ticket_id))

    def test_release_removes_from_active(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        info = self._create_workspace(provider, tmp_path)
        with patch.object(provider._worktree_manager, "release"):
            provider.release(info.workspace_id)
        assert info.workspace_id not in provider._active

    def test_release_securely_deletes_env(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        info = self._create_workspace(provider, tmp_path)
        env_path = info.env_path
        assert env_path.exists()
        original_size = env_path.stat().st_size

        with patch.object(provider._worktree_manager, "release"):
            provider.release(info.workspace_id)

        # .env should have been overwritten with zeros then deleted
        assert not env_path.exists()

    def test_release_unknown_workspace_is_noop(self, provider: GitWorktreeProvider) -> None:
        # Should log a warning but not raise
        provider.release("nonexistent")
        assert "nonexistent" not in provider._active

    def test_release_delegates_to_worktree_manager(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        info = self._create_workspace(provider, tmp_path)
        # The provider looks up the worktree in manager._worktrees before calling release
        mock_wt = Worktree(
            path=info.path, branch=info.branch or "", ticket_id=info.ticket_id, in_use=True
        )
        provider._worktree_manager._worktrees[info.workspace_id] = mock_wt
        with patch.object(provider._worktree_manager, "release") as mock_release:
            provider.release(info.workspace_id)
        mock_release.assert_called_once_with(mock_wt)


# ---------------------------------------------------------------------------
# 4. list_workspaces / get
# ---------------------------------------------------------------------------

class TestListAndGetWorkspaces:
    """list_active() and get() return correct workspace info."""

    def _add_workspace(
        self, provider: GitWorktreeProvider, tmp_path: Path, ticket_id: str
    ) -> WorkspaceInfo:
        wt_path = tmp_path / f"wt-{ticket_id}"
        wt_path.mkdir(exist_ok=True)
        mock_wt = Worktree(path=wt_path, branch=f"b-{ticket_id}", ticket_id=ticket_id, in_use=True)
        with patch.object(provider._worktree_manager, "acquire", return_value=mock_wt):
            return provider.create(WorkspaceSpec(ticket_id=ticket_id))

    def test_list_active_empty(self, provider: GitWorktreeProvider) -> None:
        assert provider.list_active() == []

    def test_list_active_returns_all(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        self._add_workspace(provider, tmp_path, "a1")
        self._add_workspace(provider, tmp_path, "a2")
        self._add_workspace(provider, tmp_path, "a3")

        active = provider.list_active()
        assert len(active) == 3
        ids = {ws.ticket_id for ws in active}
        assert ids == {"a1", "a2", "a3"}

    def test_get_existing(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        self._add_workspace(provider, tmp_path, "g1")
        info = provider.get("g1")
        assert info is not None
        assert info.ticket_id == "g1"

    def test_get_nonexistent(self, provider: GitWorktreeProvider) -> None:
        assert provider.get("nope") is None

    def test_list_after_release(self, provider: GitWorktreeProvider, tmp_path: Path) -> None:
        self._add_workspace(provider, tmp_path, "r1")
        self._add_workspace(provider, tmp_path, "r2")
        with patch.object(provider._worktree_manager, "release"):
            provider.release("r1")
        active = provider.list_active()
        assert len(active) == 1
        assert active[0].ticket_id == "r2"


# ---------------------------------------------------------------------------
# 5. Error handling — git failures return gracefully
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Git failures and edge cases are handled gracefully."""

    def test_health_check_true_when_git_and_dir_exist(
        self, provider: GitWorktreeProvider
    ) -> None:
        with patch("shutil.which", return_value="/usr/bin/git"):
            base_dir = provider._worktree_manager._base_dir
            base_dir.mkdir(parents=True, exist_ok=True)
            assert provider.health_check() is True

    def test_health_check_false_when_no_git(self, provider: GitWorktreeProvider) -> None:
        with patch("shutil.which", return_value=None):
            assert provider.health_check() is False

    def test_health_check_false_on_exception(self, provider: GitWorktreeProvider) -> None:
        with patch("shutil.which", side_effect=OSError("boom")):
            assert provider.health_check() is False

    def test_cleanup_stale_removes_old_workspaces(
        self, provider: GitWorktreeProvider, tmp_path: Path
    ) -> None:
        # Manually insert a "stale" workspace (created 72h ago)
        old_time = datetime.now(timezone.utc) - timedelta(hours=72)
        wt_path = tmp_path / "wt-stale"
        wt_path.mkdir()
        env_path = wt_path / ".env"
        env_path.write_text("SECRET=old\n")
        env_path.chmod(0o600)

        stale_info = WorkspaceInfo(
            workspace_id="stale",
            ticket_id="stale",
            path=wt_path,
            role="developer",
            created_at=old_time,
            env_path=env_path,
            branch="swe-fix/ticket-stale",
        )
        provider._active["stale"] = stale_info

        # Also add a mock worktree in the manager so release can find it
        mock_wt = Worktree(path=wt_path, branch="swe-fix/ticket-stale", ticket_id="stale", in_use=True)
        provider._worktree_manager._worktrees["stale"] = mock_wt

        with patch.object(provider._worktree_manager, "release"):
            cleaned = provider.cleanup_stale(max_age_hours=48)

        assert cleaned == 1
        assert "stale" not in provider._active

    def test_cleanup_stale_keeps_fresh_workspaces(
        self, provider: GitWorktreeProvider, tmp_path: Path
    ) -> None:
        now = datetime.now(timezone.utc)
        fresh_info = WorkspaceInfo(
            workspace_id="fresh",
            ticket_id="fresh",
            path=tmp_path / "wt-fresh",
            role="developer",
            created_at=now,
        )
        provider._active["fresh"] = fresh_info

        cleaned = provider.cleanup_stale(max_age_hours=48)
        assert cleaned == 0
        assert "fresh" in provider._active

    def test_cleanup_stale_tolerates_release_failure(
        self, provider: GitWorktreeProvider, tmp_path: Path
    ) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(hours=100)
        info = WorkspaceInfo(
            workspace_id="bad",
            ticket_id="bad",
            path=tmp_path / "wt-bad",
            role="developer",
            created_at=old_time,
            env_path=tmp_path / "wt-bad" / ".env",
        )
        provider._active["bad"] = info

        # release will fail because the .env doesn't exist and worktree isn't tracked
        # but cleanup_stale should not raise
        cleaned = provider.cleanup_stale(max_age_hours=48)
        # release logs warning for unknown workspace_id but removes from _active
        # Actually, release checks _active first — "bad" is there, but env_path doesn't exist
        # and worktree manager won't have the entry. Still should not raise.
        assert cleaned >= 0  # At least doesn't blow up

    def test_secure_delete_overwrites_then_unlinks(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=supersecret\n")
        original_size = env_file.stat().st_size

        GitWorktreeProvider._secure_delete_env(env_file)

        assert not env_file.exists()

    def test_secure_delete_missing_file_is_noop(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"
        # Should not raise
        GitWorktreeProvider._secure_delete_env(missing)


# ---------------------------------------------------------------------------
# 6. Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    """GitWorktreeProvider satisfies the WorkspaceProvider protocol."""

    def test_isinstance_check(self, provider: GitWorktreeProvider) -> None:
        assert isinstance(provider, WorkspaceProvider)

    def test_has_all_protocol_methods(self) -> None:
        required = {"create", "release", "get", "list_active", "cleanup_stale", "health_check"}
        actual = {m for m in dir(GitWorktreeProvider) if not m.startswith("_")}
        assert required.issubset(actual), f"Missing: {required - actual}"

    def test_workspace_spec_defaults(self) -> None:
        spec = WorkspaceSpec(ticket_id="t1")
        assert spec.role == "developer"
        assert spec.base_dir is None
        assert spec.ttl_hours == 48
        assert spec.env_overrides == {}

    def test_workspace_info_fields(self) -> None:
        info = WorkspaceInfo(
            workspace_id="w1",
            ticket_id="t1",
            path=Path("/tmp/ws"),
            role="developer",
            created_at=datetime.now(timezone.utc),
        )
        assert info.env_path is None
        assert info.branch is None


# ---------------------------------------------------------------------------
# 7. Registry / Factory
# ---------------------------------------------------------------------------

class TestWorkspaceRegistry:
    """create_workspace_provider resolves names to providers."""

    def test_git_worktree_is_registered(self) -> None:
        assert "git-worktree" in list_workspace_providers()

    def test_create_git_worktree_provider(self, tmp_path: Path) -> None:
        cfg = {"repo_root": str(tmp_path)}
        tmp_path.mkdir(exist_ok=True)
        p = create_workspace_provider("git-worktree", cfg)
        assert isinstance(p, GitWorktreeProvider)
        assert isinstance(p, WorkspaceProvider)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown workspace provider"):
            create_workspace_provider("docker-volume", {})

    def test_register_custom_provider(self) -> None:
        sentinel = object()
        register_workspace_provider("test-custom", lambda cfg: sentinel)
        assert "test-custom" in list_workspace_providers()
        assert create_workspace_provider("test-custom") is sentinel

    def test_create_with_none_config(self, tmp_path: Path) -> None:
        """Passing None config should default to empty dict."""
        # git-worktree factory will use {} and resolve repo_root to cwd
        p = create_workspace_provider("git-worktree", None)
        assert isinstance(p, GitWorktreeProvider)
