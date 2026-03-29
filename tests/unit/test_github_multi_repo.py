"""Unit tests for src.swe_team.github_multi_repo and related dashboard integration."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.github_multi_repo import (
    MultiRepoAggregation,
    MultiRepoIssueAggregator,
    RepoIssueCount,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(number: int = 1, title: str = "Test", url: str = "") -> dict:
    return {
        "number": number,
        "title": title,
        "url": url or f"https://github.com/owner/repo/issues/{number}",
        "labels": [],
        "state": "OPEN",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }


def _proc(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def _agg(repos: List[str], max_issues: int = 100) -> MultiRepoIssueAggregator:
    return MultiRepoIssueAggregator(repos=repos, max_issues_per_repo=max_issues)


# ---------------------------------------------------------------------------
# RepoIssueCount dataclass
# ---------------------------------------------------------------------------

class TestRepoIssueCount:
    def test_defaults(self):
        rc = RepoIssueCount(repo="owner/repo", open_count=5)
        assert rc.issues == []
        assert rc.error is None

    def test_with_error(self):
        rc = RepoIssueCount(repo="owner/repo", open_count=0, error="timeout")
        assert rc.error == "timeout"


# ---------------------------------------------------------------------------
# MultiRepoAggregation dataclass
# ---------------------------------------------------------------------------

class TestMultiRepoAggregation:
    def test_defaults(self):
        agg = MultiRepoAggregation()
        assert agg.total_open == 0
        assert agg.orphaned_issues == []
        assert agg.linked_issues == []
        assert agg.by_repo == {}


# ---------------------------------------------------------------------------
# MultiRepoIssueAggregator — construction
# ---------------------------------------------------------------------------

class TestAggregatorInit:
    def test_empty_repos_filtered(self):
        agg = MultiRepoIssueAggregator(repos=["", "owner/repo", ""])
        assert agg._repos == ["owner/repo"]

    def test_no_repos(self):
        agg = MultiRepoIssueAggregator(repos=[])
        assert agg._repos == []


# ---------------------------------------------------------------------------
# aggregate() — no repos
# ---------------------------------------------------------------------------

class TestAggregateNoRepos:
    def test_empty_repos_returns_zero_totals(self):
        agg = _agg(repos=[])
        result = agg.aggregate()
        assert result.total_open == 0
        assert result.by_repo == {}
        assert result.orphaned_issues == []
        assert result.linked_issues == []


# ---------------------------------------------------------------------------
# aggregate() — single repo happy path
# ---------------------------------------------------------------------------

class TestAggregateSingleRepo:
    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_counts_open_issues(self, mock_run):
        issues = [_issue(1), _issue(2), _issue(3)]
        mock_run.return_value = _proc(json.dumps(issues))
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 3
        assert result.by_repo == {"owner/repo": 3}

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_empty_repo_returns_zero(self, mock_run):
        mock_run.return_value = _proc("[]")
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 0
        assert result.by_repo == {"owner/repo": 0}

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_all_issues_orphaned_when_no_fingerprints(self, mock_run):
        issues = [_issue(10), _issue(20)]
        mock_run.return_value = _proc(json.dumps(issues))
        result = _agg(["owner/repo"]).aggregate(known_fingerprints=set())
        assert len(result.orphaned_issues) == 2
        assert len(result.linked_issues) == 0

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_linked_when_fingerprint_matches(self, mock_run):
        issues = [_issue(7)]
        mock_run.return_value = _proc(json.dumps(issues))
        result = _agg(["owner/repo"]).aggregate(known_fingerprints={"gh-issue-7"})
        assert len(result.linked_issues) == 1
        assert len(result.orphaned_issues) == 0

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_mixed_linked_and_orphaned(self, mock_run):
        issues = [_issue(1), _issue(2), _issue(3)]
        mock_run.return_value = _proc(json.dumps(issues))
        # Only issue 2 is tracked
        result = _agg(["owner/repo"]).aggregate(known_fingerprints={"gh-issue-2"})
        assert len(result.orphaned_issues) == 2
        assert len(result.linked_issues) == 1
        assert result.linked_issues[0]["number"] == 2


# ---------------------------------------------------------------------------
# aggregate() — multi-repo
# ---------------------------------------------------------------------------

class TestAggregateMultiRepo:
    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_sums_across_repos(self, mock_run):
        def side_effect(cmd, **kwargs):
            repo = cmd[cmd.index("--repo") + 1]
            if repo == "org/a":
                return _proc(json.dumps([_issue(1), _issue(2)]))
            if repo == "org/b":
                return _proc(json.dumps([_issue(10), _issue(11), _issue(12)]))
            return _proc("[]")

        mock_run.side_effect = side_effect
        result = _agg(["org/a", "org/b"]).aggregate()
        assert result.total_open == 5
        assert result.by_repo == {"org/a": 2, "org/b": 3}

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_by_repo_has_entry_per_configured_repo(self, mock_run):
        mock_run.return_value = _proc("[]")
        result = _agg(["r1/x", "r2/y", "r3/z"]).aggregate()
        assert set(result.by_repo.keys()) == {"r1/x", "r2/y", "r3/z"}

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_repo_attached_to_each_issue(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(1)]))
        result = _agg(["org/a", "org/b"]).aggregate()
        repos_seen = {entry["repo"] for entry in result.orphaned_issues}
        assert repos_seen == {"org/a", "org/b"}


# ---------------------------------------------------------------------------
# aggregate() — error handling
# ---------------------------------------------------------------------------

class TestAggregateErrors:
    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_failed_repo_counted_as_zero(self, mock_run):
        mock_run.return_value = _proc("", returncode=1)
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 0
        assert len(result.repos) == 1
        assert result.repos[0].error is not None

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_repos_with_errors_have_nonzero_error(self, mock_run):
        def side_effect(cmd, **kwargs):
            repo = cmd[cmd.index("--repo") + 1]
            if repo == "bad/repo":
                return _proc("", returncode=1)
            return _proc(json.dumps([_issue(1)]))

        mock_run.side_effect = side_effect
        result = _agg(["bad/repo", "good/repo"]).aggregate()
        assert result.total_open == 1
        assert result.by_repo["bad/repo"] == 0
        assert result.by_repo["good/repo"] == 1

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_timeout_returns_zero_and_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 0
        assert result.repos[0].error is not None
        assert "timeout" in result.repos[0].error

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_invalid_json_returns_zero(self, mock_run):
        mock_run.return_value = _proc("not-json")
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 0

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_generic_exception_handled(self, mock_run):
        mock_run.side_effect = OSError("network failure")
        result = _agg(["owner/repo"]).aggregate()
        assert result.total_open == 0
        assert result.repos[0].error is not None


# ---------------------------------------------------------------------------
# _fetch_repo_issues — gh CLI call verification
# ---------------------------------------------------------------------------

class TestGhCliCall:
    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_uses_correct_repo_flag(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["myorg/myrepo"]).aggregate()
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--repo")
        assert cmd[idx + 1] == "myorg/myrepo"

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_requests_open_state(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["myorg/myrepo"]).aggregate()
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--state")
        assert cmd[idx + 1] == "open"

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_requests_json_output(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["myorg/myrepo"]).aggregate()
        cmd = mock_run.call_args[0][0]
        assert "--json" in cmd

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_timeout_applied(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["myorg/myrepo"]).aggregate()
        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout", 0) > 0

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_limit_flag_set(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["myorg/myrepo"], max_issues=50).aggregate()
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--limit")
        assert cmd[idx + 1] == "50"

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_called_once_per_repo(self, mock_run):
        mock_run.return_value = _proc("[]")
        _agg(["r1/a", "r2/b", "r3/c"]).aggregate()
        assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# orphaned_issues entry structure
# ---------------------------------------------------------------------------

class TestOrphanedIssueEntry:
    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_entry_has_required_fields(self, mock_run):
        issues = [_issue(number=42, title="Fix it", url="https://github.com/o/r/issues/42")]
        mock_run.return_value = _proc(json.dumps(issues))
        result = _agg(["o/r"]).aggregate()
        entry = result.orphaned_issues[0]
        assert entry["repo"] == "o/r"
        assert entry["number"] == 42
        assert entry["title"] == "Fix it"
        assert entry["url"] == "https://github.com/o/r/issues/42"
        assert entry["fingerprint"] == "gh-issue-42"

    @patch("src.swe_team.github_multi_repo.subprocess.run")
    def test_labels_included_in_entry(self, mock_run):
        issue = _issue(1)
        issue["labels"] = [{"name": "bug"}, {"name": "swe-squad"}]
        mock_run.return_value = _proc(json.dumps([issue]))
        result = _agg(["o/r"]).aggregate()
        entry = result.orphaned_issues[0]
        assert set(entry["labels"]) == {"bug", "swe-squad"}


# ---------------------------------------------------------------------------
# dashboard_data integration — fetch_github_issues (public function)
# ---------------------------------------------------------------------------

class TestFetchGithubIssues:
    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_returns_dict_keyed_by_repo(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(1)]))
        from scripts.ops.dashboard_data import fetch_github_issues
        result = fetch_github_issues(["owner/repo"])
        assert "owner/repo" in result
        assert isinstance(result["owner/repo"], list)

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_issue_count_per_repo(self, mock_run):
        issues = [_issue(i) for i in range(1, 6)]
        mock_run.return_value = _proc(json.dumps(issues))
        from scripts.ops.dashboard_data import fetch_github_issues
        result = fetch_github_issues(["owner/repo"])
        assert len(result["owner/repo"]) == 5

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_failed_repo_returns_empty_list(self, mock_run):
        mock_run.return_value = _proc("", returncode=1)
        from scripts.ops.dashboard_data import fetch_github_issues
        result = fetch_github_issues(["owner/repo"])
        assert result["owner/repo"] == []

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_empty_string_repos_skipped(self, mock_run):
        mock_run.return_value = _proc("[]")
        from scripts.ops.dashboard_data import fetch_github_issues
        result = fetch_github_issues(["", "  ", "owner/repo"])
        assert "" not in result
        assert "owner/repo" in result

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_multiple_repos(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(1)]))
        from scripts.ops.dashboard_data import fetch_github_issues
        result = fetch_github_issues(["r1/a", "r2/b"])
        assert set(result.keys()) == {"r1/a", "r2/b"}


# ---------------------------------------------------------------------------
# generate_dashboard_data integration — github_repos param
# ---------------------------------------------------------------------------

class TestGenerateDashboardDataGithubRepos:
    def _make_store(self):
        store = MagicMock()
        store.list_all.return_value = []
        store.list_open.return_value = []
        store.list_recently_resolved.return_value = []
        store.known_fingerprints = set()
        return store

    def test_github_summary_key_always_present(self):
        """github_summary is always in output (enabled=False when no repos)."""
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store())
        assert "github_summary" in data

    def test_github_summary_disabled_when_no_repos(self):
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store())
        assert data["github_summary"]["enabled"] is False
        assert data["github_summary"]["total_open"] == 0

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_github_summary_enabled_when_repos_provided(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(1)]))
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store(), github_repos=["owner/repo"])
        assert data["github_summary"]["enabled"] is True

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_total_open_correct(self, mock_run):
        issues = [_issue(i) for i in range(1, 4)]
        mock_run.return_value = _proc(json.dumps(issues))
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store(), github_repos=["owner/repo"])
        assert data["github_summary"]["total_open"] == 3

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_by_repo_has_entry(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(5)]))
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store(), github_repos=["owner/repo"])
        assert "owner/repo" in data["github_summary"]["by_repo"]

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_multi_repo_by_repo_keys(self, mock_run):
        mock_run.return_value = _proc(json.dumps([_issue(1)]))
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(
            self._make_store(), github_repos=["o/r1", "o/r2", "o/r3"]
        )
        by_repo = data["github_summary"]["by_repo"]
        assert set(by_repo.keys()) == {"o/r1", "o/r2", "o/r3"}

    @patch("scripts.ops.dashboard_data.subprocess.run")
    def test_orphaned_count_when_no_linked_tickets(self, mock_run):
        issues = [_issue(1), _issue(2)]
        mock_run.return_value = _proc(json.dumps(issues))
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store(), github_repos=["owner/repo"])
        assert data["github_summary"]["orphaned_count"] == 2

    def test_backward_compat_all_existing_keys_present(self):
        """Calling without github_repos should not break existing callers."""
        from scripts.ops.dashboard_data import generate_dashboard_data
        data = generate_dashboard_data(self._make_store())
        for key in ("ticket_summary", "recent_activity", "tickets_by_state",
                    "agent_performance", "memory_stats", "generated_at"):
            assert key in data
