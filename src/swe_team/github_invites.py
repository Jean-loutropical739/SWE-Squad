"""GitHub collaboration invite auto-acceptor.

Accepts pending repository collaboration invitations for a GitHub account.
Supports allowlist filtering so only invites from specific orgs/owners are
accepted.  Disabled by default — must be explicitly enabled via config.

Usage (standalone):
    from src.swe_team.github_invites import accept_pending_invites

    accepted = accept_pending_invites(
        github_account="my-bot-account",
        allowlist=["my-org"],
        dry_run=False,
    )

Config keys read from config dict:
    auto_accept_invites: bool   — master toggle (default: False)
    invite_allowlist: list[str] — org/owner filter (default: [] = accept all)
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def accept_pending_invites(
    github_account: str,
    allowlist: Optional[List[str]] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Accept pending GitHub repository collaboration invitations.

    Parameters
    ----------
    github_account:
        The GitHub account whose invitations should be processed.  Used only
        for logging — the ``gh`` CLI uses whichever account is authenticated.
    allowlist:
        If provided, only invitations from repositories owned by one of the
        listed org/user names are accepted.  Comparison is case-insensitive.
        An empty list or ``None`` means "accept everything".
    dry_run:
        When ``True``, log what *would* be accepted but do not call the PATCH
        endpoint.

    Returns
    -------
    List of dicts with keys:
        ``id``, ``repo``, ``owner``, ``inviter``, ``created_at``
    """
    invitations = _list_pending_invites()
    if not invitations:
        logger.debug("github_invites: no pending invitations for %s", github_account)
        return []

    logger.info(
        "github_invites: %d pending invitation(s) for %s",
        len(invitations),
        github_account,
    )

    accepted: List[Dict[str, Any]] = []
    normalised_allowlist = (
        [entry.lower() for entry in allowlist] if allowlist else []
    )

    for invite in invitations:
        invite_id = invite.get("id")
        repo_info = invite.get("repository") or {}
        repo_full = repo_info.get("full_name", "")
        owner = repo_full.split("/")[0] if "/" in repo_full else repo_full
        inviter_info = invite.get("inviter") or {}
        inviter = inviter_info.get("login", "unknown")
        created_at = invite.get("created_at", "")

        # Allowlist filter
        if normalised_allowlist and owner.lower() not in normalised_allowlist:
            logger.info(
                "github_invites: SKIP invite %s from %s — owner '%s' not in allowlist %s",
                invite_id,
                repo_full,
                owner,
                normalised_allowlist,
            )
            continue

        record = {
            "id": invite_id,
            "repo": repo_full,
            "owner": owner,
            "inviter": inviter,
            "created_at": created_at,
        }

        if dry_run:
            logger.info(
                "github_invites: DRY-RUN — would accept invite %s from %s (invited by %s)",
                invite_id,
                repo_full,
                inviter,
            )
            accepted.append(record)
            continue

        if _accept_invite(invite_id):
            logger.info(
                "github_invites: ACCEPTED invite %s — repo=%s inviter=%s",
                invite_id,
                repo_full,
                inviter,
            )
            accepted.append(record)
        else:
            logger.warning(
                "github_invites: FAILED to accept invite %s for repo=%s",
                invite_id,
                repo_full,
            )

    logger.info(
        "github_invites: accepted %d/%d invite(s) (dry_run=%s)",
        len(accepted),
        len(invitations),
        dry_run,
    )
    return accepted


def accept_pending_invites_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept invites using values from a config dict.

    Reads:
        ``auto_accept_invites`` — must be True to do anything (default: False)
        ``invite_allowlist``    — optional list of org/owner names
        ``github_account``      — account name for logging

    Returns an empty list (and does nothing) when disabled.
    """
    if not config.get("auto_accept_invites", False):
        logger.debug("github_invites: auto_accept_invites is disabled — skipping")
        return []

    github_account: str = config.get("github_account", "")
    allowlist: Optional[List[str]] = config.get("invite_allowlist") or None
    dry_run: bool = bool(config.get("dry_run", False))

    return accept_pending_invites(
        github_account=github_account,
        allowlist=allowlist,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_pending_invites() -> List[Dict[str, Any]]:
    """Call ``gh api /user/repository_invitations`` and return parsed JSON."""
    try:
        result = subprocess.run(
            ["gh", "api", "/user/repository_invitations"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "github_invites: gh api list failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return []
        raw = result.stdout.strip()
        return json.loads(raw) if raw else []
    except subprocess.TimeoutExpired:
        logger.warning("github_invites: gh api list timed out")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("github_invites: failed to list invitations: %s", exc)
        return []


def _accept_invite(invite_id: int) -> bool:
    """Call ``gh api -X PATCH /user/repository_invitations/{id}``."""
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "PATCH", f"/user/repository_invitations/{invite_id}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "github_invites: PATCH invite %s failed (rc=%d): %s",
                invite_id,
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("github_invites: PATCH invite %s timed out", invite_id)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("github_invites: PATCH invite %s error: %s", invite_id, exc)
        return False
