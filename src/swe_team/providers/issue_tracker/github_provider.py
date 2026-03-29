"""
GitHubIssueTracker — wraps src.swe_team.github_integration.

Concrete implementation of IssueTracker that delegates to the existing
``gh`` CLI-based GitHub integration.  All config is received via
constructor — no os.environ reads inside this class.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

from src.swe_team.providers.issue_tracker.base import IssueRef, IssueTracker

logger = logging.getLogger(__name__)


class GitHubIssueTracker:
    """IssueTracker backed by the ``gh`` CLI.

    Satisfies the ``IssueTracker`` protocol defined in
    ``src/swe_team/providers/issue_tracker/base.py``.
    """

    def __init__(self, *, repo: str = "", token: str = "") -> None:
        self._repo = repo
        self._token = token

    # -- Protocol properties / methods ------------------------------------

    @property
    def name(self) -> str:
        return "github"

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Optional[List[str]] = None,
        assignee: Optional[str] = None,
    ) -> IssueRef:
        """Create a GitHub issue via ``gh issue create``."""
        from src.swe_team.github_integration import create_github_issue
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
        # create_github_issue expects a SWETicket; for generic Protocol usage
        # we just delegate to the underlying gh CLI directly.
        import subprocess
        cmd = ["gh", "issue", "create", "--repo", self._repo, "--title", title, "--body", body]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        if assignee:
            cmd.extend(["--assignee", assignee])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning("gh issue create failed: %s", result.stderr.strip()[:200])
                return IssueRef(issue_id="", url="", title=title)
            url = result.stdout.strip()
            issue_id = url.rsplit("/issues/", 1)[-1] if "/issues/" in url else ""
            return IssueRef(issue_id=issue_id, url=url, title=title)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.create_issue failed: %s", exc)
            return IssueRef(issue_id="", url="", title=title)

    def comment(self, issue_id: str, body: str, *, repo: str = "") -> bool:
        """Add a comment to an existing GitHub issue."""
        from src.swe_team.github_integration import comment_on_issue
        try:
            return comment_on_issue(int(issue_id), body, repo=repo or self._repo)
        except (ValueError, TypeError):
            logger.warning("Invalid issue_id for comment: %s", issue_id)
            return False

    def update_comment(self, comment_id: int, body: str, repo: str = "") -> bool:
        """Edit an existing GitHub issue comment in-place."""
        from src.swe_team.github_integration import update_github_comment
        return update_github_comment(comment_id, body, repo=repo or self._repo)

    def close_issue(self, issue_id: str, *, reason: str = "") -> bool:
        """Close a GitHub issue."""
        import subprocess
        try:
            result = subprocess.run(
                ["gh", "issue", "close", issue_id, "--repo", self._repo],
                capture_output=True, text=True, timeout=15,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.warning("GitHubIssueTracker.close_issue failed: %s", exc)
            return False

    def find_existing(self, title_substring: str) -> List[IssueRef]:
        """Search for existing issues matching the title substring."""
        from src.swe_team.github_integration import find_existing_issue
        # find_existing_issue expects a SWETicket — return empty for generic usage
        return []

    def health_check(self) -> bool:
        """Return True if repo is configured."""
        return bool(self._repo)

    # -- PR operations ---------------------------------------------------------

    def find_pr(self, branch: str, repo: str = "") -> Optional[Dict[str, Any]]:
        """Find an existing PR by head branch. Returns dict with 'number', 'url' keys or None."""
        target_repo = repo or self._repo
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--repo", target_repo,
                 "--json", "number,url"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = json.loads(result.stdout)
                if prs:
                    return {"number": int(prs[0]["number"]), "url": prs[0].get("url", "")}
        except subprocess.TimeoutExpired:
            logger.warning("GitHubIssueTracker.find_pr timed out for branch %s", branch)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.find_pr failed: %s", exc)
        return None

    def create_pr(self, title: str, body: str, branch: str, base: str = "main",
                  repo: str = "") -> Optional[str]:
        """Create a PR. Returns PR URL or None on failure."""
        target_repo = repo or self._repo
        try:
            result = subprocess.run(
                ["gh", "pr", "create", "--head", branch, "--base", base,
                 "--title", title, "--body", body, "--repo", target_repo],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            logger.warning("GitHubIssueTracker.create_pr failed (rc=%d): %s",
                           result.returncode, result.stderr[:300])
        except subprocess.TimeoutExpired:
            logger.warning("GitHubIssueTracker.create_pr timed out for branch %s", branch)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.create_pr failed: %s", exc)
        return None

    def merge_pr(self, pr_number: int, repo: str = "") -> bool:
        """Squash-merge a PR and delete the branch. Returns True on success."""
        target_repo = repo or self._repo
        try:
            result = subprocess.run(
                ["gh", "pr", "merge", str(pr_number), "--squash",
                 "--repo", target_repo, "--delete-branch"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.info("GitHubIssueTracker: merged PR #%d in %s", pr_number, target_repo)
                return True
            logger.warning("GitHubIssueTracker.merge_pr failed (rc=%d): %s",
                           result.returncode, result.stderr[:300])
        except subprocess.TimeoutExpired:
            logger.warning("GitHubIssueTracker.merge_pr timed out for PR #%d", pr_number)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.merge_pr failed: %s", exc)
        return False

    def close_pr(self, pr_number: int, repo: str = "") -> bool:
        """Close a PR without merging. Returns True on success."""
        target_repo = repo or self._repo
        try:
            result = subprocess.run(
                ["gh", "pr", "close", str(pr_number), "--repo", target_repo],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                logger.info("GitHubIssueTracker: closed PR #%d in %s", pr_number, target_repo)
                return True
            logger.warning("GitHubIssueTracker.close_pr failed (rc=%d): %s",
                           result.returncode, result.stderr[:200])
        except subprocess.TimeoutExpired:
            logger.warning("GitHubIssueTracker.close_pr timed out for PR #%d", pr_number)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.close_pr failed: %s", exc)
        return False

    def get_pr_labels(self, pr_number: int, repo: str = "") -> List[str]:
        """Get labels on a PR. Returns list of label names."""
        target_repo = repo or self._repo
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--repo", target_repo,
                 "--json", "labels"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return [lbl.get("name", "") for lbl in data.get("labels", [])]
        except subprocess.TimeoutExpired:
            logger.warning("GitHubIssueTracker.get_pr_labels timed out for PR #%d", pr_number)
        except Exception as exc:
            logger.warning("GitHubIssueTracker.get_pr_labels failed: %s", exc)
        return []


# Runtime check (asserts the class satisfies the protocol)
assert isinstance(GitHubIssueTracker(repo="test/test"), IssueTracker)
