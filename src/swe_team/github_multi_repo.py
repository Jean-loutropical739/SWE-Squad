"""Multi-repo GitHub issue aggregation.

Fetches open issues from multiple configured repositories and aggregates
counts, linking results back to the internal ticket store to identify
orphaned issues (GitHub issues with no corresponding SWE ticket).

Usage::

    from src.swe_team.github_multi_repo import MultiRepoIssueAggregator
    agg = MultiRepoIssueAggregator(repos=["owner/repo1", "owner/repo2"])
    result = agg.aggregate(known_fingerprints=store.known_fingerprints)
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_GITHUB_ISSUE_FIELDS = "number,title,url,labels,state,createdAt,updatedAt"


@dataclass
class RepoIssueCount:
    """Issue count for a single repository."""

    repo: str
    open_count: int
    issues: List[Dict] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class MultiRepoAggregation:
    """Aggregated GitHub issue data across multiple repositories."""

    repos: List[RepoIssueCount] = field(default_factory=list)
    total_open: int = 0
    # Issues that have no corresponding internal ticket (fingerprint not in store)
    orphaned_issues: List[Dict] = field(default_factory=list)
    # Issues that are linked to internal tickets
    linked_issues: List[Dict] = field(default_factory=list)
    by_repo: Dict[str, int] = field(default_factory=dict)


class MultiRepoIssueAggregator:
    """Fetch open GitHub issues from multiple repos and aggregate counts."""

    def __init__(
        self,
        repos: List[str],
        max_issues_per_repo: int = 100,
        timeout: int = 30,
    ) -> None:
        """
        Parameters
        ----------
        repos:
            List of ``owner/repo`` strings to query.
        max_issues_per_repo:
            Maximum number of issues to fetch per repository.
        timeout:
            Subprocess timeout in seconds.
        """
        self._repos = [r for r in repos if r]
        self._max_issues_per_repo = max_issues_per_repo
        self._timeout = timeout

    def aggregate(
        self,
        known_fingerprints: Optional[Set[str]] = None,
    ) -> MultiRepoAggregation:
        """Fetch issues from all repos and return aggregated result.

        Parameters
        ----------
        known_fingerprints:
            Set of fingerprint strings from the ticket store (e.g. ``gh-issue-42``).
            Used to identify orphaned issues.

        Returns
        -------
        MultiRepoAggregation
        """
        fps = known_fingerprints or set()
        repo_counts: List[RepoIssueCount] = []
        all_issues: List[Dict] = []

        for repo in self._repos:
            rc = self._fetch_repo_issues(repo)
            repo_counts.append(rc)
            for issue in rc.issues:
                issue["_repo"] = repo
            all_issues.extend(rc.issues)

        total_open = sum(rc.open_count for rc in repo_counts)
        by_repo = {rc.repo: rc.open_count for rc in repo_counts}

        orphaned: List[Dict] = []
        linked: List[Dict] = []
        for issue in all_issues:
            num = issue.get("number")
            repo = issue.get("_repo", "")
            # Fingerprint format: gh-issue-{number}  (same as github_scanner.py)
            fp = f"gh-issue-{num}"
            entry = {
                "repo": repo,
                "number": num,
                "title": issue.get("title", ""),
                "url": issue.get("url", ""),
                "labels": [
                    lbl.get("name", "") for lbl in issue.get("labels", [])
                ],
                "fingerprint": fp,
            }
            if fp in fps:
                linked.append(entry)
            else:
                orphaned.append(entry)

        return MultiRepoAggregation(
            repos=repo_counts,
            total_open=total_open,
            orphaned_issues=orphaned,
            linked_issues=linked,
            by_repo=by_repo,
        )

    def _fetch_repo_issues(self, repo: str) -> RepoIssueCount:
        """Use gh CLI to fetch open issues for a single repo."""
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", repo,
                    "--state", "open",
                    "--limit", str(self._max_issues_per_repo),
                    "--json", _GITHUB_ISSUE_FIELDS,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                msg = result.stderr.strip()
                logger.warning("gh issue list failed for %s: %s", repo, msg)
                return RepoIssueCount(repo=repo, open_count=0, error=msg)

            if not result.stdout.strip():
                return RepoIssueCount(repo=repo, open_count=0)

            issues = json.loads(result.stdout)
            return RepoIssueCount(
                repo=repo,
                open_count=len(issues),
                issues=issues,
            )
        except subprocess.TimeoutExpired:
            msg = f"timeout after {self._timeout}s"
            logger.warning("gh issue list timed out for %s", repo)
            return RepoIssueCount(repo=repo, open_count=0, error=msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch issues for %s: %s", repo, exc)
            return RepoIssueCount(repo=repo, open_count=0, error=str(exc))
