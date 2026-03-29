"""
IssueTracker interface — pluggable issue/ticket backend.

Implement this to swap GitHub Issues for Jira, Linear, GitLab, etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class IssueRef:
    issue_id: str
    url: str
    title: str


@runtime_checkable
class IssueTracker(Protocol):

    @property
    def name(self) -> str: ...

    def create_issue(self, title: str, body: str, *, labels: Optional[List[str]] = None,
                     assignee: Optional[str] = None) -> IssueRef: ...

    def comment(self, issue_id: str, body: str) -> bool: ...

    def close_issue(self, issue_id: str, *, reason: str = "") -> bool: ...

    def find_existing(self, title_substring: str) -> List[IssueRef]: ...

    def health_check(self) -> bool: ...

    # -- PR operations (optional — providers may raise NotImplementedError) --

    def find_pr(self, branch: str, repo: str = "") -> Optional[Dict[str, Any]]:
        """Find an existing PR by head branch. Returns dict with 'number', 'url' keys or None."""
        ...

    def create_pr(self, title: str, body: str, branch: str, base: str = "main", repo: str = "") -> Optional[str]:
        """Create a PR. Returns PR URL or None on failure."""
        ...

    def merge_pr(self, pr_number: int, repo: str = "") -> bool:
        """Merge a PR. Returns True on success."""
        ...

    def close_pr(self, pr_number: int, repo: str = "") -> bool:
        """Close a PR without merging. Returns True on success."""
        ...

    def get_pr_labels(self, pr_number: int, repo: str = "") -> List[str]:
        """Get labels on a PR. Returns list of label names."""
        ...
