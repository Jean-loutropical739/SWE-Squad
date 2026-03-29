"""
Control Plane for SWE-Squad — dynamic pipeline orchestration.

Provides runtime control over:
- Urgent ticket injection (bypass cycle queue)
- Per-project configuration (hot-reloadable from YAML)
- Pipeline pause/resume, cycle interval, model routing overrides
- Priority queue management (view, reorder, promote, remove)

All state is persisted to ``config/control_plane.yaml`` and
``data/swe_team/queue.json`` so changes survive restarts.

Usage::

    from src.swe_team.control_plane import ControlPlane

    cp = ControlPlane()
    cp.submit_urgent_ticket({...})
    cp.update_project_config("owner/repo", {"priority_weight": 0.9})
    cp.pause_pipeline()
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "config/control_plane.yaml"
_DEFAULT_QUEUE_PATH = "data/swe_team/queue.json"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ProjectConfig:
    """Per-project runtime configuration."""

    max_concurrent_agents: int = 2
    budget_cap_daily: float = 50.0
    budget_cap_weekly: float = 200.0
    priority_weight: float = 0.5
    model_tier: str = "T2"
    cycle_interval_minutes: int = 15
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectConfig":
        return cls(
            max_concurrent_agents=data.get("max_concurrent_agents", 2),
            budget_cap_daily=data.get("budget_cap_daily", 50.0),
            budget_cap_weekly=data.get("budget_cap_weekly", 200.0),
            priority_weight=data.get("priority_weight", 0.5),
            model_tier=data.get("model_tier", "T2"),
            cycle_interval_minutes=data.get("cycle_interval_minutes", 15),
            enabled=data.get("enabled", True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineState:
    """Global pipeline runtime state."""

    paused: bool = False
    cycle_interval_minutes: int = 15
    model_routing: Dict[str, str] = field(default_factory=lambda: {
        "t1_heavy": "opus",
        "t2_standard": "sonnet",
        "t3_fast": "haiku",
    })

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineState":
        return cls(
            paused=data.get("paused", False),
            cycle_interval_minutes=data.get("cycle_interval_minutes", 15),
            model_routing=data.get("model_routing", {
                "t1_heavy": "opus",
                "t2_standard": "sonnet",
                "t3_fast": "haiku",
            }),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paused": self.paused,
            "cycle_interval_minutes": self.cycle_interval_minutes,
            "model_routing": dict(self.model_routing),
        }


@dataclass
class QueuedTicket:
    """A ticket in the priority queue awaiting processing."""

    ticket_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    description: str = ""
    severity: str = "medium"
    priority: int = 50  # 0 = highest, 100 = lowest
    project: str = ""
    source: str = "api"  # "api", "monitor", "urgent"
    status: str = "queued"  # "queued", "processing", "completed", "failed"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "priority": self.priority,
            "project": self.project,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueuedTicket":
        return cls(
            ticket_id=data.get("ticket_id", uuid.uuid4().hex[:12]),
            title=data.get("title", ""),
            description=data.get("description", ""),
            severity=data.get("severity", "medium"),
            priority=data.get("priority", 50),
            project=data.get("project", ""),
            source=data.get("source", "api"),
            status=data.get("status", "queued"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Queue persistence
# ---------------------------------------------------------------------------

class QueueStore:
    """JSON file-backed priority queue persistence."""

    def __init__(self, path: Path) -> None:
        if isinstance(path, str):
            path = Path(path)
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> List[QueuedTicket]:
        with self._lock:
            if not self._path.exists():
                return []
            try:
                data = json.loads(self._path.read_text())
                return [QueuedTicket.from_dict(t) for t in data]
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt queue store at %s — returning empty", self._path)
                return []

    def save_all(self, tickets: List[QueuedTicket]) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                [t.to_dict() for t in tickets], indent=2, default=str
            ))
            tmp.rename(self._path)

    def upsert(self, ticket: QueuedTicket) -> None:
        tickets = self.load_all()
        for i, t in enumerate(tickets):
            if t.ticket_id == ticket.ticket_id:
                tickets[i] = ticket
                self.save_all(tickets)
                return
        tickets.append(ticket)
        self.save_all(tickets)

    def remove(self, ticket_id: str) -> bool:
        tickets = self.load_all()
        before = len(tickets)
        tickets = [t for t in tickets if t.ticket_id != ticket_id]
        if len(tickets) < before:
            self.save_all(tickets)
            return True
        return False


# ---------------------------------------------------------------------------
# Config persistence (hot-reload from YAML)
# ---------------------------------------------------------------------------

class ConfigStore:
    """YAML-backed configuration with hot-reload support."""

    def __init__(self, path: Path) -> None:
        if isinstance(path, str):
            path = Path(path)
        self._path = path
        self._lock = threading.Lock()
        self._last_mtime: float = 0.0
        self._cached_pipeline: Optional[PipelineState] = None
        self._cached_projects: Optional[Dict[str, ProjectConfig]] = None

    def _needs_reload(self) -> bool:
        if not self._path.exists():
            return self._cached_pipeline is None
        try:
            mtime = self._path.stat().st_mtime
            return mtime > self._last_mtime
        except OSError:
            return True

    def _load_raw(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path) as fh:
                return yaml.safe_load(fh) or {}
        except Exception:
            logger.exception("Failed to load control plane config from %s", self._path)
            return {}

    def reload(self) -> None:
        """Force reload from disk."""
        with self._lock:
            raw = self._load_raw()
            self._cached_pipeline = PipelineState.from_dict(raw.get("pipeline", {}))
            projects_raw = raw.get("projects", {})
            self._cached_projects = {
                name: ProjectConfig.from_dict(cfg)
                for name, cfg in projects_raw.items()
            }
            if self._path.exists():
                self._last_mtime = self._path.stat().st_mtime
            logger.info(
                "Control plane config reloaded: %d projects, paused=%s",
                len(self._cached_projects), self._cached_pipeline.paused,
            )

    def _ensure_loaded(self) -> None:
        if self._needs_reload():
            self.reload()

    def get_pipeline_state(self) -> PipelineState:
        self._ensure_loaded()
        return self._cached_pipeline or PipelineState()

    def get_projects(self) -> Dict[str, ProjectConfig]:
        self._ensure_loaded()
        return dict(self._cached_projects or {})

    def get_project(self, name: str) -> Optional[ProjectConfig]:
        self._ensure_loaded()
        if self._cached_projects:
            return self._cached_projects.get(name)
        return None

    def save(self) -> None:
        """Persist current state to YAML."""
        with self._lock:
            pipeline = self._cached_pipeline or PipelineState()
            projects = self._cached_projects or {}
            data = {
                "pipeline": pipeline.to_dict(),
                "projects": {
                    name: cfg.to_dict() for name, cfg in projects.items()
                },
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                yaml.dump(data, fh, default_flow_style=False, sort_keys=False)
            tmp.rename(self._path)
            self._last_mtime = self._path.stat().st_mtime
            logger.info("Control plane config saved to %s", self._path)

    def update_pipeline(self, updates: Dict[str, Any]) -> PipelineState:
        """Apply partial updates to pipeline state and persist."""
        self._ensure_loaded()
        state = self._cached_pipeline or PipelineState()
        if "paused" in updates:
            state.paused = bool(updates["paused"])
        if "cycle_interval_minutes" in updates:
            state.cycle_interval_minutes = int(updates["cycle_interval_minutes"])
        if "model_routing" in updates:
            state.model_routing.update(updates["model_routing"])
        self._cached_pipeline = state
        self.save()
        return state

    def update_project(self, name: str, updates: Dict[str, Any]) -> ProjectConfig:
        """Apply partial updates to a project config and persist."""
        self._ensure_loaded()
        if self._cached_projects is None:
            self._cached_projects = {}
        existing = self._cached_projects.get(name, ProjectConfig())
        for key, value in updates.items():
            if hasattr(existing, key):
                setattr(existing, key, type(getattr(existing, key))(value))
        self._cached_projects[name] = existing
        self.save()
        return existing


# ---------------------------------------------------------------------------
# Control Plane — main orchestrator
# ---------------------------------------------------------------------------

class ControlPlane:
    """Central control plane for the SWE-Squad pipeline.

    Manages:
    - Urgent ticket submission (immediate execution bypass)
    - Per-project configuration (hot-reloadable)
    - Pipeline pause/resume and runtime overrides
    - Priority queue management
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        queue_path: Optional[str] = None,
        executor: Optional[Callable] = None,
    ) -> None:
        self._config_store = ConfigStore(
            Path(config_path or os.environ.get(
                "CONTROL_PLANE_CONFIG", _DEFAULT_CONFIG_PATH
            ))
        )
        self._queue_store = QueueStore(
            Path(queue_path or os.environ.get(
                "CONTROL_PLANE_QUEUE", _DEFAULT_QUEUE_PATH
            ))
        )
        self._executor = executor  # Callable[[QueuedTicket], None]
        self._active_agents: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- Config accessors --

    @property
    def pipeline_state(self) -> PipelineState:
        return self._config_store.get_pipeline_state()

    @property
    def is_paused(self) -> bool:
        return self._config_store.get_pipeline_state().paused

    def get_projects(self) -> Dict[str, ProjectConfig]:
        return self._config_store.get_projects()

    def get_project(self, name: str) -> Optional[ProjectConfig]:
        return self._config_store.get_project(name)

    def update_project(self, name: str, updates: Dict[str, Any]) -> ProjectConfig:
        return self._config_store.update_project(name, updates)

    def update_projects_bulk(self, updates: Dict[str, Dict[str, Any]]) -> Dict[str, ProjectConfig]:
        results = {}
        for name, project_updates in updates.items():
            results[name] = self._config_store.update_project(name, project_updates)
        return results

    # -- Pipeline controls --

    def pause_pipeline(self) -> PipelineState:
        logger.warning("Pipeline PAUSED by control plane")
        return self._config_store.update_pipeline({"paused": True})

    def resume_pipeline(self) -> PipelineState:
        logger.info("Pipeline RESUMED by control plane")
        return self._config_store.update_pipeline({"paused": False})

    def set_cycle_interval(self, minutes: int) -> PipelineState:
        if minutes < 1:
            raise ValueError("cycle_interval_minutes must be >= 1")
        logger.info("Cycle interval changed to %d minutes", minutes)
        return self._config_store.update_pipeline({"cycle_interval_minutes": minutes})

    def set_model_routing(self, routing: Dict[str, str]) -> PipelineState:
        valid_tiers = {"t1_heavy", "t2_standard", "t3_fast"}
        for key in routing:
            if key not in valid_tiers:
                raise ValueError(f"Invalid model tier key: {key}. Valid: {valid_tiers}")
        logger.info("Model routing updated: %s", routing)
        return self._config_store.update_pipeline({"model_routing": routing})

    def get_status(self) -> Dict[str, Any]:
        """Return current pipeline status snapshot."""
        pipeline = self._config_store.get_pipeline_state()
        queue = self._queue_store.load_all()
        queued = [t for t in queue if t.status == "queued"]
        processing = [t for t in queue if t.status == "processing"]
        return {
            "pipeline": pipeline.to_dict(),
            "active_agents": dict(self._active_agents),
            "queue_depth": len(queued),
            "processing_count": len(processing),
            "total_queued": len(queue),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # -- Urgent ticket submission --

    def submit_urgent_ticket(self, payload: Dict[str, Any]) -> QueuedTicket:
        """Submit an urgent ticket that bypasses the normal cycle queue.

        The ticket is added to the queue with priority=0 (highest) and
        source='urgent'. If an executor is configured, it is dispatched
        immediately in a background thread.
        """
        ticket = QueuedTicket(
            title=payload.get("title", "Urgent ticket"),
            description=payload.get("description", ""),
            severity=payload.get("severity", "critical"),
            priority=0,  # highest priority
            project=payload.get("project", ""),
            source="urgent",
            metadata=payload.get("metadata", {}),
        )
        self._queue_store.upsert(ticket)
        logger.warning(
            "URGENT ticket submitted: %s [%s] — %s",
            ticket.ticket_id, ticket.severity, ticket.title,
        )

        # Dispatch immediately if executor is available
        if self._executor and not self.is_paused:
            ticket.status = "processing"
            self._queue_store.upsert(ticket)
            thread = threading.Thread(
                target=self._execute_ticket,
                args=(ticket,),
                daemon=True,
                name=f"urgent-{ticket.ticket_id}",
            )
            thread.start()

        return ticket

    def _execute_ticket(self, ticket: QueuedTicket) -> None:
        """Execute a ticket via the configured executor."""
        with self._lock:
            self._active_agents[ticket.ticket_id] = {
                "ticket_id": ticket.ticket_id,
                "title": ticket.title,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
            }
        try:
            self._executor(ticket)
            ticket.status = "completed"
            logger.info("Ticket %s completed successfully", ticket.ticket_id)
        except Exception as exc:
            ticket.status = "failed"
            ticket.metadata["error"] = str(exc)[:500]
            logger.exception("Ticket %s execution failed", ticket.ticket_id)
        finally:
            self._queue_store.upsert(ticket)
            with self._lock:
                self._active_agents.pop(ticket.ticket_id, None)

    # -- Queue management --

    def get_queue(self) -> List[QueuedTicket]:
        """Return all queued tickets sorted by priority (lowest number first)."""
        tickets = self._queue_store.load_all()
        return sorted(tickets, key=lambda t: (t.priority, t.created_at))

    def get_ticket(self, ticket_id: str) -> Optional[QueuedTicket]:
        for t in self._queue_store.load_all():
            if t.ticket_id == ticket_id:
                return t
        return None

    def update_ticket_priority(self, ticket_id: str, priority: int) -> Optional[QueuedTicket]:
        """Change a queued ticket's priority (0=highest, 100=lowest)."""
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            return None
        ticket.priority = max(0, min(100, priority))
        self._queue_store.upsert(ticket)
        logger.info("Ticket %s priority changed to %d", ticket_id, ticket.priority)
        return ticket

    def promote_ticket(self, ticket_id: str) -> Optional[QueuedTicket]:
        """Move a ticket to the front of the queue (priority=0)."""
        return self.update_ticket_priority(ticket_id, 0)

    def remove_ticket(self, ticket_id: str) -> bool:
        """Remove a ticket from the queue."""
        removed = self._queue_store.remove(ticket_id)
        if removed:
            logger.info("Ticket %s removed from queue", ticket_id)
        return removed

    def add_ticket(self, payload: Dict[str, Any]) -> QueuedTicket:
        """Add a regular (non-urgent) ticket to the queue."""
        severity_to_priority = {
            "critical": 10,
            "high": 30,
            "medium": 50,
            "low": 70,
        }
        severity = payload.get("severity", "medium")
        ticket = QueuedTicket(
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            severity=severity,
            priority=payload.get("priority", severity_to_priority.get(severity, 50)),
            project=payload.get("project", ""),
            source=payload.get("source", "api"),
            metadata=payload.get("metadata", {}),
        )
        self._queue_store.upsert(ticket)
        logger.info("Ticket %s added to queue (priority=%d)", ticket.ticket_id, ticket.priority)
        return ticket

    # -- Hot-reload --

    def reload_config(self) -> None:
        """Force reload of control plane configuration from disk."""
        self._config_store.reload()
