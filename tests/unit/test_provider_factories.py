"""Unit tests for provider factory/registry functions.

Tests that each domain's factory:
  - Resolves its default provider correctly
  - Raises ValueError with available providers listed for unknown names
  - list_*() returns the expected available provider names
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class TestNotificationProviderFactory:
    def test_create_default_telegram(self):
        from src.swe_team.providers.notification import create_notification_provider
        provider = create_notification_provider("telegram", {"token": "t", "chat_id": "c"})
        assert provider is not None
        assert provider.name == "telegram"

    def test_create_unknown_raises_value_error(self):
        from src.swe_team.providers.notification import create_notification_provider
        with pytest.raises(ValueError, match="Unknown notification provider 'slack'"):
            create_notification_provider("slack")

    def test_unknown_error_lists_available(self):
        from src.swe_team.providers.notification import create_notification_provider
        with pytest.raises(ValueError, match="telegram"):
            create_notification_provider("nonexistent")

    def test_list_returns_telegram(self):
        from src.swe_team.providers.notification import list_notification_providers
        providers = list_notification_providers()
        assert "telegram" in providers

    def test_list_is_sorted(self):
        from src.swe_team.providers.notification import list_notification_providers
        providers = list_notification_providers()
        assert providers == sorted(providers)

    def test_create_with_empty_config(self):
        from src.swe_team.providers.notification import create_notification_provider
        provider = create_notification_provider("telegram")
        assert provider is not None

    def test_register_custom_provider(self):
        from src.swe_team.providers.notification import (
            create_notification_provider,
            list_notification_providers,
            register_notification_provider,
        )

        class _FakeProvider:
            @property
            def name(self) -> str:
                return "fake-notif"

            def send_alert(self, message, *, level="info"):
                return True

            def send_daily_summary(self, summary):
                return True

            def send_hitl_escalation(self, ticket_id, message):
                return True

            def health_check(self):
                return True

        register_notification_provider("fake-notif", lambda cfg: _FakeProvider())
        provider = create_notification_provider("fake-notif")
        assert provider.name == "fake-notif"
        assert "fake-notif" in list_notification_providers()


# ---------------------------------------------------------------------------
# Issue Tracker
# ---------------------------------------------------------------------------

class TestIssueTrackerFactory:
    def test_create_default_github(self):
        from src.swe_team.providers.issue_tracker import create_issue_tracker
        tracker = create_issue_tracker("github", {"repo": "owner/repo"})
        assert tracker is not None
        assert tracker.name == "github"

    def test_create_unknown_raises_value_error(self):
        from src.swe_team.providers.issue_tracker import create_issue_tracker
        with pytest.raises(ValueError, match="Unknown issue tracker provider 'jira'"):
            create_issue_tracker("jira")

    def test_unknown_error_lists_available(self):
        from src.swe_team.providers.issue_tracker import create_issue_tracker
        with pytest.raises(ValueError, match="github"):
            create_issue_tracker("nonexistent")

    def test_list_returns_github(self):
        from src.swe_team.providers.issue_tracker import list_issue_trackers
        trackers = list_issue_trackers()
        assert "github" in trackers

    def test_list_is_sorted(self):
        from src.swe_team.providers.issue_tracker import list_issue_trackers
        trackers = list_issue_trackers()
        assert trackers == sorted(trackers)

    def test_create_with_empty_config(self):
        from src.swe_team.providers.issue_tracker import create_issue_tracker
        tracker = create_issue_tracker("github")
        assert tracker is not None

    def test_register_custom_tracker(self):
        from src.swe_team.providers.issue_tracker import (
            create_issue_tracker,
            list_issue_trackers,
            register_issue_tracker,
        )

        class _FakeTracker:
            @property
            def name(self) -> str:
                return "fake-tracker"

            def create_issue(self, title, body, *, labels=None, assignee=None):
                from src.swe_team.providers.issue_tracker.base import IssueRef
                return IssueRef(issue_id="1", url="http://example.com/1", title=title)

            def comment(self, issue_id, body):
                return True

            def close_issue(self, issue_id, *, reason=""):
                return True

            def find_existing(self, title_substring):
                return []

            def health_check(self):
                return True

        register_issue_tracker("fake-tracker", lambda cfg: _FakeTracker())
        tracker = create_issue_tracker("fake-tracker")
        assert tracker.name == "fake-tracker"
        assert "fake-tracker" in list_issue_trackers()


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class TestSandboxProviderFactory:
    def test_create_local(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider
        sandbox = create_sandbox_provider("local")
        assert sandbox is not None
        assert sandbox.name == "local"

    def test_create_docker(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider
        sandbox = create_sandbox_provider("docker")
        assert sandbox is not None
        assert sandbox.name == "docker"

    def test_create_proxmox_missing_gateway_raises(self):
        """ProxmoxSandbox requires gateway_url; missing key raises KeyError."""
        from src.swe_team.providers.sandbox import create_sandbox_provider
        with pytest.raises((KeyError, Exception)):
            create_sandbox_provider("proxmox", {})

    def test_create_proxmox_with_config(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider
        sandbox = create_sandbox_provider(
            "proxmox",
            {"gateway_url": "http://localhost:8080", "api_key": "test-key"},
        )
        assert sandbox is not None
        assert sandbox.name == "proxmox"

    def test_create_unknown_raises_value_error(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider
        with pytest.raises(ValueError, match="Unknown sandbox provider 'kubernetes'"):
            create_sandbox_provider("kubernetes")

    def test_unknown_error_lists_available(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider
        with pytest.raises(ValueError, match="docker"):
            create_sandbox_provider("nonexistent")

    def test_list_returns_all_builtin(self):
        from src.swe_team.providers.sandbox import list_sandbox_providers
        providers = list_sandbox_providers()
        assert "local" in providers
        assert "docker" in providers
        assert "proxmox" in providers

    def test_list_is_sorted(self):
        from src.swe_team.providers.sandbox import list_sandbox_providers
        providers = list_sandbox_providers()
        assert providers == sorted(providers)

    def test_register_custom_sandbox(self):
        from src.swe_team.providers.sandbox import (
            create_sandbox_provider,
            list_sandbox_providers,
            register_sandbox_provider,
        )
        from src.swe_team.providers.sandbox.base import SandboxInfo, SandboxSpec

        class _FakeSandbox:
            name = "fake-sandbox"

            def create(self, spec: SandboxSpec) -> SandboxInfo:
                return SandboxInfo(sandbox_id="fake", name=spec.name, ip=None,
                                   status="running", provider=self.name)

            def status(self, sandbox_id):
                return SandboxInfo(sandbox_id=sandbox_id, name="fake", ip=None,
                                   status="running", provider=self.name)

            def run_command(self, sandbox_id, command):
                return 0, "", ""

            def snapshot(self, sandbox_id, label):
                return label

            def rollback(self, sandbox_id, label):
                pass

            def delete(self, sandbox_id):
                pass

            def health_check(self):
                return True

        register_sandbox_provider("fake-sandbox", lambda cfg: _FakeSandbox())
        sandbox = create_sandbox_provider("fake-sandbox")
        assert sandbox.name == "fake-sandbox"
        assert "fake-sandbox" in list_sandbox_providers()


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class TestWorkspaceProviderFactory:
    def test_create_git_worktree(self):
        from src.swe_team.providers.workspace import create_workspace_provider
        provider = create_workspace_provider("git-worktree", {})
        assert provider is not None
        # GitWorktreeProvider does not expose a `.name` property in its protocol,
        # but we can verify the type
        from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider
        assert isinstance(provider, GitWorktreeProvider)

    def test_create_unknown_raises_value_error(self):
        from src.swe_team.providers.workspace import create_workspace_provider
        with pytest.raises(ValueError, match="Unknown workspace provider 'docker-volume'"):
            create_workspace_provider("docker-volume")

    def test_unknown_error_lists_available(self):
        from src.swe_team.providers.workspace import create_workspace_provider
        with pytest.raises(ValueError, match="git-worktree"):
            create_workspace_provider("nonexistent")

    def test_list_returns_git_worktree(self):
        from src.swe_team.providers.workspace import list_workspace_providers
        providers = list_workspace_providers()
        assert "git-worktree" in providers

    def test_list_is_sorted(self):
        from src.swe_team.providers.workspace import list_workspace_providers
        providers = list_workspace_providers()
        assert providers == sorted(providers)

    def test_create_with_empty_config(self):
        from src.swe_team.providers.workspace import create_workspace_provider
        provider = create_workspace_provider("git-worktree")
        assert provider is not None

    def test_register_custom_workspace(self):
        from src.swe_team.providers.workspace import (
            create_workspace_provider,
            list_workspace_providers,
            register_workspace_provider,
        )

        class _FakeWorkspace:
            def create(self, spec):
                pass

            def release(self, workspace_id):
                pass

            def get(self, workspace_id):
                return None

            def list_active(self):
                return []

            def cleanup_stale(self, max_age_hours):
                return 0

            def health_check(self):
                return True

        register_workspace_provider("fake-workspace", lambda cfg: _FakeWorkspace())
        provider = create_workspace_provider("fake-workspace")
        assert isinstance(provider, _FakeWorkspace)
        assert "fake-workspace" in list_workspace_providers()


# ---------------------------------------------------------------------------
# Repo Map
# ---------------------------------------------------------------------------

class TestRepoMapProviderFactory:
    def test_create_ctags(self):
        from src.swe_team.providers.repomap import create_repomap_provider
        provider = create_repomap_provider("ctags")
        assert provider is not None
        from src.swe_team.providers.repomap.ctags_provider import CtagsRepoMapProvider
        assert isinstance(provider, CtagsRepoMapProvider)

    def test_create_unknown_raises_value_error(self):
        from src.swe_team.providers.repomap import create_repomap_provider
        with pytest.raises(ValueError, match="Unknown repo map provider 'tree-sitter'"):
            create_repomap_provider("tree-sitter")

    def test_unknown_error_lists_available(self):
        from src.swe_team.providers.repomap import create_repomap_provider
        with pytest.raises(ValueError, match="ctags"):
            create_repomap_provider("nonexistent")

    def test_list_returns_ctags(self):
        from src.swe_team.providers.repomap import list_repomap_providers
        providers = list_repomap_providers()
        assert "ctags" in providers

    def test_list_is_sorted(self):
        from src.swe_team.providers.repomap import list_repomap_providers
        providers = list_repomap_providers()
        assert providers == sorted(providers)

    def test_create_with_config(self):
        from src.swe_team.providers.repomap import create_repomap_provider
        provider = create_repomap_provider("ctags", {"some_option": True})
        assert provider is not None

    def test_register_custom_repomap(self):
        from pathlib import Path

        from src.swe_team.providers.repomap import (
            create_repomap_provider,
            list_repomap_providers,
            register_repomap_provider,
        )
        from src.swe_team.providers.repomap.base import RepoMap

        class _FakeRepoMap:
            def generate(self, repo_path: Path, max_tokens=2000, ignore=None) -> RepoMap:
                return RepoMap(repo_path=str(repo_path))

            def is_available(self) -> bool:
                return True

            def health_check(self) -> bool:
                return True

        register_repomap_provider("fake-repomap", lambda cfg: _FakeRepoMap())
        provider = create_repomap_provider("fake-repomap")
        assert isinstance(provider, _FakeRepoMap)
        assert "fake-repomap" in list_repomap_providers()
