#!/usr/bin/env python3
"""SWE Squad CLI — lightweight operational queries and reporting.

Standalone CLI tool (argparse, no external deps beyond python-dotenv)
for querying system status, listing tickets, generating summaries,
and sending reports via Telegram.

Usage:
    swe_cli.py status [--json]
    swe_cli.py tickets [--status STATUS] [--severity SEV] [--team TEAM] [--json]
    swe_cli.py issues [--json]
    swe_cli.py repos [--json]
    swe_cli.py summary [--json]
    swe_cli.py dashboard [--json] [--html]
    swe_cli.py report {daily|status|cycle|dashboard}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# Load .env (same pattern as swe_team_runner.py)
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass  # dotenv optional at import time; needed at runtime for credentials

logger = logging.getLogger("swe_cli")

# ── Optional Rich output ─────────────────────────────────────────────────────
try:
    from scripts.ops.cli_rich import HAS_RICH
    import scripts.ops.cli_rich as _rich
except ImportError:
    HAS_RICH = False
    _rich = None  # type: ignore[assignment]

# ── Constants ─────────────────────────────────────────────────────────────────
STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"
TICKETS_PATH = PROJECT_ROOT / "data" / "swe_team" / "tickets.json"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_status() -> Optional[Dict[str, Any]]:
    """Load data/swe_team/status.json if it exists."""
    if not STATUS_PATH.is_file():
        return None
    try:
        with open(STATUS_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _get_ticket_store():
    """Get a TicketStore instance (JSON-backed)."""
    from src.swe_team.ticket_store import TicketStore
    return TicketStore(str(TICKETS_PATH))


def _truncate(text: str, width: int) -> str:
    """Truncate text to *width* characters, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _run_gh(args: List[str], timeout: int = 15) -> Optional[str]:
    """Run a ``gh`` CLI command and return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("gh command failed: %s", result.stderr.strip())
            return None
        return result.stdout
    except FileNotFoundError:
        logger.warning("GitHub CLI (gh) not found in PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("gh command timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.warning("gh command error: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Subcommands
# ══════════════════════════════════════════════════════════════════════════════

def cmd_status(args: argparse.Namespace) -> int:
    """Show current system status."""
    status = _load_status()

    # Also query ticket store for live counts
    try:
        store = _get_ticket_store()
        all_tickets = store.list_all()
        open_tickets = store.list_open()
    except Exception:
        all_tickets = []
        open_tickets = []

    from src.swe_team.models import TicketStatus

    counts = {
        "total": len(all_tickets),
        "open": len([t for t in open_tickets if t.status == TicketStatus.OPEN]),
        "investigating": len(
            [t for t in open_tickets if t.status == TicketStatus.INVESTIGATING]
        ),
        "in_development": len(
            [t for t in open_tickets if t.status == TicketStatus.IN_DEVELOPMENT]
        ),
        "resolved": len(
            [t for t in all_tickets if t.status == TicketStatus.RESOLVED]
        ),
    }

    data = {
        "last_cycle": status.get("last_cycle") if status else None,
        "gate_verdict": status.get("gate_verdict") if status else None,
        "next_cycle": status.get("next_cycle") if status else None,
        "tickets_open": status.get("tickets_open") if status else len(open_tickets),
        "tickets_investigating": (
            status.get("tickets_investigating") if status else counts["investigating"]
        ),
        "ticket_counts": counts,
    }

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_status(data, status)
        return 0

    # Formatted text output (fallback)
    print("=== SWE Squad Status ===\n")
    if status:
        print(f"  Last cycle:     {status.get('last_cycle', 'N/A')}")
        print(f"  Gate verdict:   {status.get('gate_verdict', 'N/A')}")
        print(f"  Next cycle:     {status.get('next_cycle', 'N/A')}")
    else:
        print("  Status file:    not found (no cycle has run yet)")
    print()
    print(f"  Total tickets:  {counts['total']}")
    print(f"  Open:           {counts['open']}")
    print(f"  Investigating:  {counts['investigating']}")
    print(f"  In development: {counts['in_development']}")
    print(f"  Resolved:       {counts['resolved']}")
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    """List tickets with optional filters."""
    from src.swe_team.models import TicketSeverity, TicketStatus

    try:
        store = _get_ticket_store()
    except Exception as exc:
        print(f"Error loading ticket store: {exc}", file=sys.stderr)
        return 1

    tickets = store.list_all()

    # Apply filters
    if args.status:
        try:
            target_status = TicketStatus(args.status)
        except ValueError:
            print(f"Unknown status: {args.status}", file=sys.stderr)
            print(
                f"Valid statuses: {', '.join(s.value for s in TicketStatus)}",
                file=sys.stderr,
            )
            return 1
        tickets = [t for t in tickets if t.status == target_status]

    if args.severity:
        try:
            target_sev = TicketSeverity(args.severity)
        except ValueError:
            print(f"Unknown severity: {args.severity}", file=sys.stderr)
            print(
                f"Valid severities: {', '.join(s.value for s in TicketSeverity)}",
                file=sys.stderr,
            )
            return 1
        tickets = [t for t in tickets if t.severity == target_sev]

    if args.team:
        tickets = [t for t in tickets if t.assigned_to == args.team]

    if not args.status:
        # Default: show only open tickets (not resolved/closed/acknowledged)
        closed_statuses = {
            TicketStatus.RESOLVED,
            TicketStatus.CLOSED,
            TicketStatus.ACKNOWLEDGED,
        }
        tickets = [t for t in tickets if t.status not in closed_statuses]

    if args.json:
        print(json.dumps([t.to_dict() for t in tickets], indent=2))
        return 0

    if not tickets:
        print("No tickets found matching filters.")
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_tickets(tickets, _truncate)
        return 0

    # Tabular output (fallback)
    header = f"{'TICKET_ID':<14} {'SEVERITY':<10} {'STATUS':<20} {'ASSIGNED_TO':<16} {'TITLE'}"
    print(header)
    print("-" * len(header))
    for t in tickets:
        print(
            f"{t.ticket_id:<14} "
            f"{t.severity.value:<10} "
            f"{t.status.value:<20} "
            f"{(t.assigned_to or '-'):<16} "
            f"{_truncate(t.title, 50)}"
        )

    print(f"\n{len(tickets)} ticket(s)")
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    """List GitHub issues assigned to the team."""
    github_account = os.environ.get("SWE_GITHUB_ACCOUNT", "")
    github_repo = os.environ.get("SWE_GITHUB_REPO", "")

    if not github_account:
        print("SWE_GITHUB_ACCOUNT not set in environment.", file=sys.stderr)
        return 1

    gh_args = [
        "issue", "list",
        "--state", "open",
        "--assignee", github_account,
        "--json", "number,title,labels,createdAt",
    ]
    if github_repo:
        gh_args.extend(["--repo", github_repo])

    output = _run_gh(gh_args)
    if output is None:
        print("Failed to fetch GitHub issues.", file=sys.stderr)
        return 1

    try:
        issues = json.loads(output)
    except json.JSONDecodeError:
        print("Failed to parse GitHub CLI output.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(issues, indent=2))
        return 0

    if not issues:
        print("No open issues assigned to the team.")
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_issues(issues, _truncate)
        return 0

    # Plain text fallback
    header = f"{'#':<8} {'CREATED':<12} {'LABELS':<30} {'TITLE'}"
    print(header)
    print("-" * len(header))
    for issue in issues:
        num = issue.get("number", "?")
        title = issue.get("title", "")
        created = issue.get("createdAt", "")[:10]
        label_names = [l.get("name", "") for l in issue.get("labels", [])]
        labels_str = ", ".join(label_names) if label_names else "-"
        print(
            f"#{num:<7} "
            f"{created:<12} "
            f"{_truncate(labels_str, 28):<30} "
            f"{_truncate(title, 50)}"
        )

    print(f"\n{len(issues)} issue(s)")
    return 0


def cmd_repos(args: argparse.Namespace) -> int:
    """List repos the bot account has access to."""
    gh_args = [
        "repo", "list",
        "--json", "name,visibility,viewerPermission",
        "--limit", "50",
    ]

    output = _run_gh(gh_args)
    if output is None:
        print("Failed to list repos.", file=sys.stderr)
        return 1

    try:
        repos = json.loads(output)
    except json.JSONDecodeError:
        print("Failed to parse GitHub CLI output.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(repos, indent=2))
        return 0

    if not repos:
        print("No repos found.")
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_repos(repos, _truncate)
        return 0

    # Plain text fallback
    header = f"{'NAME':<40} {'VISIBILITY':<14} {'PERMISSION'}"
    print(header)
    print("-" * len(header))
    for repo in repos:
        name = repo.get("name", "?")
        vis = repo.get("visibility", "?")
        perm = repo.get("viewerPermission", "?")
        print(f"{_truncate(name, 38):<40} {vis:<14} {perm}")

    print(f"\n{len(repos)} repo(s)")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Generate a summary report."""
    from src.swe_team.models import TicketSeverity, TicketStatus

    try:
        store = _get_ticket_store()
    except Exception as exc:
        print(f"Error loading ticket store: {exc}", file=sys.stderr)
        return 1

    all_tickets = store.list_all()
    open_tickets = store.list_open()
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Counts by severity
    severity_counts: Dict[str, int] = {}
    for t in open_tickets:
        severity_counts[t.severity.value] = severity_counts.get(t.severity.value, 0) + 1

    # Counts by status
    status_counts: Dict[str, int] = {}
    for t in open_tickets:
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1

    # Recent investigations (last 24h)
    recent_investigations = []
    for t in all_tickets:
        if t.status in (
            TicketStatus.INVESTIGATION_COMPLETE,
            TicketStatus.IN_DEVELOPMENT,
            TicketStatus.IN_REVIEW,
            TicketStatus.RESOLVED,
        ) and t.investigation_report:
            try:
                updated = datetime.fromisoformat(t.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated >= cutoff_24h:
                    recent_investigations.append(t)
            except (ValueError, TypeError):
                pass

    # Recent fixes (last 24h)
    recent_resolved = store.list_recently_resolved(hours=24)
    fix_success = len([t for t in recent_resolved if t.test_results and t.test_results.get("status") == "pass"])
    fix_fail = len([t for t in recent_resolved if t.test_results and t.test_results.get("status") == "fail"])

    # Gate verdict history (from status.json)
    status = _load_status()

    data = {
        "generated_at": now.isoformat(),
        "open_tickets": len(open_tickets),
        "total_tickets": len(all_tickets),
        "severity_counts": severity_counts,
        "status_counts": status_counts,
        "recent_investigations_24h": len(recent_investigations),
        "recent_fixes_24h": {
            "total": len(recent_resolved),
            "success": fix_success,
            "fail": fix_fail,
        },
        "gate_verdict": status.get("gate_verdict") if status else None,
        "last_cycle": status.get("last_cycle") if status else None,
    }

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_summary(data)
        return 0

    # Formatted text output (fallback)
    print("=== SWE Squad Summary ===")
    print(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    print(f"Open tickets: {len(open_tickets)}  |  Total: {len(all_tickets)}")
    print()

    print("By severity:")
    for sev in ("critical", "high", "medium", "low"):
        count = severity_counts.get(sev, 0)
        if count:
            print(f"  {sev.upper():<10} {count}")
    if not severity_counts:
        print("  (none)")

    print("\nBy status:")
    for st, count in sorted(status_counts.items()):
        print(f"  {st:<22} {count}")
    if not status_counts:
        print("  (none)")

    print(f"\nRecent investigations (24h): {len(recent_investigations)}")
    for t in recent_investigations[:5]:
        print(f"  - [{t.severity.value.upper()}] {_truncate(t.title, 60)}")

    print(f"\nRecent fixes (24h): {len(recent_resolved)}")
    if recent_resolved:
        print(f"  Success: {fix_success}  |  Failed: {fix_fail}")

    if status:
        print(f"\nLast gate verdict: {status.get('gate_verdict', 'N/A')}")
        print(f"Last cycle: {status.get('last_cycle', 'N/A')}")

    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Generate observability dashboard data."""
    from scripts.ops.dashboard_data import (
        generate_dashboard_data,
        render_dashboard_html,
    )

    try:
        store = _get_ticket_store()
    except Exception as exc:
        print(f"Error loading ticket store: {exc}", file=sys.stderr)
        return 1

    data = generate_dashboard_data(store)

    if getattr(args, "html", False):
        html = render_dashboard_html(data)
        print(html)
        return 0

    # Default: JSON output
    print(json.dumps(data, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Send a report via Telegram."""
    report_type = args.report_type

    if report_type == "daily":
        return _report_daily()
    elif report_type == "status":
        return _report_status()
    elif report_type == "cycle":
        return _report_cycle()
    elif report_type == "dashboard":
        return _report_dashboard()
    else:
        print(f"Unknown report type: {report_type}", file=sys.stderr)
        print("Valid types: daily, status, cycle, dashboard", file=sys.stderr)
        return 1


def _send_telegram(message: str) -> bool:
    """Send a Telegram message, trying swe_team.notifier first."""
    try:
        from src.swe_team.notifier import _send
        return _send(message)
    except ImportError:
        logger.warning("Notifier module not available")
        return False


def _report_daily() -> int:
    """Send daily summary report via Telegram."""
    try:
        store = _get_ticket_store()
        from src.swe_team.notifier import notify_daily_summary
        notify_daily_summary(store)
        print("Daily summary sent.")
        return 0
    except Exception as exc:
        print(f"Failed to send daily summary: {exc}", file=sys.stderr)
        return 1


def _report_status() -> int:
    """Send current status snapshot via Telegram."""
    status = _load_status()
    if not status:
        msg = "<b>SWE Squad Status</b>\n\nNo status file found. No cycles have run yet."
    else:
        msg = (
            f"<b>SWE Squad Status</b>\n\n"
            f"Last cycle: {status.get('last_cycle', 'N/A')}\n"
            f"Open tickets: {status.get('tickets_open', '?')}\n"
            f"Investigating: {status.get('tickets_investigating', '?')}\n"
            f"Gate verdict: {status.get('gate_verdict', 'N/A')}\n"
            f"Next cycle: {status.get('next_cycle', 'N/A')}"
        )

    ok = _send_telegram(msg)
    if ok:
        print("Status report sent.")
        return 0
    else:
        print("Failed to send status report.", file=sys.stderr)
        return 1


def _report_cycle() -> int:
    """Send last cycle results via Telegram."""
    status = _load_status()
    if not status:
        print("No status file found — no cycle results to report.", file=sys.stderr)
        return 1

    msg = (
        f"<b>SWE Squad — Last Cycle</b>\n\n"
        f"Time: {status.get('last_cycle', 'N/A')}\n"
        f"Open tickets: {status.get('tickets_open', '?')}\n"
        f"Investigating: {status.get('tickets_investigating', '?')}\n"
        f"Gate verdict: <b>{status.get('gate_verdict', 'N/A')}</b>"
    )

    ok = _send_telegram(msg)
    if ok:
        print("Cycle report sent.")
        return 0
    else:
        print("Failed to send cycle report.", file=sys.stderr)
        return 1


def _report_dashboard() -> int:
    """Send dashboard summary report via Telegram."""
    from scripts.ops.dashboard_data import (
        format_dashboard_telegram,
        generate_dashboard_data,
    )

    try:
        store = _get_ticket_store()
        data = generate_dashboard_data(store)
        msg = format_dashboard_telegram(data)
    except Exception as exc:
        print(f"Failed to generate dashboard: {exc}", file=sys.stderr)
        return 1

    ok = _send_telegram(msg)
    if ok:
        print("Dashboard report sent.")
        return 0
    else:
        print("Failed to send dashboard report.", file=sys.stderr)
        return 1


def cmd_auth(args: argparse.Namespace) -> int:
    """Display per-provider authentication status."""
    from src.swe_team.providers.auth import InMemoryAuthProvider

    # Build a provider with the known provider names from config
    known_providers = ["base_llm", "github", "telegram", "supabase"]
    auth = InMemoryAuthProvider(known_providers)

    # Try to read live state from the dashboard API if running
    import urllib.request
    import urllib.error
    dashboard_url = os.environ.get("SWE_DASHBOARD_URL", "http://localhost:8080")
    try:
        req = urllib.request.Request(f"{dashboard_url}/api/auth/status", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            providers = data.get("providers", [])
    except Exception:
        # Dashboard not running -- show offline placeholder state
        providers = [
            {"name": n, "is_authenticated": False, "auth_method": "api_key",
             "is_healthy": False, "consecutive_failures": 0, "last_error": None}
            for n in known_providers
        ]

    if getattr(args, "json", False):
        print(json.dumps({"providers": providers}, indent=2))
        return 0

    # Table output
    header = f"{'Provider':<15} {'Status':<12} {'Method':<10} {'Failures':<10} {'Last Error'}"
    print(header)
    print("-" * len(header))
    for p in providers:
        if p["is_healthy"]:
            status = "healthy"
        elif p["consecutive_failures"] > 0:
            status = "failed" if p["consecutive_failures"] >= 3 else "degraded"
        else:
            status = "unknown"
        error = (p["last_error"] or "")[:50]
        print(f"{p['name']:<15} {status:<12} {p['auth_method']:<10} {p['consecutive_failures']:<10} {error}")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    """Display RBAC role definitions."""
    from src.swe_team.agent_rbac import get_rbac_engine
    engine = get_rbac_engine()
    roles = engine.list_roles()
    if not roles:
        print("No roles defined. Create config/swe_team/roles.yaml")
        return 0
    for name, role in roles.items():
        status = "ENABLED" if role.enabled else "DISABLED"
        perms = ", ".join(sorted(role.permissions))
        denies = ", ".join(sorted(role.deny)) if role.deny else "—"
        print(f"\n[{status}] {name}: {role.description}")
        print(f"  Permissions: {perms}")
        print(f"  Deny: {denies}")
        print(f"  Models: {', '.join(role.models) or '—'}")
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    """Show token usage and cost summary."""
    from src.swe_team.token_tracker import TokenTracker
    tracker = TokenTracker()
    summary = tracker.summary()

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_costs(summary)
        return 0

    # Plain text fallback
    print(f"Total cost: ${summary['total_cost_usd']:.4f}")
    print(f"Today's spend: ${summary['daily_spend']:.4f}")
    print(f"Total records: {summary['total_records']}")
    print()

    if summary['by_model']:
        print(f"{'Model':<15} {'Calls':<8} {'Input Tokens':<15} {'Output Tokens':<16} {'Cost'}")
        print("-" * 70)
        for model, data in summary['by_model'].items():
            print(f"{model:<15} {data['calls']:<8} {data['input_tokens']:<15} {data['output_tokens']:<16} ${data['cost_usd']:.4f}")
    else:
        print("No token usage recorded yet.")

    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """Show multi-project operations status."""
    from src.swe_team.ops.project_registry import ProjectRegistry
    registry = ProjectRegistry()
    projects = registry.list_projects()

    if not projects:
        print("No projects registered. Add YAML files to config/projects/")
        return 0

    print(f"{'Project':<25} {'Repo':<35} {'Daily Cap':<12} {'Status'}")
    print("-" * 85)

    validation = registry.validate_all()
    for p in projects:
        missing = validation.get(p.name, [])
        status = "OK" if not missing else f"MISSING: {', '.join(missing[:3])}"
        cap = f"${p.budget.daily_cap_usd:.0f}" if p.budget.daily_cap_usd else "\u2014"
        print(f"{p.name:<25} {p.repo:<35} {cap:<12} {status}")

    return 0


def _load_config_yaml() -> dict:
    """Load raw swe_team.yaml as a dict."""
    import yaml
    config_path = PROJECT_ROOT / "config" / "swe_team.yaml"
    if not config_path.is_file():
        return {}
    with open(config_path) as fh:
        return yaml.safe_load(fh) or {}


def _save_config_yaml(data: dict) -> None:
    """Save dict back to swe_team.yaml."""
    import yaml
    config_path = PROJECT_ROOT / "config" / "swe_team.yaml"
    with open(config_path, "w") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)


def cmd_project(args: argparse.Namespace) -> int:
    """Handle project subcommands."""
    action = getattr(args, "project_action", None)
    if action == "list":
        return _project_list(args)
    elif action == "init":
        return _project_init(args)
    elif action == "add-repo":
        return _project_add_repo(args)
    else:
        print("Unknown project action. Use: list, init, add-repo", file=sys.stderr)
        return 1


def _project_list(args: argparse.Namespace) -> int:
    """List all configured projects."""
    raw = _load_config_yaml()
    repos = raw.get("repos", [])

    if getattr(args, "json", False):
        print(json.dumps(repos, indent=2))
        return 0

    if not repos:
        print("No projects configured in config/swe_team.yaml.")
        return 0

    if HAS_RICH and _rich is not None:
        _rich.render_project_list(repos, _truncate)
        return 0

    # Plain text fallback
    header = f"{'NAME':<35} {'LOCAL PATH':<40} {'PRIORITY':<10} {'STATUS'}"
    print(header)
    print("-" * len(header))
    for r in repos:
        name = r.get("name", "?")
        local_path = r.get("local_path", "-")
        priority = r.get("priority", "medium")
        status = "monitor-only" if r.get("monitor_only", False) else "active"
        print(f"{_truncate(name, 33):<35} {_truncate(local_path, 38):<40} {priority:<10} {status}")

    print(f"\n{len(repos)} project(s)")
    return 0


def _project_init(args: argparse.Namespace) -> int:
    """Add a new project to config."""
    raw = _load_config_yaml()
    repos = raw.get("repos", [])

    name = args.name
    repo = getattr(args, "repo", name)
    local_path = getattr(args, "local_path", "")

    # Check duplicate
    for r in repos:
        if r.get("name") == name:
            print(f"Project {name!r} already exists.", file=sys.stderr)
            return 1

    new_entry = {
        "name": name,
        "local_path": local_path,
        "description": "",
        "priority": "medium",
    }
    repos.append(new_entry)
    raw["repos"] = repos
    _save_config_yaml(raw)
    print(f"Project {name!r} added to config/swe_team.yaml.")
    return 0


def _project_add_repo(args: argparse.Namespace) -> int:
    """Add a GitHub repo to an existing project."""
    raw = _load_config_yaml()
    repos = raw.get("repos", [])

    name = args.name
    repo = getattr(args, "repo", "")

    for r in repos:
        if r.get("name") == name:
            r["name"] = repo if repo else r["name"]
            raw["repos"] = repos
            _save_config_yaml(raw)
            print(f"Project {name!r} updated with repo {repo!r}.")
            return 0

    print(f"Project {name!r} not found.", file=sys.stderr)
    return 1


def cmd_repo_configure(args: argparse.Namespace) -> int:
    """Show/set config for a repo."""
    raw = _load_config_yaml()
    repos = raw.get("repos", [])
    repo_name = args.repo

    for r in repos:
        if r.get("name") == repo_name:
            print(json.dumps(r, indent=2))
            return 0

    print(f"Repo {repo_name!r} not found in config.", file=sys.stderr)
    return 1


def cmd_governor(args: argparse.Namespace) -> int:
    """Show usage governor information."""
    action = getattr(args, "governor_action", None)
    if not action:
        print("Usage: swe_cli governor {status|decision|schedule|summary|alerts}", file=sys.stderr)
        return 1

    # Load governor
    try:
        raw = _load_config_yaml()
        providers_cfg = raw.get("providers", {})
        gov_cfg = providers_cfg.get("usage_governor")
        if not gov_cfg:
            print(
                "Usage governor is not configured. "
                "Add providers.usage_governor to swe_team.yaml",
                file=sys.stderr,
            )
            return 1
        from src.swe_team.providers.usage_governor import create_usage_governor
        governor = create_usage_governor(gov_cfg)
    except Exception as exc:
        print(f"Failed to create usage governor: {exc}", file=sys.stderr)
        return 1

    if action == "status":
        return _governor_status(governor)
    elif action == "decision":
        return _governor_decision(governor)
    elif action == "schedule":
        return _governor_schedule(governor)
    elif action == "summary":
        return _governor_summary(governor)
    elif action == "alerts":
        return _governor_alerts(governor)
    else:
        print(f"Unknown governor action: {action}", file=sys.stderr)
        return 1


def _governor_status(governor) -> int:
    """Show QuotaStatus."""
    status = governor.get_quota_status()
    used_pct = 100.0 - status.remaining_pct
    if status.estimated_hours_until_exhaustion is not None:
        hours = status.estimated_hours_until_exhaustion
        days = hours / 24
        exhaustion = f"{hours:.0f}h ({days:.1f} days)"
    else:
        exhaustion = "N/A (no burn rate)"

    print("=== Usage Governor Status ===")
    print(f"Quota Used:     {status.total_tokens_used:,} / {status.quota_limit:,} tokens ({used_pct:.1f}%)")
    print(f"Burn Rate:      {status.burn_rate_tokens_per_hour:,.0f} tokens/hour")
    print(f"Est. Exhaustion: {exhaustion}")
    print(f"Period:         {status.current_period}")
    return 0


def _governor_decision(governor) -> int:
    """Show ConcurrencyDecision."""
    decision = governor.get_concurrency_decision()
    print("=== Concurrency Decision ===")
    print(f"Max Parallel Agents: {decision.max_parallel_agents}")
    print(f"Priority Floor:      {decision.priority_floor}")
    print(f"Allow New Work:      {'yes' if decision.allow_new_work else 'no'}")
    print(f"Reason:              {decision.reason}")
    return 0


def _governor_schedule(governor) -> int:
    """Show current schedule window."""
    scheduler = governor._scheduler
    if scheduler is None:
        print("=== Schedule Window ===")
        print("No scheduler configured.")
        return 0

    window = scheduler.get_current_window()
    is_peak = scheduler.is_peak_hours()
    is_weekend = scheduler.is_weekend()

    # Calculate next window change
    now = scheduler._now()
    if window.start_hour <= window.end_hour:
        # Normal window: ends at end_hour
        next_hour = window.end_hour
    else:
        # Overnight window
        if now.hour >= window.start_hour:
            next_hour = 24  # midnight, then continues to end_hour next day
        else:
            next_hour = window.end_hour

    hours_until = next_hour - now.hour - 1
    mins_until = 60 - now.minute
    if mins_until == 60:
        hours_until += 1
        mins_until = 0
    if hours_until < 0:
        next_change_str = "unknown"
    else:
        next_change_str = f"in {hours_until}h {mins_until}m"

    print("=== Schedule Window ===")
    print(f"Current Window:  {window.name}")
    print(f"Multiplier:      {window.concurrency_multiplier}x")
    print(f"Is Peak:         {'yes' if is_peak else 'no'}")
    print(f"Is Weekend:      {'yes' if is_weekend else 'no'}")
    print(f"Next Change:     {next_change_str}")
    return 0


def _governor_summary(governor) -> int:
    """Show daily summary."""
    print("=== Daily Usage Summary ===")
    print(governor.get_daily_summary())
    return 0


def _governor_alerts(governor) -> int:
    """Show active alerts."""
    print("=== Active Alerts ===")
    alerts = governor.check_alerts()
    if not alerts:
        print("No active alerts")
    else:
        for alert in alerts:
            print(f"  - {alert}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the live dashboard web server."""
    from scripts.ops.dashboard_server import main as serve_main
    sys.argv = ["dashboard_server", "--port", str(args.port), "--host", args.host]
    serve_main()
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def cmd_session(args: argparse.Namespace) -> int:
    """List Claude Code sessions."""
    from src.swe_team.session_store import SessionStore

    try:
        store = SessionStore()
    except Exception as exc:
        print(f"Error loading session store: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "all", False):
        sessions = store.list_all()
    else:
        sessions = store.list_active()

    if getattr(args, "json", False):
        print(json.dumps([s.to_dict() for s in sessions], indent=2))
        return 0

    if not sessions:
        print("No sessions found.")
        return 0

    import time as _time
    header = f"{'SESSION ID':<40} {'TICKET':<12} {'AGENT':<14} {'STATUS':<12} {'AGE'}"
    print(header)
    print("-" * len(header))
    now = _time.time()
    for s in sessions:
        age_s = now - s.created_at
        if age_s < 60:
            age_str = f"{int(age_s)}s"
        elif age_s < 3600:
            age_str = f"{int(age_s / 60)}m"
        else:
            age_str = f"{age_s / 3600:.1f}h"
        print(
            f"{_truncate(s.session_id, 40):<40} "
            f"{_truncate(s.ticket_id, 12):<12} "
            f"{s.agent_type:<14} "
            f"{s.status:<12} "
            f"{age_str}"
        )
    print(f"\n{len(sessions)} session(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="swe_cli",
        description="SWE Squad CLI — operational queries and reporting",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # ── status ────────────────────────────────────────────────────────────
    sp_status = subparsers.add_parser("status", help="Current system status")
    sp_status.add_argument("--json", action="store_true", help="JSON output")
    sp_status.set_defaults(func=cmd_status)

    # ── tickets ───────────────────────────────────────────────────────────
    sp_tickets = subparsers.add_parser("tickets", help="List tickets")
    sp_tickets.add_argument("--status", help="Filter by status (e.g. investigating)")
    sp_tickets.add_argument("--severity", help="Filter by severity (e.g. critical)")
    sp_tickets.add_argument("--team", help="Filter by assigned team/agent")
    sp_tickets.add_argument("--json", action="store_true", help="JSON output")
    sp_tickets.set_defaults(func=cmd_tickets)

    # ── issues ────────────────────────────────────────────────────────────
    sp_issues = subparsers.add_parser(
        "issues", help="List GitHub issues assigned to the team"
    )
    sp_issues.add_argument("--json", action="store_true", help="JSON output")
    sp_issues.set_defaults(func=cmd_issues)

    # ── repos ─────────────────────────────────────────────────────────────
    sp_repos = subparsers.add_parser(
        "repos", help="List repos the bot account has access to"
    )
    sp_repos.add_argument("--json", action="store_true", help="JSON output")
    sp_repos.set_defaults(func=cmd_repos)

    # ── summary ───────────────────────────────────────────────────────────
    sp_summary = subparsers.add_parser("summary", help="Generate summary report")
    sp_summary.add_argument("--json", action="store_true", help="JSON output")
    sp_summary.set_defaults(func=cmd_summary)

    # ── dashboard ─────────────────────────────────────────────────────────
    sp_dashboard = subparsers.add_parser(
        "dashboard", help="Generate observability dashboard"
    )
    sp_dashboard.add_argument("--json", action="store_true", help="JSON output (default)")
    sp_dashboard.add_argument(
        "--html", action="store_true", help="Output self-contained HTML dashboard"
    )
    sp_dashboard.set_defaults(func=cmd_dashboard)

    # ── report ────────────────────────────────────────────────────────────
    sp_report = subparsers.add_parser("report", help="Send report via Telegram")
    sp_report.add_argument(
        "report_type",
        choices=["daily", "status", "cycle", "dashboard"],
        help="Report type: daily, status, cycle, or dashboard",
    )
    sp_report.set_defaults(func=cmd_report)

    # ── auth ──────────────────────────────────────────────────────────────
    sp_auth = subparsers.add_parser("auth", help="Provider authentication status")
    auth_sub = sp_auth.add_subparsers(dest="auth_action")
    sp_auth_status = auth_sub.add_parser("status", help="Show per-provider auth state")
    sp_auth_status.add_argument("--json", action="store_true", help="JSON output")
    sp_auth.set_defaults(func=cmd_auth)

    # ── roles ─────────────────────────────────────────────────────────────
    sp_roles = subparsers.add_parser("roles", help="Display RBAC role definitions")
    sp_roles.set_defaults(func=cmd_roles)

    # ── costs ─────────────────────────────────────────────────────────────
    sp_costs = subparsers.add_parser("costs", help="Show token usage and cost summary")
    sp_costs.add_argument("--json", action="store_true", help="JSON output")
    sp_costs.set_defaults(func=cmd_costs)

    # ── ops ──────────────────────────────────────────────────────────────
    sp_ops = subparsers.add_parser("ops", help="Multi-project operations status")
    sp_ops.set_defaults(func=cmd_ops)

    # ── project ──────────────────────────────────────────────────────
    sp_project = subparsers.add_parser("project", help="Manage projects/repos")
    project_sub = sp_project.add_subparsers(dest="project_action")

    sp_proj_list = project_sub.add_parser("list", help="List all configured projects")
    sp_proj_list.add_argument("--json", action="store_true", help="JSON output")

    sp_proj_init = project_sub.add_parser("init", help="Add a new project to config")
    sp_proj_init.add_argument("name", help="Project name (e.g. owner/repo)")
    sp_proj_init.add_argument("--repo", default="", help="GitHub repo (owner/repo)")
    sp_proj_init.add_argument("--local-path", dest="local_path", default="", help="Local clone path")

    sp_proj_add_repo = project_sub.add_parser("add-repo", help="Add GitHub repo to existing project")
    sp_proj_add_repo.add_argument("name", help="Existing project name")
    sp_proj_add_repo.add_argument("--repo", required=True, help="GitHub repo (owner/repo)")

    sp_project.set_defaults(func=cmd_project)

    # ── repo ────────────────────────────────────────────────────────
    sp_repo = subparsers.add_parser("repo", help="Repo configuration")
    repo_sub = sp_repo.add_subparsers(dest="repo_action")

    sp_repo_configure = repo_sub.add_parser("configure", help="Show/set config for a repo")
    sp_repo_configure.add_argument("repo", help="Repo name (e.g. owner/repo)")
    sp_repo.set_defaults(func=cmd_repo_configure)

    # serve — live dashboard web server
    # ── session ────────────────────────────────────────────────────────
    sp_session = subparsers.add_parser("session", help="Session lifecycle management")
    session_sub = sp_session.add_subparsers(dest="session_action")
    sp_session_list = session_sub.add_parser("list", help="List active sessions")
    sp_session_list.add_argument("--all", action="store_true", help="Show all sessions (not just active)")
    sp_session_list.add_argument("--json", action="store_true", help="JSON output")
    sp_session.set_defaults(func=cmd_session)

    # ── governor ──────────────────────────────────────────────────────
    sp_governor = subparsers.add_parser("governor", help="Usage governor status and controls")
    governor_sub = sp_governor.add_subparsers(dest="governor_action")
    governor_sub.add_parser("status", help="Show quota usage status")
    governor_sub.add_parser("decision", help="Show concurrency decision")
    governor_sub.add_parser("schedule", help="Show current schedule window")
    governor_sub.add_parser("summary", help="Show daily usage summary")
    governor_sub.add_parser("alerts", help="Show active alerts")
    sp_governor.set_defaults(func=cmd_governor)

    p_serve = subparsers.add_parser("serve", help="Start live dashboard web server")
    p_serve.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
