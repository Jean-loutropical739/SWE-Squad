"""
Control Plane HTTP API routes for SWE-Squad.

Provides a set of handler functions that can be integrated into the
existing dashboard server or run standalone. All routes return JSON
and accept JSON request bodies where applicable.

Routes:
    POST /api/tickets/urgent            — submit urgent ticket
    GET  /api/config/projects           — list project configs
    PUT  /api/config/projects           — update project configs
    GET  /api/config/projects/<name>    — get single project config
    POST /api/control/pause             — pause pipeline
    POST /api/control/resume            — resume pipeline
    PUT  /api/control/cycle-interval    — set cycle interval
    PUT  /api/control/model-routing     — set model routing
    GET  /api/control/status            — get pipeline status
    GET  /api/queue                     — list queue
    PUT  /api/queue/<id>/priority       — set ticket priority
    POST /api/queue/<id>/promote        — promote ticket to front
    DELETE /api/queue/<id>              — remove ticket from queue
"""
from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional, Tuple

from src.swe_team.control_plane import ControlPlane
from src.swe_team.parallel_executor import ParallelExecutor

logger = logging.getLogger(__name__)

# Global reference to the parallel executor — set by the runner at startup
_executor_ref: Optional[ParallelExecutor] = None


def set_executor_ref(executor: Optional[ParallelExecutor]) -> None:
    """Register the parallel executor for API access."""
    global _executor_ref
    _executor_ref = executor


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Read and parse JSON from the request body."""
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return {}
    body = handler.rfile.read(content_length)
    return json.loads(body.decode("utf-8"))


def _json_response(
    handler: BaseHTTPRequestHandler,
    data: Any,
    status: int = 200,
) -> None:
    """Send a JSON response."""
    try:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        logger.debug("Client disconnected during response")


def _error_response(
    handler: BaseHTTPRequestHandler,
    message: str,
    status: int = 400,
) -> None:
    """Send a JSON error response."""
    _json_response(handler, {"error": message}, status=status)


# ---------------------------------------------------------------------------
# Route matching
# ---------------------------------------------------------------------------

# Pattern: /api/queue/<ticket_id>/priority
_QUEUE_PRIORITY_RE = re.compile(r"^/api/queue/([a-zA-Z0-9_-]+)/priority$")
# Pattern: /api/queue/<ticket_id>/promote
_QUEUE_PROMOTE_RE = re.compile(r"^/api/queue/([a-zA-Z0-9_-]+)/promote$")
# Pattern: /api/queue/<ticket_id>  (for DELETE)
_QUEUE_TICKET_RE = re.compile(r"^/api/queue/([a-zA-Z0-9_-]+)$")
# Pattern: /api/config/projects/<name>
_PROJECT_CONFIG_RE = re.compile(r"^/api/config/projects/(.+)$")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def handle_get(
    handler: BaseHTTPRequestHandler,
    control_plane: ControlPlane,
) -> bool:
    """Handle GET requests for control plane routes. Returns True if handled."""
    path = handler.path

    if path == "/api/control/status":
        _json_response(handler, control_plane.get_status())
        return True

    if path == "/api/config/projects":
        projects = control_plane.get_projects()
        result = {name: cfg.to_dict() for name, cfg in projects.items()}
        _json_response(handler, result)
        return True

    match = _PROJECT_CONFIG_RE.match(path)
    if match:
        name = match.group(1)
        cfg = control_plane.get_project(name)
        if cfg is None:
            _error_response(handler, f"Project not found: {name}", 404)
        else:
            _json_response(handler, {name: cfg.to_dict()})
        return True

    if path == "/api/queue":
        queue = control_plane.get_queue()
        _json_response(handler, [t.to_dict() for t in queue])
        return True

    if path == "/api/execution/status":
        if _executor_ref is None:
            _json_response(handler, {
                "mode": "sequential",
                "message": "Parallel executor not initialized",
            })
        else:
            _json_response(handler, _executor_ref.status())
        return True

    return False


def handle_post(
    handler: BaseHTTPRequestHandler,
    control_plane: ControlPlane,
) -> bool:
    """Handle POST requests for control plane routes. Returns True if handled."""
    path = handler.path

    if path == "/api/tickets/urgent":
        try:
            payload = _read_json_body(handler)
            if not payload.get("title"):
                _error_response(handler, "Missing required field: title")
                return True
            ticket = control_plane.submit_urgent_ticket(payload)
            _json_response(handler, {
                "ticket_id": ticket.ticket_id,
                "status": ticket.status,
                "priority": ticket.priority,
                "message": "Urgent ticket submitted",
            }, status=201)
        except json.JSONDecodeError:
            _error_response(handler, "Invalid JSON body")
        except Exception as exc:
            logger.exception("Error submitting urgent ticket")
            _error_response(handler, str(exc), 500)
        return True

    if path == "/api/control/pause":
        state = control_plane.pause_pipeline()
        _json_response(handler, {
            "status": "paused",
            "pipeline": state.to_dict(),
        })
        return True

    if path == "/api/control/resume":
        state = control_plane.resume_pipeline()
        _json_response(handler, {
            "status": "resumed",
            "pipeline": state.to_dict(),
        })
        return True

    if path == "/api/execution/quota-window":
        if _executor_ref is None:
            _error_response(handler, "Parallel executor not initialized", 503)
            return True
        try:
            payload = _read_json_body(handler)
            multiplier = float(payload.get("multiplier", 1.0))
            profile = payload.get("profile", "burst")
            _executor_ref._config.adaptive.quota_multiplier = multiplier
            _executor_ref.scale_to(profile)
            _json_response(handler, {
                "message": f"Quota window active: {profile} at {multiplier}x",
                "status": _executor_ref.status(),
            })
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error setting quota window")
            _error_response(handler, str(exc), 500)
        return True

    # Promote ticket
    match = _QUEUE_PROMOTE_RE.match(path)
    if match:
        ticket_id = match.group(1)
        ticket = control_plane.promote_ticket(ticket_id)
        if ticket is None:
            _error_response(handler, f"Ticket not found: {ticket_id}", 404)
        else:
            _json_response(handler, ticket.to_dict())
        return True

    return False


def handle_put(
    handler: BaseHTTPRequestHandler,
    control_plane: ControlPlane,
) -> bool:
    """Handle PUT requests for control plane routes. Returns True if handled."""
    path = handler.path

    if path == "/api/config/projects":
        try:
            payload = _read_json_body(handler)
            results = control_plane.update_projects_bulk(payload)
            _json_response(handler, {
                name: cfg.to_dict() for name, cfg in results.items()
            })
        except json.JSONDecodeError:
            _error_response(handler, "Invalid JSON body")
        except Exception as exc:
            logger.exception("Error updating project configs")
            _error_response(handler, str(exc), 500)
        return True

    # Single project update: PUT /api/config/projects/<name>
    match = _PROJECT_CONFIG_RE.match(path)
    if match:
        name = match.group(1)
        try:
            payload = _read_json_body(handler)
            cfg = control_plane.update_project(name, payload)
            _json_response(handler, {name: cfg.to_dict()})
        except json.JSONDecodeError:
            _error_response(handler, "Invalid JSON body")
        except Exception as exc:
            logger.exception("Error updating project config")
            _error_response(handler, str(exc), 500)
        return True

    if path == "/api/control/cycle-interval":
        try:
            payload = _read_json_body(handler)
            minutes = payload.get("cycle_interval_minutes")
            if minutes is None:
                _error_response(handler, "Missing field: cycle_interval_minutes")
                return True
            state = control_plane.set_cycle_interval(int(minutes))
            _json_response(handler, {"pipeline": state.to_dict()})
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error setting cycle interval")
            _error_response(handler, str(exc), 500)
        return True

    if path == "/api/control/model-routing":
        try:
            payload = _read_json_body(handler)
            state = control_plane.set_model_routing(payload)
            _json_response(handler, {"pipeline": state.to_dict()})
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error setting model routing")
            _error_response(handler, str(exc), 500)
        return True

    if path == "/api/execution/mode":
        if _executor_ref is None:
            _error_response(handler, "Parallel executor not initialized", 503)
            return True
        try:
            payload = _read_json_body(handler)
            profile = payload.get("profile")
            if not profile:
                _error_response(handler, "Missing field: profile")
                return True
            _executor_ref.scale_to(profile)
            _json_response(handler, _executor_ref.status())
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error switching execution mode")
            _error_response(handler, str(exc), 500)
        return True

    if path == "/api/execution/profile":
        if _executor_ref is None:
            _error_response(handler, "Parallel executor not initialized", 503)
            return True
        try:
            payload = _read_json_body(handler)
            # Update profile parameters at runtime
            profile_name = payload.get("name", _executor_ref.active_profile_name)
            profile = _executor_ref._config.profiles.get(profile_name)
            if profile is None:
                _error_response(handler, f"Unknown profile: {profile_name}", 404)
                return True
            if "max_concurrent_investigations" in payload:
                profile.max_concurrent_investigations = int(payload["max_concurrent_investigations"])
            if "max_concurrent_developments" in payload:
                profile.max_concurrent_developments = int(payload["max_concurrent_developments"])
            if "cycle_interval_seconds" in payload:
                profile.cycle_interval_seconds = int(payload["cycle_interval_seconds"])
            # Rebuild pools if updating the active profile
            if profile_name == _executor_ref.active_profile_name:
                _executor_ref._rebuild_pools()
            _json_response(handler, {
                "profile": profile_name,
                "config": profile.to_dict(),
            })
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error updating execution profile")
            _error_response(handler, str(exc), 500)
        return True

    # Ticket priority: PUT /api/queue/<id>/priority
    match = _QUEUE_PRIORITY_RE.match(path)
    if match:
        ticket_id = match.group(1)
        try:
            payload = _read_json_body(handler)
            priority = payload.get("priority")
            if priority is None:
                _error_response(handler, "Missing field: priority")
                return True
            ticket = control_plane.update_ticket_priority(ticket_id, int(priority))
            if ticket is None:
                _error_response(handler, f"Ticket not found: {ticket_id}", 404)
            else:
                _json_response(handler, ticket.to_dict())
        except (json.JSONDecodeError, ValueError) as exc:
            _error_response(handler, str(exc))
        except Exception as exc:
            logger.exception("Error updating ticket priority")
            _error_response(handler, str(exc), 500)
        return True

    return False


def handle_delete(
    handler: BaseHTTPRequestHandler,
    control_plane: ControlPlane,
) -> bool:
    """Handle DELETE requests for control plane routes. Returns True if handled."""
    path = handler.path

    match = _QUEUE_TICKET_RE.match(path)
    if match:
        ticket_id = match.group(1)
        removed = control_plane.remove_ticket(ticket_id)
        if removed:
            _json_response(handler, {"message": f"Ticket {ticket_id} removed"})
        else:
            _error_response(handler, f"Ticket not found: {ticket_id}", 404)
        return True

    return False


# ---------------------------------------------------------------------------
# Control Panel HTML page
# ---------------------------------------------------------------------------

_CONTROL_PANEL_NAV = (
    '<nav style="margin-bottom:20px;font-family:monospace">'
    '<a href="/" style="color:#e94560;margin-right:15px;text-decoration:none">Dashboard</a>'
    '<a href="/costs" style="color:#e94560;margin-right:15px;text-decoration:none">Costs</a>'
    '<a href="/scheduler" style="color:#e94560;margin-right:15px;text-decoration:none">Scheduler</a>'
    '<a href="/control" style="color:#e94560;margin-right:15px;text-decoration:none;font-weight:bold">Control Plane</a>'
    '<a href="/data" style="color:#e94560;margin-right:15px;text-decoration:none">API</a>'
    '</nav>'
)


def render_control_panel(control_plane: ControlPlane) -> str:
    """Render the Control Plane WebUI page."""
    status = control_plane.get_status()
    pipeline = status["pipeline"]
    projects = control_plane.get_projects()
    queue = control_plane.get_queue()

    # Pipeline status card
    paused_badge = (
        '<span style="color:#f44336;font-weight:bold">PAUSED</span>'
        if pipeline["paused"]
        else '<span style="color:#4CAF50;font-weight:bold">RUNNING</span>'
    )

    # Project config rows
    project_rows = ""
    for name, cfg in projects.items():
        project_rows += (
            f'<tr><td>{name}</td>'
            f'<td>{cfg.max_concurrent_agents}</td>'
            f'<td>${cfg.budget_cap_daily:.0f}</td>'
            f'<td>${cfg.budget_cap_weekly:.0f}</td>'
            f'<td>{cfg.priority_weight:.1f}</td>'
            f'<td>{cfg.model_tier}</td>'
            f'<td>{cfg.cycle_interval_minutes}m</td>'
            f'<td>{"Yes" if cfg.enabled else "No"}</td></tr>'
        )

    # Queue rows
    queue_rows = ""
    for t in queue[:20]:
        sev_color = {
            "critical": "#f44336", "high": "#FF9800",
            "medium": "#2196F3", "low": "#9E9E9E",
        }.get(t.severity, "#e0e0e0")
        queue_rows += (
            f'<tr><td>{t.ticket_id}</td>'
            f'<td>{t.title[:60]}</td>'
            f'<td style="color:{sev_color}">{t.severity.upper()}</td>'
            f'<td>{t.priority}</td>'
            f'<td>{t.source}</td>'
            f'<td>{t.status}</td>'
            f'<td>{t.created_at[:16]}</td></tr>'
        )

    model_routing = pipeline.get("model_routing", {})

    html = f"""<!DOCTYPE html>
<html><head><title>SWE-Squad Control Plane</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
.cards {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
.card {{ background: #16213e; padding: 20px; border-radius: 8px; min-width: 200px; }}
.card h3 {{ margin: 0; color: #0f3460; font-size: 14px; }}
.card .value {{ font-size: 28px; color: #e94560; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; margin-bottom: 20px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #0f3460; }}
th {{ color: #e94560; }}
h2 {{ color: #e94560; margin-top: 30px; }}
.btn {{ background: #e94560; color: white; border: none; padding: 8px 16px;
        border-radius: 4px; cursor: pointer; font-family: monospace; margin: 4px; }}
.btn:hover {{ background: #c73a50; }}
.btn-secondary {{ background: #0f3460; }}
.btn-secondary:hover {{ background: #1a4a80; }}
code {{ background: #0f3460; padding: 2px 6px; border-radius: 3px; }}
.api-ref {{ background: #16213e; padding: 15px; border-radius: 8px; margin: 10px 0; }}
.api-ref code {{ display: inline-block; margin: 2px 0; }}
</style>
<script>
async function apiCall(method, url, body) {{
    const opts = {{ method, headers: {{ 'Content-Type': 'application/json' }} }};
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    const data = await resp.json();
    document.getElementById('result').textContent = JSON.stringify(data, null, 2);
    if (method !== 'GET') setTimeout(() => location.reload(), 1000);
    return data;
}}
function pausePipeline() {{ apiCall('POST', '/api/control/pause'); }}
function resumePipeline() {{ apiCall('POST', '/api/control/resume'); }}
function submitUrgent() {{
    const title = prompt('Urgent ticket title:');
    if (!title) return;
    const desc = prompt('Description:') || '';
    apiCall('POST', '/api/tickets/urgent', {{
        title, description: desc, severity: 'critical'
    }});
}}
</script>
</head><body>
{_CONTROL_PANEL_NAV}
<h1>Control Plane</h1>

<div class="cards">
  <div class="card"><h3>Pipeline Status</h3><div class="value">{paused_badge}</div></div>
  <div class="card"><h3>Cycle Interval</h3><div class="value">{pipeline['cycle_interval_minutes']}m</div></div>
  <div class="card"><h3>Queue Depth</h3><div class="value">{status['queue_depth']}</div></div>
  <div class="card"><h3>Active Agents</h3><div class="value">{len(status['active_agents'])}</div></div>
  <div class="card"><h3>Processing</h3><div class="value">{status['processing_count']}</div></div>
</div>

<div>
  <button class="btn" onclick="pausePipeline()">Pause Pipeline</button>
  <button class="btn" onclick="resumePipeline()">Resume Pipeline</button>
  <button class="btn btn-secondary" onclick="submitUrgent()">Submit Urgent Ticket</button>
</div>

<h2>Model Routing</h2>
<table>
<tr><th>Tier</th><th>Model</th></tr>
<tr><td>T1 Heavy</td><td>{model_routing.get('t1_heavy', 'opus')}</td></tr>
<tr><td>T2 Standard</td><td>{model_routing.get('t2_standard', 'sonnet')}</td></tr>
<tr><td>T3 Fast</td><td>{model_routing.get('t3_fast', 'haiku')}</td></tr>
</table>

<h2>Project Configuration</h2>
<table>
<tr><th>Project</th><th>Max Agents</th><th>Daily Cap</th><th>Weekly Cap</th>
<th>Weight</th><th>Model</th><th>Cycle</th><th>Enabled</th></tr>
{project_rows}
</table>

<h2>Priority Queue ({len(queue)} tickets)</h2>
<table>
<tr><th>ID</th><th>Title</th><th>Severity</th><th>Priority</th>
<th>Source</th><th>Status</th><th>Created</th></tr>
{queue_rows}
</table>

<h2>API Reference</h2>
<div class="api-ref">
<p><code>POST /api/tickets/urgent</code> — Submit urgent ticket (immediate execution)</p>
<p><code>GET /api/config/projects</code> — List project configurations</p>
<p><code>PUT /api/config/projects</code> — Update project configurations (bulk)</p>
<p><code>POST /api/control/pause</code> — Pause pipeline</p>
<p><code>POST /api/control/resume</code> — Resume pipeline</p>
<p><code>PUT /api/control/cycle-interval</code> — Change cycle interval</p>
<p><code>PUT /api/control/model-routing</code> — Override model routing</p>
<p><code>GET /api/control/status</code> — Pipeline status</p>
<p><code>GET /api/queue</code> — View ticket queue</p>
<p><code>PUT /api/queue/&lt;id&gt;/priority</code> — Change ticket priority</p>
<p><code>POST /api/queue/&lt;id&gt;/promote</code> — Promote ticket to front</p>
<p><code>DELETE /api/queue/&lt;id&gt;</code> — Remove from queue</p>
</div>

<h2>Last API Response</h2>
<pre id="result" style="background:#16213e;padding:15px;border-radius:8px;max-height:300px;overflow:auto;">
(click a button above to see the response)
</pre>

</body></html>"""
    return html
