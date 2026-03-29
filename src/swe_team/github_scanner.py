"""GitHub Issue Scanner — autonomous backlog pickup.

Scans open GitHub issues labeled for SWE-Squad processing and creates
internal SWE tickets for any that don't already have one.  Complements
the existing ``fetch_github_tickets()`` helper (which only picks up issues
*assigned* to the team's GitHub account) by adding **label-based discovery**
so human-filed issues are picked up even before explicit assignment.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.swe_team.models import SWETicket, TicketSeverity

logger = logging.getLogger(__name__)

# Labels that mark issues for SWE-Squad processing (explicit opt-in only)
_PICKUP_LABELS: Set[str] = {"swe-squad", "automated"}
# Labels that allow pickup ONLY when the issue is assigned to the bot account
_ASSIGNED_ONLY_LABELS: Set[str] = {"bug", "critical", "high", "medium"}
# Labels that mean "do NOT auto-pickup"
_SKIP_LABELS: Set[str] = {"needs-human-review", "wontfix", "duplicate", "question"}

_SEVERITY_MAP: Dict[str, TicketSeverity] = {
    "critical": TicketSeverity.CRITICAL,
    "high": TicketSeverity.HIGH,
    "medium": TicketSeverity.MEDIUM,
    "low": TicketSeverity.LOW,
    "bug": TicketSeverity.HIGH,  # default for unlabeled bugs
}

# Priority order: first match wins when an issue has multiple severity labels
_SEVERITY_PRIORITY: List[str] = ["critical", "high", "bug", "medium", "low"]


@dataclass
class GitHubScannerConfig:
    """Configuration for the GitHub issue scanner."""
    repo: str = ""  # owner/repo
    pickup_labels: Set[str] = field(default_factory=lambda: set(_PICKUP_LABELS))
    assigned_only_labels: Set[str] = field(default_factory=lambda: set(_ASSIGNED_ONLY_LABELS))
    skip_labels: Set[str] = field(default_factory=lambda: set(_SKIP_LABELS))
    max_issues_per_scan: int = 10
    enabled: bool = True
    github_account: str = ""  # Bot account name for assignee checks


class GitHubIssueScanner:
    """Scans GitHub issues and creates SWE tickets for untracked ones."""

    def __init__(
        self,
        config: GitHubScannerConfig,
        known_fingerprints: Optional[Set[str]] = None,
        known_issue_numbers: Optional[Set[int]] = None,
    ) -> None:
        self._config = config
        self._repo: str = config.repo or ""
        self._github_account: str = config.github_account or ""
        self._data_dir = Path("data/swe_team")
        self._scanner_seen_file = self._data_dir / "scanner_seen.json"
        
        # Load persisted dedup state
        persisted_state = self._load_persisted_state()
        self._known_fps: Set[str] = (
            set(known_fingerprints)
            if known_fingerprints is not None
            else persisted_state.get("fingerprints", set())
        )
        self._known_issue_numbers: Set[int] = (
            set(known_issue_numbers)
            if known_issue_numbers is not None
            else persisted_state.get("issue_numbers", set())
        )

    def _load_persisted_state(self) -> Dict[str, Set]:
        """Load persisted scanner dedup state from disk."""
        if not self._scanner_seen_file.exists():
            return {"fingerprints": set(), "issue_numbers": set()}
        
        try:
            with open(self._scanner_seen_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {
                    "fingerprints": set(data.get("fingerprints", [])),
                    "issue_numbers": set(data.get("issue_numbers", []))
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load scanner dedup state: %s", exc)
            return {"fingerprints": set(), "issue_numbers": set()}

    def _save_persisted_state(self) -> None:
        """Save current scanner dedup state to disk."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "fingerprints": list(self._known_fps),
                "issue_numbers": list(self._known_issue_numbers)
            }
            with open(self._scanner_seen_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save scanner dedup state: %s", exc)

    def scan(self) -> List[SWETicket]:
        """Fetch open GitHub issues and return new SWE tickets for untracked ones."""
        if not self._config.enabled or not self._repo:
            return []

        issues = self._fetch_open_issues()
        new_tickets: List[SWETicket] = []

        for issue in issues:
            issue_num = issue.get("number")
            if issue_num is None:
                continue
            fingerprint = f"gh-issue-{issue_num}"
            if fingerprint in self._known_fps:
                continue
            if issue_num in self._known_issue_numbers:
                continue

            labels = {l.get("name", "").lower() for l in issue.get("labels", [])}

            # Skip issues with exclusion labels
            if labels & self._config.skip_labels:
                logger.debug("Skipping issue #%d — has skip label", issue_num)
                continue

            # ── Assignee gate (MANDATORY) ──────────────────────────────
            # An issue is eligible ONLY if it is assigned to this team's
            # github_account.  Labels are informational metadata, never
            # pickup triggers.  This is the core isolation mechanism that
            # allows multiple squads (alpha, beta, …) to coexist on the
            # same repos without conflicts.
            assignee_logins = {
                a.get("login", "").lower()
                for a in issue.get("assignees", [])
            }
            if not self._github_account:
                logger.debug(
                    "Skipping issue #%d — no github_account configured for assignee check",
                    issue_num,
                )
                continue
            if self._github_account.lower() not in assignee_logins:
                logger.debug(
                    "Skipping issue #%d — not assigned to %s (assignees: %s)",
                    issue_num, self._github_account, assignee_logins or "none",
                )
                continue

            ticket = self._issue_to_ticket(issue, labels)
            if ticket:
                new_tickets.append(ticket)
                self._known_fps.add(fingerprint)
                self._known_issue_numbers.add(issue_num)
                # Persist dedup state immediately
                self._save_persisted_state()

            if len(new_tickets) >= self._config.max_issues_per_scan:
                break

        if new_tickets:
            logger.info("GitHub scanner found %d new issues to process", len(new_tickets))

        return new_tickets

    def _fetch_open_issues(self) -> list:
        """Use gh CLI to fetch open issues."""
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", self._repo,
                    "--state", "open",
                    "--limit", str(self._config.max_issues_per_scan * 3),
                    "--json", "number,title,body,labels,assignees,createdAt",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning("gh issue list failed: %s", result.stderr.strip())
                return []
            return json.loads(result.stdout) if result.stdout.strip() else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHub issue scan failed: %s", exc)
            return []

    def _issue_to_ticket(self, issue: dict, labels: Set[str]) -> Optional[SWETicket]:
        """Convert a GitHub issue dict to a SWETicket."""
        issue_num = issue.get("number")
        title = issue.get("title", "").strip()
        body = issue.get("body", "").strip()

        # Determine severity from labels — pick highest matching severity
        severity = TicketSeverity.MEDIUM  # default
        for label_name in _SEVERITY_PRIORITY:
            if label_name in labels:
                severity = _SEVERITY_MAP[label_name]
                break

        fingerprint = f"gh-issue-{issue_num}"

        # Detect module from labels (module:xxx convention)
        module = "github"
        for lbl in labels:
            if lbl.startswith("module:"):
                module = lbl.replace("module:", "").strip()
                break

        ticket = SWETicket(
            title=f"[GH#{issue_num}] {title}"[:120],
            description=body[:2000] if body else title,
            severity=severity,
            source_module=module,
            metadata={
                "fingerprint": fingerprint,
                "github_issue": issue_num,
                "github_repo": self._config.repo,
                "repo": self._config.repo,
                "source": "github_scanner",
                "github_labels": sorted(labels),
            },
        )

        # Assign if issue has assignees
        assignees = issue.get("assignees", [])
        if assignees:
            ticket.assigned_to = assignees[0].get("login", "")

        return ticket
