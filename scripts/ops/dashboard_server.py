#!/usr/bin/env python3
"""
SWE-Squad Live Dashboard Server

Serves the dashboard HTML at http://0.0.0.0:PORT/ with auto-refresh every 60s.
Generates fresh data on each request — no caching layer needed.

Usage:
    python3 scripts/ops/dashboard_server.py [--port 8080] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import csv
import html as html_mod
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
load_dotenv()

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.config import load_config
from src.swe_team.ticket_store import TicketStore
from src.swe_team.token_tracker import TokenTracker
from src.swe_team.providers.usage_monitor.pricing import load_pricing, save_pricing

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = PROJECT_ROOT / "templates"
_DEFAULT_PORT = 8080
_REFRESH_SECONDS = 60
_JOBS_DIR = PROJECT_ROOT / "data" / "swe_team"
_STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"
_JOBS_PATH = PROJECT_ROOT / "data" / "swe_team" / "jobs.json"
_ROLES_PATH = PROJECT_ROOT / "config" / "swe_team" / "roles.yaml"
_CONFIG_PATH = PROJECT_ROOT / "config" / "swe_team.yaml"
_SETTINGS_PATH = PROJECT_ROOT / "data" / "swe_team" / "dashboard_settings.json"
_RUN_HISTORY_PATH = PROJECT_ROOT / "data" / "swe_team" / "run_history.jsonl"
_TOKEN_USAGE_PATH = PROJECT_ROOT / "data" / "swe_team" / "token_usage.jsonl"

_token_tracker_instance: "TokenTracker | None" = None

# ---------------------------------------------------------------------------
# GitHub OAuth configuration (all optional — auth is disabled if CLIENT_ID
# is absent so the dashboard works without credentials configured).
# ---------------------------------------------------------------------------
_OAUTH_CLIENT_ID: str = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
_OAUTH_CLIENT_SECRET: str = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
_OAUTH_COOKIE_SECRET: str = os.environ.get(
    "DASHBOARD_COOKIE_SECRET", secrets.token_hex(32)
)
_OAUTH_ALLOWED_ORGS: list = [
    o.strip()
    for o in os.environ.get("DASHBOARD_ALLOWED_ORGS", "").split(",")
    if o.strip()
]
_OAUTH_ENABLED: bool = bool(_OAUTH_CLIENT_ID and _OAUTH_CLIENT_SECRET)

_oauth_provider = None
if _OAUTH_ENABLED:
    try:
        from src.swe_team.providers.auth.github_oauth import GitHubOAuthProvider
        _oauth_provider = GitHubOAuthProvider(
            client_id=_OAUTH_CLIENT_ID,
            client_secret=_OAUTH_CLIENT_SECRET,
            allowed_orgs=_OAUTH_ALLOWED_ORGS,
            cookie_secret=_OAUTH_COOKIE_SECRET,
        )
    except Exception:
        logger.exception("Failed to initialise GitHubOAuthProvider — auth disabled")


# ---------------------------------------------------------------------------
# UserStore singleton (multi-user account system with encrypted secrets)
# ---------------------------------------------------------------------------
_user_store_instance = None
_user_store_lock = threading.Lock()

_WEBUI_DB_PATH: str = str(PROJECT_ROOT / "data" / "swe_team" / "webui_users.db")


def _get_user_store():
    """Lazy-initialise the UserStore singleton."""
    global _user_store_instance
    if _user_store_instance is not None:
        return _user_store_instance
    with _user_store_lock:
        if _user_store_instance is None:
            try:
                from src.swe_team.webui.user_store import UserStore
                _user_store_instance = UserStore(db_path=_WEBUI_DB_PATH)
            except Exception:
                logger.exception("Failed to initialise UserStore — user/secrets API disabled")
    return _user_store_instance


def _get_token_tracker() -> "TokenTracker":
    global _token_tracker_instance
    if _token_tracker_instance is None:
        _token_tracker_instance = TokenTracker(_TOKEN_USAGE_PATH)
    return _token_tracker_instance


_governor_instance = None
_governor_configured: bool | None = None


def _get_governor():
    """Lazy singleton for the UsageGovernor. Returns None if not configured."""
    global _governor_instance, _governor_configured
    if _governor_configured is not None:
        return _governor_instance
    try:
        import yaml
        from src.swe_team.providers.usage_governor import create_usage_governor

        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        gov_cfg = raw.get("providers", {}).get("usage_governor")
        if not gov_cfg:
            _governor_configured = False
            return None
        _governor_instance = create_usage_governor(gov_cfg)
        _governor_configured = True
        return _governor_instance
    except Exception:
        logger.exception("Failed to initialize UsageGovernor")
        _governor_configured = False
        return None


def _get_governor_status() -> dict:
    """Build the full governor status dict, or an error dict if not configured."""
    import dataclasses

    gov = _get_governor()
    if gov is None:
        return {"error": "Governor not configured", "configured": False}

    quota = dataclasses.asdict(gov.get_quota_status())
    decision = dataclasses.asdict(gov.get_concurrency_decision())
    alerts = gov.check_alerts()

    # Schedule info
    schedule = {"current_window": "default", "concurrency_multiplier": 1.0, "is_peak": False, "is_weekend": False}
    if gov._scheduler:
        window = gov._scheduler.get_current_window()
        schedule = {
            "current_window": window.name,
            "concurrency_multiplier": window.concurrency_multiplier,
            "is_peak": gov._scheduler.is_peak_hours(),
            "is_weekend": gov._scheduler.is_weekend(),
        }

    # Bonus info
    bonus = {"active": False, "multiplier": 1.0}
    if gov._bonus_detector:
        bonus = {
            "active": gov._bonus_detector.is_bonus_active(),
            "multiplier": gov._bonus_detector.get_multiplier(),
        }

    return {
        "quota": quota,
        "decision": decision,
        "schedule": schedule,
        "bonus": bonus,
        "alerts": alerts,
    }

# ---------------------------------------------------------------------------
# Optional control plane integration
# ---------------------------------------------------------------------------
try:
    from src.swe_team.control_plane_api import (
        handle_get as cp_handle_get,
        handle_post as cp_handle_post,
    )
    _HAS_CONTROL_PLANE = True
except Exception:
    _HAS_CONTROL_PLANE = False

    def cp_handle_get(handler, cp):  # type: ignore[misc]
        return False

    def cp_handle_post(handler, cp):  # type: ignore[misc]
        return False

# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) infrastructure
# ---------------------------------------------------------------------------
_sse_clients: list = []
_sse_lock = threading.Lock()
_last_status_mtime: float = 0.0


def _read_json_file(path: Path):
    """Read a JSON file, return parsed data or None on failure."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_roles_yaml() -> dict:
    """Read roles.yaml — uses PyYAML if available, else returns raw text."""
    try:
        import yaml
        return yaml.safe_load(_ROLES_PATH.read_text()) or {}
    except ImportError:
        try:
            return {"raw": _ROLES_PATH.read_text()}
        except Exception:
            return {"error": "roles.yaml not found"}
    except Exception:
        return {"error": "roles.yaml unreadable"}


_DEFAULT_SETTINGS: dict = {
    "theme": "dark",
    "refresh_interval": 30,
    "tickets_per_page": 25,
    "default_tab": "overview",
    "notifications_enabled": True,
    "notification_level": "errors",
}


def _read_settings() -> dict:
    """Read dashboard settings from JSON file, returning defaults if missing."""
    try:
        saved = json.loads(_SETTINGS_PATH.read_text())
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(saved)
        return merged
    except Exception:
        return dict(_DEFAULT_SETTINGS)


def _write_settings(settings: dict) -> bool:
    """Write dashboard settings to JSON file."""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(settings)
        _SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
        return True
    except Exception:
        logger.exception("Failed to write settings")
        return False


def _load_projects_from_config() -> list:
    """Read ``repos:`` from swe_team.yaml and return a list of project dicts.

    Each project dict gets an ``enabled`` key (default True) injected so
    callers always have a consistent shape.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        return []
    projects = []
    for repo in raw.get("repos", []):
        entry = dict(repo)
        entry.setdefault("enabled", True)
        projects.append(entry)
    return projects


def _save_project_to_config(project: dict) -> bool:
    """Append *project* to the ``repos:`` list in swe_team.yaml.

    Returns ``False`` (without writing) if a project with the same ``name``
    already exists; ``True`` on success.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        repos = raw.get("repos", [])
        name = project.get("name", "")
        for r in repos:
            if r.get("name") == name:
                return False
        repos.append({k: v for k, v in project.items() if k != "enabled"})
        raw["repos"] = repos
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to save project to config")
        return False


def _delete_project_from_config(name: str) -> bool:
    """Remove the project identified by *name* from swe_team.yaml.

    Returns ``True`` if the project was found and removed, ``False`` if it
    was not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        repos = raw.get("repos", [])
        new_repos = [r for r in repos if r.get("name") != name]
        if len(new_repos) == len(repos):
            return False
        raw["repos"] = new_repos
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to delete project from config")
        return False


def _build_scheduler_history() -> list:
    """Build scheduler job execution history for the Gantt timeline.

    Reads the last 20 entries from run_history.jsonl if available,
    otherwise synthesizes entries from the current status.json.
    """
    entries: list = []

    # Try to read from run_history.jsonl
    if _RUN_HISTORY_PATH.exists():
        try:
            lines = _RUN_HISTORY_PATH.read_text().strip().splitlines()
            for line in lines[-20:]:
                try:
                    rec = json.loads(line)
                    entries.append({
                        "job": rec.get("job_name", rec.get("job_id", "unknown")),
                        "ticket_id": rec.get("ticket_id"),
                        "started_at": rec.get("started_at", rec.get("timestamp", "")),
                        "ended_at": rec.get("ended_at", rec.get("completed_at", "")),
                        "status": rec.get("status", rec.get("result", "ok")),
                    })
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # Fallback: synthesize from status.json
    if not entries:
        status = _read_json_file(_STATUS_PATH) or {}
        now = datetime.now(timezone.utc)
        last_cycle = status.get("last_cycle_time") or status.get("time")
        if last_cycle:
            entries.append({
                "job": "monitor_cycle",
                "ticket_id": None,
                "started_at": last_cycle,
                "ended_at": (now).isoformat(),
                "status": "ok",
            })
        # Synthesize from jobs.json
        jobs = _read_json_file(_JOBS_PATH)
        if isinstance(jobs, list):
            for j in jobs[:5]:
                lr = j.get("last_run")
                if lr:
                    entries.append({
                        "job": j.get("name", j.get("job_id", "job")),
                        "ticket_id": None,
                        "started_at": lr,
                        "ended_at": lr,
                        "status": j.get("status", "ok"),
                    })

    return entries[-20:]


def _build_roles_matrix() -> dict:
    """Build the RBAC permission matrix from env_allowlists in swe_team.yaml."""
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        return {"roles": [], "permissions": {}, "all_vars": [], "categories": {}}

    allowlists = raw.get("env_allowlists", {})
    # Filter out non-dict entries (notification, issue_tracker etc. are nested under providers)
    role_map: dict = {}
    for key, val in allowlists.items():
        if isinstance(val, list):
            role_map[key] = val

    # Collect all unique env vars
    all_vars = sorted({v for vlist in role_map.values() for v in vlist})

    # Categorize variables
    categories: dict = {}
    cat_map = {
        "GitHub": ["GH_TOKEN", "SWE_GITHUB_REPO", "SWE_GITHUB_ACCOUNT",
                    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"],
        "Base LLM": ["BASE_LLM_API_URL", "BASE_LLM_API_KEY", "EMBEDDING_MODEL",
                      "EXTRACTION_MODEL"],
        "Telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "Anthropic": ["ANTHROPIC_API_KEY"],
        "Core": ["SWE_TEAM_ID", "SWE_TEAM_CONFIG", "SWE_REPO_PATH",
                 "PYTHONPATH", "PATH", "HOME", "LANG"],
    }
    for var in all_vars:
        assigned = False
        for cat, members in cat_map.items():
            if var in members:
                categories.setdefault(cat, []).append(var)
                assigned = True
                break
        if not assigned:
            categories.setdefault("Other", []).append(var)

    return {
        "roles": list(role_map.keys()),
        "permissions": role_map,
        "all_vars": all_vars,
        "categories": categories,
    }


def _build_sse_payload() -> str:
    """Build JSON payload for SSE broadcast."""
    status = _read_json_file(_STATUS_PATH) or {}
    jobs = _read_json_file(_JOBS_PATH) or []
    payload: dict = {
        "status": status,
        "jobs": jobs,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        payload["governor"] = _get_governor_status()
    except Exception:
        payload["governor"] = {"error": "Governor not configured", "configured": False}
    return json.dumps(payload, default=str)


def _broadcast_sse_event(event_name: str, payload: dict) -> None:
    """Push a custom SSE event to all connected clients.

    Parameters
    ----------
    event_name:
        The SSE event type (e.g. ``action``, ``investigation_complete``).
    payload:
        JSON-serialisable dict sent as the ``data:`` field.
    """
    data_str = json.dumps(payload, default=str)
    msg = f"event: {event_name}\ndata: {data_str}\n\n"
    with _sse_lock:
        dead: list = []
        for wfile in _sse_clients:
            try:
                wfile.write(msg.encode())
                wfile.flush()
            except Exception:
                dead.append(wfile)
        for d in dead:
            _sse_clients.remove(d)


def _sse_broadcaster():
    """Background thread: polls status.json mtime, broadcasts on change or every 5s."""
    global _last_status_mtime
    last_broadcast = 0.0
    while True:
        time.sleep(2)
        now = time.time()
        try:
            mtime = _STATUS_PATH.stat().st_mtime if _STATUS_PATH.exists() else 0.0
        except OSError:
            mtime = 0.0
        changed = mtime != _last_status_mtime
        periodic = (now - last_broadcast) >= 5.0
        if changed or periodic:
            _last_status_mtime = mtime
            last_broadcast = now
            payload = _build_sse_payload()
            msg = f"event: update\ndata: {payload}\n\n"
            with _sse_lock:
                dead = []
                for wfile in _sse_clients:
                    try:
                        wfile.write(msg.encode())
                        wfile.flush()
                    except Exception:
                        dead.append(wfile)
                for d in dead:
                    _sse_clients.remove(d)


def _update_job(job_id: str, updates: dict) -> bool:
    """Update a scheduler job in jobs.json by job_id."""
    jobs = _read_json_file(_JOBS_PATH)
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if job.get("job_id") == job_id:
            job.update(updates)
            try:
                _JOBS_PATH.write_text(json.dumps(jobs, indent=2))
            except Exception:
                return False
            return True
    return False

# Max items for JSON HTML view before truncation
_JSON_VIEW_MAX_TICKETS = 200

# ── Dashboard response cache (30-second TTL) ──────────────────────────────────
# Avoids re-querying Supabase on every request to /data or /api/activity.
_DATA_CACHE_TTL: float = 30.0          # seconds
_data_cache: dict = {}                 # {"data": ..., "ts": float}
_data_cache_lock = threading.Lock()    # protects _data_cache under ThreadingHTTPServer

# ── Log file tail constants ────────────────────────────────────────────────────
_LOG_TAIL_BYTES: int = 102_400         # read at most 100 KiB from end of log
_JSON_VIEW_MAX_ACTIVITY: int = 30      # max activity entries returned by /api/activity


def _tail_log_file(
    path: Path,
    max_bytes: int = _LOG_TAIL_BYTES,
) -> list:
    """Read the last *max_bytes* bytes of *path* and return a list of text lines.

    This avoids loading the whole log file into memory on every request.
    If the file does not exist or cannot be read, returns an empty list.
    Binary/corrupt bytes are dropped with ``errors='replace'``.
    """
    if not path.exists():
        return []
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as fh:
            if file_size > max_bytes:
                fh.seek(-max_bytes, 2)  # seek from end
            raw = fh.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        # First line may be partial — drop it when we seeked into the middle
        lines = text.splitlines()
        if file_size > max_bytes and lines:
            lines = lines[1:]
        return lines
    except OSError:
        return []


def _get_cached_dashboard_data(store) -> dict:
    """Return dashboard data, using an in-process 30-second cache.

    The cache prevents repeated Supabase round-trips when multiple requests
    arrive within the TTL window (e.g. auto-refresh + manual reload).
    Thread-safe: uses _data_cache_lock for concurrent access under
    ThreadingHTTPServer. Includes a 10-second timeout for data fetches.
    """
    now = time.monotonic()
    with _data_cache_lock:
        cached = _data_cache.get("data")
        cached_ts = _data_cache.get("ts", 0.0)
        if cached is not None and (now - cached_ts) < _DATA_CACHE_TTL:
            return cached

    from scripts.ops.dashboard_data import generate_dashboard_data

    # Use a thread with timeout to prevent slow Supabase queries from blocking
    result_holder: list = []
    error_holder: list = []

    def _fetch():
        try:
            result_holder.append(generate_dashboard_data(store))
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=10.0)  # 10-second timeout for data fetch

    if t.is_alive():
        logger.warning("Dashboard data fetch timed out after 10s, serving stale cache")
        with _data_cache_lock:
            stale = _data_cache.get("data")
        return stale if stale is not None else {}

    if error_holder:
        logger.warning("Dashboard data fetch failed: %s", error_holder[0])
        with _data_cache_lock:
            stale = _data_cache.get("data")
        return stale if stale is not None else {}

    fresh = result_holder[0] if result_holder else {}
    with _data_cache_lock:
        _data_cache["data"] = fresh
        _data_cache["ts"] = now
    return fresh


def _load_store(config):
    """Load ticket store — Supabase if configured, else local JSON."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if supabase_url and supabase_key:
        try:
            from src.swe_team.supabase_store import SupabaseTicketStore
            return SupabaseTicketStore(
                supabase_url=supabase_url,
                supabase_key=supabase_key,
                team_id=config.team_id,
            )
        except Exception as exc:
            logger.warning("Supabase unavailable, falling back to local store: %s", exc)
    data_dir = PROJECT_ROOT / "data" / "swe_team"
    data_dir.mkdir(parents=True, exist_ok=True)
    return TicketStore(path=data_dir / "tickets.json")


def _render_dashboard(store) -> str:
    """Generate dashboard HTML with embedded fresh data."""
    data = _get_cached_dashboard_data(store)
    template_path = _TEMPLATES_DIR / "dashboard.html"

    if not template_path.exists():
        return f"<pre>Template not found: {template_path}</pre>"

    html = template_path.read_text(encoding="utf-8")

    # Inject live data by replacing the __DASHBOARD_DATA__ placeholder in the template
    data_json = json.dumps(data, indent=2, default=str)
    html = html.replace("__DASHBOARD_DATA__", data_json, 1)

    # Inject auto-refresh meta tag
    refresh_tag = f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">\n'
    html = html.replace("</head>", f"{refresh_tag}</head>", 1)
    return html


def _get_scheduler_and_store():
    """Get JobStore and JobScheduler instances for API handlers."""
    from src.swe_team.scheduler import JobStore, JobScheduler
    store = JobStore(_JOBS_DIR / "jobs.json")
    scheduler = JobScheduler(store=store)
    return store, scheduler


def _build_graph_data(store) -> dict:
    """Build ticket similarity graph data for /api/graph.

    Returns a dict with keys:
      - nodes: list of node dicts (id, title, severity, status, module, created_days_ago)
      - edges: list of edge dicts (source, target, similarity) where similarity >= 0.75
      - heatmap: dict with modules (list) and cells (2D list of counts)

    Node count is capped at 50. Edges use TF-IDF cosine similarity on ticket titles.
    """
    import math
    from datetime import datetime, timezone

    # Fetch all tickets and cap at 50
    tickets = store.list_all()[:50]

    now = datetime.now(timezone.utc)

    # Build nodes
    nodes = []
    for t in tickets:
        try:
            created = datetime.fromisoformat(t.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_ago = int((now - created).total_seconds() / 86400)
        except (ValueError, TypeError):
            days_ago = 0

        severity_val = t.severity.value if hasattr(t.severity, "value") else str(t.severity)
        status_val = t.status.value if hasattr(t.status, "value") else str(t.status)

        nodes.append({
            "id": t.ticket_id,
            "title": t.title,
            "severity": severity_val,
            "status": status_val,
            "module": t.source_module or "unknown",
            "created_days_ago": days_ago,
        })

    # Build TF-IDF vectors for title similarity
    def _tokenize(text: str) -> list:
        return re.findall(r"[a-z0-9]+", text.lower())

    n = len(nodes)
    if n == 0:
        return {"nodes": [], "edges": [], "heatmap": {"modules": [], "cells": []}}

    titles = [nodes[i]["title"] for i in range(n)]
    tokenized = [_tokenize(t) for t in titles]

    # Build document frequency
    df: dict = {}
    for toks in tokenized:
        for tok in set(toks):
            df[tok] = df.get(tok, 0) + 1

    vocab = list(df.keys())
    vocab_idx = {w: i for i, w in enumerate(vocab)}
    V = len(vocab)

    def _tfidf_vec(toks: list) -> list:
        tf: dict = {}
        for tok in toks:
            tf[tok] = tf.get(tok, 0) + 1
        vec = [0.0] * V
        for tok, cnt in tf.items():
            if tok in vocab_idx:
                idf = math.log((n + 1) / (df.get(tok, 0) + 1)) + 1.0
                vec[vocab_idx[tok]] = (cnt / len(toks)) * idf if toks else 0.0
        return vec

    def _cosine(a: list, b: list) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)

    vecs = [_tfidf_vec(toks) for toks in tokenized]

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine(vecs[i], vecs[j])
            if sim >= 0.75:
                edges.append({
                    "source": nodes[i]["id"],
                    "target": nodes[j]["id"],
                    "similarity": round(sim, 4),
                })

    # Build heatmap
    modules = sorted({nd["module"] for nd in nodes})
    mod_idx = {m: i for i, m in enumerate(modules)}
    M = len(modules)
    cells = [[0] * M for _ in range(M)]

    for i in range(n):
        for j in range(i, n):
            mi = mod_idx[nodes[i]["module"]]
            mj = mod_idx[nodes[j]["module"]]
            if i == j:
                # Diagonal: count each ticket once for its own module
                cells[mi][mi] += 1
            else:
                cells[mi][mj] += 1
                if mi != mj:
                    cells[mj][mi] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "heatmap": {"modules": modules, "cells": cells},
    }


class DashboardHandler(BaseHTTPRequestHandler):
    store = None        # set at startup
    auth_provider = None  # optional AuthProvider for /api/auth/status

    def address_string(self):
        """Override to skip reverse DNS lookup — fixes 10-15s latency on Tailscale (#257)."""
        return self.client_address[0]

    def log_message(self, fmt, *args):  # suppress default access log noise
        logger.debug("HTTP %s %s", self.address_string(), fmt % args)

    def _send_gzipped(self, content, content_type, cache_control=None):
        """Send response with gzip compression if the client supports it."""
        raw = content.encode("utf-8") if isinstance(content, str) else content
        accept_enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc:
            compressed = gzip.compress(raw)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        # Auth routes — always accessible (no session check)
        if path == "/auth/login":
            self._handle_auth_login(query)
            return
        if path == "/auth/callback":
            self._handle_auth_callback(query)
            return
        if path == "/auth/logout":
            self._handle_auth_logout()
            return

        # Auth middleware — exempt /health from the gate
        if path != "/health":
            user = self._check_auth()
            if user is None:
                self._redirect("/auth/login")
                return
        else:
            user = None

        # Control plane API routes (handled first for /api/* prefix)
        if _HAS_CONTROL_PLANE and getattr(self, "control_plane", None) and path.startswith("/api/") and path not in ("/api/activity", "/api/tickets", "/api/costs", "/api/stream", "/api/scheduler", "/api/rbac", "/api/status", "/api/projects", "/api/auth/status", "/api/graph", "/api/settings", "/api/scheduler/history", "/api/roles") and not path.startswith("/api/projects/") and not path.startswith("/api/costs/") and not path.startswith("/api/pricing") and not path.startswith("/api/governor") and not path.startswith("/api/tickets/") and not path.startswith("/api/pipeline/"):
            if cp_handle_get(self, self.control_plane):
                return

        if path in ("/", "/dashboard"):
            self._serve_dashboard()
        elif path == "/health":
            self._json_response({"status": "ok"})
        elif path == "/data":
            self._serve_json()
        elif path == "/api/activity":
            self._handle_api_activity()
        elif path == "/api/stream":
            self._handle_sse()
        elif path == "/api/scheduler":
            self._json_response(_read_json_file(_JOBS_PATH) or [], cache_control="public, max-age=15")
        elif path == "/api/rbac":
            self._json_response(_read_roles_yaml(), cache_control="public, max-age=60")
        elif path == "/api/auth/status":
            self._handle_api_auth_status()
        elif path == "/api/graph":
            self._handle_api_graph()
        elif path == "/api/settings":
            self._json_response(_read_settings(), cache_control="private, max-age=30")
        elif path == "/api/scheduler/history":
            self._json_response(_build_scheduler_history(), cache_control="public, max-age=30")
        elif path == "/api/roles":
            self._json_response(_build_roles_matrix(), cache_control="public, max-age=60")
        elif path == "/api/costs/by_hour":
            try:
                self._json_response(_get_token_tracker().by_hour(since_hours=48), cache_control="public, max-age=120")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_day":
            try:
                self._json_response(_get_token_tracker().by_day(since_days=30), cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_week":
            try:
                self._json_response(_get_token_tracker().by_week(since_weeks=12), cache_control="public, max-age=600")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_month":
            try:
                self._json_response(_get_token_tracker().by_month(since_months=6), cache_control="public, max-age=3600")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_agent":
            try:
                self._json_response(_get_token_tracker().by_agent(since_hours=168), cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_ticket":
            try:
                self._json_response(_get_token_tracker().by_ticket(since_hours=168), cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/roi":
            self._handle_costs_roi(query)
        elif path == "/api/costs/cache_efficiency":
            self._handle_cache_efficiency()
        elif path == "/api/pricing":
            self._json_response(load_pricing(), cache_control="public, max-age=3600")
        elif path == "/api/projects":
            self._handle_list_projects()
        elif path.startswith("/api/projects/"):
            project_name = path[len("/api/projects/"):]
            self._handle_get_project(project_name)
        elif path == "/costs":
            self._handle_costs()
        elif path == "/scheduler":
            self._handle_scheduler()
        elif path == "/api/jobs":
            self._handle_list_jobs_api()
        elif re.match(r"^/api/jobs/[^/]+/history$", path):
            self._handle_job_history_api()
        elif path == "/api/governor/status":
            self._json_response(_get_governor_status())
        elif path == "/api/governor/quota":
            self._handle_governor_quota()
        elif path == "/api/governor/decision":
            self._handle_governor_decision()
        elif path == "/api/governor/alerts":
            self._handle_governor_alerts()
        elif path == "/api/governor/summary":
            self._handle_governor_summary()
        # GET /api/tickets/export — CSV/JSON export
        elif path == "/api/tickets/export":
            self._handle_tickets_export(query)
        # GET /api/tickets/<id> — single ticket detail
        elif re.match(r"^/api/tickets/[^/]+$", path):
            ticket_id = path.split("/")[-1]
            self._handle_get_ticket(ticket_id)
        # --- Multi-user account API ---
        elif path == "/api/users/me":
            self._handle_get_me(user)
        elif path == "/api/secrets":
            self._handle_list_secrets(user)
        elif path == "/api/users":
            self._handle_list_users(user)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        """Handle POST requests for scheduler job actions and ticket actions."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return

        # Control plane POST routes
        if _HAS_CONTROL_PLANE and getattr(self, "control_plane", None) and cp_handle_post(self, self.control_plane):
            return

        # POST /api/pricing — save pricing config
        if path == "/api/pricing":
            body = self._read_post_body()
            try:
                save_pricing(body, str(PROJECT_ROOT / "config" / "pricing.json"))
                self._json_response({"ok": True, "pricing": body})
            except Exception as exc:
                logger.exception("Failed to save pricing")
                self._json_response({"error": str(exc)}, status=500)
            return

        # POST /api/pricing/reset — reset pricing to defaults
        if path == "/api/pricing/reset":
            try:
                from src.swe_team.providers.usage_monitor.pricing import DEFAULT_PRICING
                defaults = dict(DEFAULT_PRICING)
                save_pricing(defaults, str(PROJECT_ROOT / "config" / "pricing.json"))
                self._json_response(defaults)
            except Exception as exc:
                logger.exception("Failed to reset pricing")
                self._json_response({"error": str(exc)}, status=500)
            return

        # POST /api/settings — save dashboard settings
        if path == "/api/settings":
            body = self._read_post_body()
            if _write_settings(body):
                self._json_response({"ok": True, "settings": _read_settings()})
            else:
                self._json_response({"error": "Failed to save settings"}, status=500)
            return

        # POST /api/projects — add a new project
        if path == "/api/projects":
            self._handle_create_project()
            return

        # POST /api/jobs — create a new job
        if path == "/api/jobs":
            self._handle_create_job()
            return

        # POST /api/jobs/<id>/<action>
        m = re.match(r"^/api/jobs/([^/]+)/(pause|resume|cancel|trigger|delete)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            self._handle_job_action(job_id, action)
            return

        # POST /api/tickets/<id>/assign — assign a ticket
        m = re.match(r"^/api/tickets/([^/]+)/assign$", path)
        if m:
            self._handle_ticket_assign(m.group(1))
            return

        # POST /api/tickets/<id>/investigate — trigger investigation
        m = re.match(r"^/api/tickets/([^/]+)/investigate$", path)
        if m:
            self._handle_ticket_investigate(m.group(1))
            return

        # POST /api/tickets/<id>/develop — trigger developer agent
        m = re.match(r"^/api/tickets/([^/]+)/develop$", path)
        if m:
            self._handle_ticket_develop(m.group(1))
            return

        # POST /api/tickets/<id>/trigger — alias for investigate (used by existing UI)
        m = re.match(r"^/api/tickets/([^/]+)/trigger$", path)
        if m:
            self._handle_ticket_investigate(m.group(1))
            return

        # POST /api/tickets/<id>/comment — add comment
        m = re.match(r"^/api/tickets/([^/]+)/comment$", path)
        if m:
            self._handle_ticket_comment(m.group(1))
            return

        # POST /api/tickets/<id>/label — update labels
        m = re.match(r"^/api/tickets/([^/]+)/label$", path)
        if m:
            self._handle_ticket_label(m.group(1))
            return

        # POST /api/pipeline/trigger — trigger full pipeline cycle
        if path == "/api/pipeline/trigger":
            self._handle_pipeline_trigger()
            return

        # POST /api/secrets — create / update a secret
        if path == "/api/secrets":
            self._handle_create_secret(user)
            return

        self.send_error(404, "Not found")

    def do_PATCH(self):
        """Handle PATCH requests for ticket status and severity updates."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return

        # PATCH /api/tickets/<id>/status
        m = re.match(r"^/api/tickets/([^/]+)/status$", path)
        if m:
            self._handle_ticket_status(m.group(1))
            return

        # PATCH /api/tickets/<id>/severity
        m = re.match(r"^/api/tickets/([^/]+)/severity$", path)
        if m:
            self._handle_ticket_severity(m.group(1))
            return

        # PATCH /api/users/me/settings
        if path == "/api/users/me/settings":
            self._handle_update_my_settings(user)
            return

        self.send_error(404, "Not found")

    def do_DELETE(self):
        """Handle DELETE requests (currently: DELETE /api/projects/<name>)."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return
        if path.startswith("/api/projects/"):
            project_name = path[len("/api/projects/"):]
            self._handle_delete_project(project_name)
            return
        # DELETE /api/secrets/<name>
        if path.startswith("/api/secrets/"):
            secret_name = path[len("/api/secrets/"):]
            self._handle_delete_secret(user, secret_name)
            return
        self.send_error(404, "Not found")

    # --- Multi-user account API helpers ---

    def _handle_get_me(self, session_user: Optional[dict]) -> None:
        """GET /api/users/me — return the current user's profile from UserStore."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            # UserStore not available — return session-only profile
            self._json_response({
                "github_login": login,
                "name": session_user.get("name", ""),
                "orgs": session_user.get("orgs", []),
                "role": "user",
            })
            return
        user = us.get_user(login)
        if user is None:
            # Auto-provision on first access via API (e.g. if OAuth callback missed it)
            user = us.get_or_create_user(login)
        self._json_response(user)

    def _handle_update_my_settings(self, session_user: Optional[dict]) -> None:
        """PATCH /api/users/me/settings — update the current user's settings."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        try:
            result = us.update_settings(login, body)
            self._json_response({"ok": True, "settings": result})
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Error updating user settings")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_secrets(self, session_user: Optional[dict]) -> None:
        """GET /api/secrets — list secret names (never values) for the current user."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            # Auto-provision if needed
            if us.get_user(login) is None:
                us.get_or_create_user(login)
            names = us.list_secret_names(login)
            self._json_response({"secrets": names})
        except Exception as exc:
            logger.exception("Error listing secrets")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_secret(self, session_user: Optional[dict]) -> None:
        """POST /api/secrets — store an encrypted secret for the current user.

        Body: {"name": "KEY_NAME", "value": "secret_value"}
        The secret value is never returned in any response.
        """
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        name = (body.get("name") or "").strip()
        value = body.get("value", "")
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        if not isinstance(value, str) or not value:
            self._json_response({"error": "Field 'value' must be a non-empty string"}, status=400)
            return
        try:
            if us.get_user(login) is None:
                us.get_or_create_user(login)
            us.set_secret(login, name, value)
            self._json_response({"ok": True, "name": name}, status=201)
        except Exception as exc:
            logger.exception("Error storing secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_secret(self, session_user: Optional[dict], secret_name: str) -> None:
        """DELETE /api/secrets/<name> — delete a secret for the current user."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        if not secret_name:
            self._json_response({"error": "Secret name required in URL"}, status=400)
            return
        try:
            deleted = us.delete_secret(login, secret_name)
            if deleted:
                self._json_response({"ok": True, "deleted": secret_name})
            else:
                self._json_response({"error": f"Secret {secret_name!r} not found"}, status=404)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Error deleting secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_users(self, session_user: Optional[dict]) -> None:
        """GET /api/users — admin only, return all users."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        # Check admin role
        user_record = us.get_user(login)
        if user_record is None:
            user_record = us.get_or_create_user(login)
        if user_record.get("role") != "admin":
            self._json_response({"error": "Forbidden — admin only"}, status=403)
            return
        self._json_response(us.list_users())

    # --- Projects API helpers ---

    def _handle_list_projects(self):
        """GET /api/projects — return all configured projects as a JSON list."""
        projects = _load_projects_from_config()
        self._json_response(projects)

    def _handle_get_project(self, name: str):
        """GET /api/projects/<name> — return a single project or 404."""
        projects = _load_projects_from_config()
        for p in projects:
            if p.get("name") == name:
                self._json_response(p)
                return
        self._json_response({"error": f"Project {name!r} not found"}, status=404)

    def _handle_create_project(self):
        """POST /api/projects — add a new project to config."""
        body = self._read_post_body()
        name = body.get("name", "").strip()
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        ok = _save_project_to_config(body)
        if not ok:
            self._json_response({"error": f"Project {name!r} already exists"}, status=409)
            return
        project = dict(body)
        project.setdefault("enabled", True)
        self._json_response({"ok": True, "project": project}, status=201)

    def _handle_delete_project(self, name: str):
        """DELETE /api/projects/<name> — remove a project from config."""
        ok = _delete_project_from_config(name)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "deleted": name})

    # --- Scheduler API helpers ---

    def _read_post_body(self) -> dict:
        """Read and parse JSON POST body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _handle_job_action(self, job_id: str, action: str):
        """Handle pause/resume/cancel/trigger/delete actions on a job."""
        try:
            _store, sched = _get_scheduler_and_store()
            if action == "delete":
                deleted = sched.delete_job(job_id)
                if not deleted:
                    self._json_response({"error": f"Job {job_id} not found"}, status=404)
                    return
                self._json_response({"ok": True, "deleted": job_id})
                return

            method = getattr(sched, f"{action}_job", None)
            if method is None:
                self._json_response({"error": f"Unknown action: {action}"}, status=400)
                return
            job = method(job_id)
            if job is None:
                self._json_response(
                    {"error": f"Job {job_id} not found or action not applicable"},
                    status=404,
                )
                return
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Job action %s/%s error", job_id, action)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_job(self):
        """Handle POST /api/jobs to create a new job."""
        try:
            from src.swe_team.scheduler import ScheduledJob
            body = self._read_post_body()
            if not body.get("name"):
                self._json_response({"error": "Job name is required"}, status=400)
                return
            job = ScheduledJob.from_dict(body)
            _store, sched = _get_scheduler_and_store()
            job = sched.add_job(job)
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Create job error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_jobs_api(self):
        """GET /api/jobs — return all jobs as JSON."""
        try:
            from src.swe_team.scheduler import JobStore
            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()
            self._json_response([j.to_dict() for j in jobs])
        except Exception as exc:
            logger.exception("List jobs API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_job_history_api(self):
        """GET /api/jobs/<id>/history — return run history for a job."""
        try:
            from src.swe_team.scheduler import RunHistoryStore
            parsed = urlparse(self.path)
            m = re.match(r"^/api/jobs/([^/]+)/history$", parsed.path)
            if not m:
                self._json_response({"error": "Not found", "status": 404}, status=404)
                return
            job_id = m.group(1)
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            records = history_store.get_history(job_id=job_id, limit=50)
            self._json_response([r.to_dict() for r in records])
        except Exception as exc:
            logger.exception("Job history API error")
            self._json_response({"error": str(exc)}, status=500)

    # --- Governor API helpers ---

    def _handle_governor_quota(self):
        """GET /api/governor/quota — return just QuotaStatus."""
        import dataclasses
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response(dataclasses.asdict(gov.get_quota_status()))

    def _handle_governor_decision(self):
        """GET /api/governor/decision — return just ConcurrencyDecision."""
        import dataclasses
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response(dataclasses.asdict(gov.get_concurrency_decision()))

    def _handle_governor_alerts(self):
        """GET /api/governor/alerts — return list of active alert strings."""
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response(gov.check_alerts())

    def _handle_governor_summary(self):
        """GET /api/governor/summary — return daily summary text."""
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response({"summary": gov.get_daily_summary()})

    # --- Ticket action API handlers ---

    def _handle_get_ticket(self, ticket_id: str):
        """GET /api/tickets/<id> — return full ticket detail as JSON."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        self._json_response(ticket.to_dict())

    def _handle_tickets_export(self, query: dict):
        """GET /api/tickets/export — export tickets as CSV."""
        fmt = query.get("format", ["csv"])[0]
        tickets = self.store.list_all()
        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["ticket_id", "title", "severity", "status", "assigned_to",
                             "source_module", "created_at", "updated_at"])
            for t in tickets:
                writer.writerow([
                    t.ticket_id, t.title,
                    t.severity.value if hasattr(t.severity, "value") else str(t.severity),
                    t.status.value if hasattr(t.status, "value") else str(t.status),
                    t.assigned_to or "", t.source_module or "",
                    t.created_at, t.updated_at,
                ])
            body = output.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=tickets.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json_response([t.to_dict() for t in tickets])

    def _handle_ticket_assign(self, ticket_id: str):
        """POST /api/tickets/<id>/assign — assign ticket to an agent."""
        body = self._read_post_body()
        assignee = body.get("assignee", "").strip()
        if not assignee:
            self._json_response({"error": "Field 'assignee' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        ticket.assigned_to = assignee
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue if present
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket assigned to **{assignee}** via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "ticket_assigned",
            "ticket_id": ticket_id,
            "assignee": assignee,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "assignee": assignee})

    def _handle_ticket_investigate(self, ticket_id: str):
        """POST /api/tickets/<id>/investigate — trigger investigation in background."""
        body = self._read_post_body()
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        model = body.get("model", "sonnet")

        def _run():
            try:
                from src.swe_team.investigator import InvestigatorAgent
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                agent = InvestigatorAgent(config=cfg, ticket_store=self.store)
                agent.investigate(ticket, model=model)
                _broadcast_sse_event("action", {
                    "event": "investigation_complete",
                    "ticket_id": ticket_id,
                })
            except Exception as exc:
                logger.exception("Background investigation failed for %s", ticket_id)
                _broadcast_sse_event("action", {
                    "event": "investigation_failed",
                    "ticket_id": ticket_id,
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name=f"investigate-{ticket_id}")
        thread.start()
        self._json_response({"status": "queued", "ticket_id": ticket_id, "action": "investigate"})

    def _handle_ticket_develop(self, ticket_id: str):
        """POST /api/tickets/<id>/develop — trigger developer agent in background."""
        body = self._read_post_body()
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        model = body.get("model", "sonnet")

        def _run():
            try:
                from src.swe_team.developer import DeveloperAgent
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                agent = DeveloperAgent(config=cfg, ticket_store=self.store)
                agent.attempt_fix(ticket, model=model)
                _broadcast_sse_event("action", {
                    "event": "development_complete",
                    "ticket_id": ticket_id,
                })
            except Exception as exc:
                logger.exception("Background development failed for %s", ticket_id)
                _broadcast_sse_event("action", {
                    "event": "development_failed",
                    "ticket_id": ticket_id,
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name=f"develop-{ticket_id}")
        thread.start()
        self._json_response({"status": "queued", "ticket_id": ticket_id, "action": "develop"})

    def _handle_ticket_status(self, ticket_id: str):
        """PATCH /api/tickets/<id>/status — update ticket status."""
        body = self._read_post_body()
        new_status_str = body.get("status", "").strip().lower()
        if not new_status_str:
            self._json_response({"error": "Field 'status' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Validate the status value
        from src.swe_team.models import TicketStatus
        try:
            new_status = TicketStatus(new_status_str)
        except ValueError:
            valid = [s.value for s in TicketStatus]
            self._json_response(
                {"error": f"Invalid status '{new_status_str}'. Valid: {valid}"},
                status=400,
            )
            return

        # If a resolution_note is provided, set it before transition (for bypass)
        if body.get("resolution_note"):
            ticket.metadata["resolution_note"] = body["resolution_note"]

        try:
            ticket.transition(new_status)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=422)
            return

        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket status changed to **{new_status.value}** via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "status_changed",
            "ticket_id": ticket_id,
            "status": new_status.value,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "new_status": new_status.value})

    def _handle_ticket_severity(self, ticket_id: str):
        """PATCH /api/tickets/<id>/severity — update ticket severity."""
        body = self._read_post_body()
        new_sev_str = body.get("severity", "").strip().lower()
        if not new_sev_str:
            self._json_response({"error": "Field 'severity' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        from src.swe_team.models import TicketSeverity
        try:
            new_severity = TicketSeverity(new_sev_str)
        except ValueError:
            valid = [s.value for s in TicketSeverity]
            self._json_response(
                {"error": f"Invalid severity '{new_sev_str}'. Valid: {valid}"},
                status=400,
            )
            return

        ticket.severity = new_severity
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "severity_changed",
            "ticket_id": ticket_id,
            "severity": new_severity.value,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "new_severity": new_severity.value})

    def _handle_ticket_comment(self, ticket_id: str):
        """POST /api/tickets/<id>/comment — add comment to ticket and GitHub."""
        body = self._read_post_body()
        comment_text = body.get("comment", "").strip()
        if not comment_text:
            self._json_response({"error": "Field 'comment' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Store comment in ticket metadata
        comments = ticket.metadata.get("comments", [])
        comments.append({
            "text": comment_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "dashboard",
        })
        ticket.metadata["comments"] = comments
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(gh_number, comment_text)

        _broadcast_sse_event("action", {
            "event": "comment_added",
            "ticket_id": ticket_id,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id})

    def _handle_ticket_label(self, ticket_id: str):
        """POST /api/tickets/<id>/label — update labels on ticket and GitHub."""
        body = self._read_post_body()
        add_labels = body.get("add", [])
        remove_labels = body.get("remove", [])

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Update local labels
        for label in add_labels:
            if label not in ticket.labels:
                ticket.labels.append(label)
        for label in remove_labels:
            if label in ticket.labels:
                ticket.labels.remove(label)
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Update GitHub issue labels
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            repo = os.environ.get("SWE_GITHUB_REPO", "")
            if repo:
                def _update_gh_labels():
                    import subprocess
                    try:
                        if add_labels:
                            labels_str = ",".join(add_labels)
                            subprocess.run(
                                ["gh", "issue", "edit", str(gh_number),
                                 "--repo", repo, "--add-label", labels_str],
                                capture_output=True, timeout=15,
                            )
                        if remove_labels:
                            labels_str = ",".join(remove_labels)
                            subprocess.run(
                                ["gh", "issue", "edit", str(gh_number),
                                 "--repo", repo, "--remove-label", labels_str],
                                capture_output=True, timeout=15,
                            )
                    except Exception as exc:
                        logger.warning("Failed to update GH labels: %s", exc)
                threading.Thread(target=_update_gh_labels, daemon=True).start()

        _broadcast_sse_event("action", {
            "event": "labels_updated",
            "ticket_id": ticket_id,
            "labels": ticket.labels,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "labels": ticket.labels})

    def _handle_pipeline_trigger(self):
        """POST /api/pipeline/trigger — trigger a full pipeline cycle in background."""

        def _run():
            try:
                from scripts.ops.swe_team_runner import run_cycle
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                run_cycle(cfg)
                _broadcast_sse_event("action", {
                    "event": "pipeline_complete",
                })
            except Exception as exc:
                logger.exception("Background pipeline cycle failed")
                _broadcast_sse_event("action", {
                    "event": "pipeline_failed",
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name="pipeline-cycle")
        thread.start()
        self._json_response({"status": "triggered"})

    def _gh_comment_async(self, issue_number, body_text: str):
        """Post a comment to a GitHub issue in a background thread."""
        repo = os.environ.get("SWE_GITHUB_REPO", "")
        if not repo:
            return

        def _post():
            import subprocess
            try:
                subprocess.run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", repo, "--body", body_text],
                    capture_output=True, timeout=15,
                )
            except Exception as exc:
                logger.warning("Failed to comment on GH issue #%s: %s", issue_number, exc)

        threading.Thread(target=_post, daemon=True).start()

    # --- Auth helpers ---

    _SESSION_COOKIE_NAME = "swe_session"

    def _check_auth(self) -> Optional[dict]:
        """Read and validate the session cookie.

        Returns the user dict on success, or ``None`` if not authenticated.
        If OAuth is not enabled, returns a synthetic "anonymous" user so that
        the dashboard remains accessible without credentials.
        """
        if _oauth_provider is None:
            return {"login": "anonymous", "name": "Anonymous", "orgs": []}

        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return None
        morsel = jar.get(self._SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        return _oauth_provider.validate_session(morsel.value)

    def _redirect(self, location: str, status: int = 302) -> None:
        """Send a redirect response."""
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def _set_session_cookie(self, cookie_value: str, clear: bool = False) -> None:
        """Emit a Set-Cookie header for the session cookie.

        When *clear* is True the cookie is expired immediately.
        """
        if clear:
            self.send_header(
                "Set-Cookie",
                f"{self._SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
        else:
            self.send_header(
                "Set-Cookie",
                f"{self._SESSION_COOKIE_NAME}={cookie_value}; Path=/; HttpOnly; SameSite=Lax",
            )

    def _handle_auth_login(self, query: dict) -> None:
        """GET /auth/login — redirect the browser to GitHub's OAuth authorize URL."""
        if _oauth_provider is None:
            self._redirect("/")
            return
        state = secrets.token_urlsafe(24)
        authorize_url = _oauth_provider.get_authorize_url(state)
        # Store state in a short-lived cookie so we can verify it on callback
        self.send_response(302)
        self.send_header("Location", authorize_url)
        self.send_header(
            "Set-Cookie",
            f"swe_oauth_state={state}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=600",
        )
        self.end_headers()

    def _handle_auth_callback(self, query: dict) -> None:
        """GET /auth/callback — exchange code for token, set session cookie."""
        if _oauth_provider is None:
            self._redirect("/")
            return

        code_list = query.get("code", [])
        state_list = query.get("state", [])
        if not code_list:
            self.send_error(400, "Missing code parameter")
            return
        code = code_list[0]

        # Verify state cookie (CSRF protection)
        raw_cookie = self.headers.get("Cookie", "")
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            pass
        state_morsel = jar.get("swe_oauth_state")
        expected_state = state_morsel.value if state_morsel else None
        received_state = state_list[0] if state_list else None
        if expected_state and received_state and not hmac.compare_digest(expected_state, received_state):
            self.send_error(403, "State mismatch — possible CSRF")
            return

        try:
            user_info = _oauth_provider.exchange_code(code)
        except Exception as exc:
            logger.error("OAuth code exchange failed: %s", exc)
            self.send_error(500, f"OAuth error: {exc}")
            return

        if not _oauth_provider.is_authorized(user_info):
            body = (
                "<html><body><h2>Access Denied</h2>"
                f"<p>Your account (<b>{user_info.get('login','')}</b>) is not a member of "
                f"an authorised organisation: {', '.join(_OAUTH_ALLOWED_ORGS)}</p>"
                "<p><a href='/auth/login'>Try again</a></p></body></html>"
            ).encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        cookie_value = _oauth_provider.create_session_cookie(user_info)
        self.send_response(302)
        self.send_header("Location", "/")
        self._set_session_cookie(cookie_value)
        # Clear the state cookie
        self.send_header(
            "Set-Cookie",
            "swe_oauth_state=; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=0",
        )
        self.end_headers()
        logger.info("OAuth login: %s", user_info.get("login", "unknown"))
        # Auto-provision / update user in UserStore on every OAuth login
        try:
            _us = _get_user_store()
            if _us is not None:
                _us.get_or_create_user(
                    github_login=user_info.get("login", ""),
                    email=user_info.get("email", ""),
                    display_name=user_info.get("name", ""),
                    avatar_url=user_info.get("avatar_url", ""),
                )
        except Exception:
            logger.exception("UserStore auto-provision failed (non-fatal)")

    def _handle_auth_logout(self) -> None:
        """GET /auth/logout — clear session cookie and redirect to login."""
        self.send_response(302)
        self.send_header("Location", "/auth/login")
        self._set_session_cookie("", clear=True)
        self.end_headers()

    # --- Page handlers ---

    def _serve_dashboard(self):
        try:
            html = _render_dashboard(self.store)
            self._send_gzipped(html, "text/html; charset=utf-8",
                               cache_control="public, max-age=60")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            logger.exception("Dashboard render error")
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_sse(self):
        """GET /api/stream — Server-Sent Events endpoint for live dashboard updates."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            payload = _build_sse_payload()
            self.wfile.write(f"event: update\ndata: {payload}\n\n".encode())
            self.wfile.flush()
            with _sse_lock:
                _sse_clients.append(self.wfile)
            while True:
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                if self.wfile in _sse_clients:
                    _sse_clients.remove(self.wfile)

    def _handle_api_auth_status(self):
        """GET /api/auth/status — return authentication state for all providers and OAuth session."""
        # OAuth session user (from GitHub OAuth)
        oauth_user = self._check_auth()
        session_info: dict = {}
        if oauth_user and oauth_user.get("login") != "anonymous":
            session_info = {
                "authenticated": True,
                "login": oauth_user.get("login", ""),
                "name": oauth_user.get("name", ""),
                "orgs": oauth_user.get("orgs", []),
            }
        elif oauth_user and oauth_user.get("login") == "anonymous":
            session_info = {"authenticated": False, "login": "anonymous"}
        else:
            session_info = {"authenticated": False}

        # Provider auth states (API key tracking)
        if self.auth_provider is None:
            self._json_response({"providers": [], "session": session_info})
            return
        providers = []
        for state in self.auth_provider.list_states():
            providers.append({
                "name": state.provider_name,
                "is_authenticated": state.is_authenticated,
                "is_healthy": state.is_healthy(),
                "consecutive_failures": state.consecutive_auth_failures,
                "last_error": state.last_auth_error,
            })
        self._json_response({"providers": providers, "session": session_info})

    def _handle_api_graph(self):
        """GET /api/graph — return ticket similarity graph data."""
        try:
            data = _build_graph_data(self.store)
            body = json.dumps(data, indent=2, default=str)
            self._send_gzipped(body, "application/json",
                               cache_control="public, max-age=60")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during graph response — ignored")
        except Exception as exc:
            self._json_response({"error": str(exc)}, status=500)

    def _handle_api_activity(self):
        log_path = PROJECT_ROOT / "logs" / "swe_team.log"
        entries = []
        if log_path.exists():
            lines = _tail_log_file(log_path)[-30:]
            for line in lines:
                if '[INFO]' in line and any(w in line for w in ['Investigating', 'attempt_fix', 'Triaged', 'SESSION', 'Dispatched', 'Claude CLI', 'gate:']):
                    parts = line.split(' ', 3)
                    entries.append({"time": parts[0] + ' ' + parts[1][:8] if len(parts) > 1 else "", "agent": "swe-squad", "action": parts[-1][:120] if parts else line[:120]})
        try:
            body = json.dumps(entries[-20:], indent=2, default=str)
            self._send_gzipped(body, "application/json",
                               cache_control="public, max-age=30")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during activity response — ignored")

    def _handle_costs_roi(self, query: dict):
        """GET /api/costs/roi — subscription ROI calculation."""
        try:
            monthly_fee = float(query.get("monthly_fee", [200.0])[0])
            since_days = int(query.get("since_days", [30])[0])
            tracker = _get_token_tracker()
            roi = tracker.subscription_roi(monthly_fee=monthly_fee, since_days=since_days)
            self._json_response(roi)
        except Exception as exc:
            logger.exception("ROI endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_cache_efficiency(self):
        """GET /api/costs/cache_efficiency — cache read vs creation breakdown."""
        try:
            tracker = _get_token_tracker()
            records = tracker._load_records()
            cache_read_total = sum(r.cache_read_tokens for r in records)
            cache_creation_total = sum(r.cache_creation_tokens for r in records)
            input_total = sum(r.input_tokens for r in records)
            denominator = cache_read_total + input_total
            efficiency_pct = round(cache_read_total / denominator * 100, 2) if denominator else 0.0
            # Estimate savings: cache reads are ~90% cheaper than regular input
            avg_input_rate = 0.003  # USD per 1K tokens (sonnet default)
            estimated_savings = round(cache_read_total / 1000 * avg_input_rate * 0.9, 4)
            self._json_response({
                "cache_read_tokens_total": cache_read_total,
                "cache_creation_tokens_total": cache_creation_total,
                "input_tokens_total": input_total,
                "cache_efficiency_pct": efficiency_pct,
                "estimated_cache_savings_usd": estimated_savings,
            })
        except Exception as exc:
            logger.exception("Cache efficiency endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_costs(self):
        """Redirect legacy /costs page to the SPA Costs tab."""
        self.send_response(302)
        self.send_header("Location", "/#costs")
        self.end_headers()

    def _handle_scheduler(self):
        """Redirect legacy /scheduler page to the SPA Scheduler tab."""
        self.send_response(302)
        self.send_header("Location", "/#scheduler")
        self.end_headers()

    def _serve_json(self):
        try:
            data = _get_cached_dashboard_data(self.store)
            # Inject extended cost data
            try:
                tracker = _get_token_tracker()
                records = tracker._load_records()
                cache_read = sum(r.cache_read_tokens for r in records)
                cache_creation = sum(r.cache_creation_tokens for r in records)
                input_total = sum(r.input_tokens for r in records)
                denom = cache_read + input_total
                data["costs_extended"] = {
                    "cache_read_tokens_total": cache_read,
                    "cache_creation_tokens_total": cache_creation,
                    "cache_efficiency_pct": round(cache_read / denom * 100, 2) if denom else 0.0,
                    "estimated_cache_savings_usd": round(cache_read / 1000 * 0.003 * 0.9, 4),
                }
            except Exception:
                data["costs_extended"] = {}
            # Inject governor status
            try:
                data["governor"] = _get_governor_status()
            except Exception:
                data["governor"] = {"error": "Governor not configured", "configured": False}
            body = json.dumps(data, indent=2, default=str)
            self._send_gzipped(body, "application/json",
                               cache_control="public, max-age=30")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            try:
                self._json_response({"error": str(exc), "status": 500}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json_response(self, data, status: int = 200, cache_control: str | None = None):
        try:
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            accept_enc = self.headers.get("Accept-Encoding", "")
            if "gzip" in accept_enc and len(body) > 1024:
                compressed = gzip.compress(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                self.wfile.write(compressed)
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="SWE-Squad live dashboard server")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    config = load_config()
    store = _load_store(config)

    DashboardHandler.store = store

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    logger.info("Dashboard running at http://%s:%d/", args.host, args.port)
    logger.info("Auto-refresh: every %ds | Data API: /data | Health: /health", _REFRESH_SECONDS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server")


if __name__ == "__main__":
    main()
