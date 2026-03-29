"""
QueuedDispatcher — bridges TaskQueueProvider with ParallelExecutor.

Instead of the runner directly submitting tickets to ThreadPoolExecutor,
the QueuedDispatcher:
  1. Enqueues tickets into the TaskQueueProvider (priority-ordered)
  2. Claims tasks from the queue and dispatches to ParallelExecutor workers
  3. Reports results back to the queue (complete/fail + dead-letter)
  4. Runs heartbeat threads for long-running tasks

This decouples ticket producers (runner) from consumers (executor workers),
enabling future swap from in-memory queue → Redis/RabbitMQ for cross-VM dispatch.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.swe_team.providers.task_queue.base import QueuedTask, TaskQueueProvider

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Result of a dispatched task."""
    task_id: str
    ticket_id: str
    task_type: str
    success: bool
    duration_s: float = 0.0
    error: Optional[str] = None
    result: Optional[Dict] = None


# Severity → queue priority mapping (lower = higher priority)
SEVERITY_PRIORITY = {
    "CRITICAL": 10,
    "HIGH": 30,
    "MEDIUM": 50,
    "LOW": 70,
    "INFO": 90,
}


class QueuedDispatcher:
    """Queue-backed task dispatcher with priority ordering and dead-letter support.

    Usage::

        dispatcher = QueuedDispatcher(queue, worker_id="swe-squad-1")

        # Producer side (runner)
        dispatcher.enqueue_investigation(ticket)
        dispatcher.enqueue_development(ticket)

        # Consumer side (dispatch loop)
        results = dispatcher.dispatch_batch(
            task_type="investigate",
            worker_fn=investigator.investigate,
            max_tasks=3,
        )

        # Health
        dispatcher.queue_depth("investigate")
        dispatcher.get_dead_letter_tasks()
    """

    def __init__(
        self,
        queue: TaskQueueProvider,
        worker_id: str = "swe-squad-default",
        heartbeat_interval_s: float = 45.0,
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._heartbeat_interval = heartbeat_interval_s
        self._active_heartbeats: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    @property
    def queue(self) -> TaskQueueProvider:
        return self._queue

    def enqueue_investigation(
        self,
        ticket: Any,
        *,
        priority: Optional[int] = None,
    ) -> QueuedTask:
        """Enqueue a ticket for investigation."""
        if priority is None:
            severity = getattr(ticket, "severity", None)
            sev_name = getattr(severity, "name", str(severity)) if severity else "MEDIUM"
            priority = SEVERITY_PRIORITY.get(sev_name, 50)

        ticket_id = getattr(ticket, "ticket_id", "unknown")
        payload = {
            "ticket_id": ticket_id,
            "severity": getattr(ticket, "severity", None),
            "title": getattr(ticket, "title", ""),
            "fingerprint": getattr(ticket, "fingerprint", ""),
        }
        # Serialize severity enum if needed
        if hasattr(payload["severity"], "name"):
            payload["severity"] = payload["severity"].name

        task = self._queue.enqueue(
            task_type="investigate",
            ticket_id=ticket_id,
            payload=payload,
            priority=priority,
        )
        logger.info(
            "Enqueued investigation: ticket=%s priority=%d task_id=%s",
            ticket_id, priority, task.task_id,
        )
        return task

    def enqueue_development(
        self,
        ticket: Any,
        *,
        priority: Optional[int] = None,
        worktree_path: Optional[str] = None,
    ) -> QueuedTask:
        """Enqueue a ticket for development."""
        if priority is None:
            severity = getattr(ticket, "severity", None)
            sev_name = getattr(severity, "name", str(severity)) if severity else "MEDIUM"
            priority = SEVERITY_PRIORITY.get(sev_name, 50)

        ticket_id = getattr(ticket, "ticket_id", "unknown")
        payload = {
            "ticket_id": ticket_id,
            "severity": getattr(ticket, "severity", None),
            "title": getattr(ticket, "title", ""),
            "fingerprint": getattr(ticket, "fingerprint", ""),
            "fix_plan": getattr(ticket, "fix_plan", ""),
        }
        if hasattr(payload["severity"], "name"):
            payload["severity"] = payload["severity"].name
        if worktree_path:
            payload["worktree_path"] = worktree_path

        task = self._queue.enqueue(
            task_type="develop",
            ticket_id=ticket_id,
            payload=payload,
            priority=priority,
        )
        logger.info(
            "Enqueued development: ticket=%s priority=%d task_id=%s",
            ticket_id, priority, task.task_id,
        )
        return task

    def _start_heartbeat(self, task_id: str) -> threading.Event:
        """Start a background thread that heartbeats a claimed task."""
        stop_event = threading.Event()

        def _heartbeat_loop():
            while not stop_event.is_set():
                try:
                    self._queue.heartbeat(task_id)
                except Exception:
                    logger.debug("Heartbeat failed for task %s (may be completed)", task_id)
                    break
                stop_event.wait(self._heartbeat_interval)

        t = threading.Thread(
            target=_heartbeat_loop,
            name=f"heartbeat-{task_id[:8]}",
            daemon=True,
        )
        t.start()

        with self._lock:
            self._active_heartbeats[task_id] = stop_event

        return stop_event

    def _stop_heartbeat(self, task_id: str) -> None:
        """Stop the heartbeat thread for a task."""
        with self._lock:
            stop_event = self._active_heartbeats.pop(task_id, None)
        if stop_event:
            stop_event.set()

    def dispatch_one(
        self,
        task_type: str,
        worker_fn: Callable[[Any], bool],
        ticket_lookup: Callable[[str], Any],
    ) -> Optional[DispatchResult]:
        """Claim one task from the queue and execute it.

        Parameters
        ----------
        task_type:
            Type of task to claim ("investigate" or "develop").
        worker_fn:
            Function that takes a ticket and returns True/False for success.
        ticket_lookup:
            Function that takes a ticket_id and returns the ticket object.

        Returns
        -------
        DispatchResult if a task was claimed and executed, None if queue empty.
        """
        task = self._queue.claim(task_type, self._worker_id)
        if task is None:
            return None

        # Start heartbeat for long-running task
        self._start_heartbeat(task.task_id)
        start = time.monotonic()

        try:
            ticket = ticket_lookup(task.ticket_id)
            if ticket is None:
                self._queue.fail(task.task_id, f"Ticket {task.ticket_id} not found")
                return DispatchResult(
                    task_id=task.task_id,
                    ticket_id=task.ticket_id,
                    task_type=task_type,
                    success=False,
                    error=f"Ticket {task.ticket_id} not found",
                )

            success = worker_fn(ticket)
            duration = time.monotonic() - start

            if success:
                self._queue.complete(task.task_id, {
                    "duration_s": round(duration, 2),
                    "ticket_id": task.ticket_id,
                })
            else:
                self._queue.fail(task.task_id, "Worker returned failure")

            return DispatchResult(
                task_id=task.task_id,
                ticket_id=task.ticket_id,
                task_type=task_type,
                success=success,
                duration_s=round(duration, 2),
            )

        except Exception as exc:
            duration = time.monotonic() - start
            logger.exception("Task %s failed: %s", task.task_id, exc)
            self._queue.fail(task.task_id, str(exc))
            return DispatchResult(
                task_id=task.task_id,
                ticket_id=task.ticket_id,
                task_type=task_type,
                success=False,
                duration_s=round(duration, 2),
                error=str(exc),
            )
        finally:
            self._stop_heartbeat(task.task_id)

    def dispatch_batch(
        self,
        task_type: str,
        worker_fn: Callable[[Any], bool],
        ticket_lookup: Callable[[str], Any],
        max_tasks: int = 5,
    ) -> List[DispatchResult]:
        """Claim and execute up to max_tasks from the queue sequentially.

        For parallel execution, use dispatch_parallel() instead.
        """
        results = []
        for _ in range(max_tasks):
            result = self.dispatch_one(task_type, worker_fn, ticket_lookup)
            if result is None:
                break  # Queue empty
            results.append(result)
        return results

    def dispatch_parallel(
        self,
        task_type: str,
        worker_fn: Callable[[Any], bool],
        ticket_lookup: Callable[[str], Any],
        executor: Any,  # ParallelExecutor or ThreadPoolExecutor
        max_tasks: int = 5,
        timeout_s: float = 1800.0,
    ) -> List[DispatchResult]:
        """Claim tasks from queue and dispatch to a thread pool executor.

        Claims up to max_tasks, submits each to the executor's thread pool,
        then collects results. Each task gets its own heartbeat thread.

        Parameters
        ----------
        task_type:
            "investigate" or "develop"
        worker_fn:
            Function(ticket) → bool
        ticket_lookup:
            Function(ticket_id) → ticket object
        executor:
            Must have a .submit(callable) method returning a Future.
            Can be a ThreadPoolExecutor or ParallelExecutor.
        max_tasks:
            Maximum tasks to claim in this batch.
        timeout_s:
            Per-task timeout in seconds.
        """
        # Claim tasks
        claimed: List[QueuedTask] = []
        for _ in range(max_tasks):
            task = self._queue.claim(task_type, self._worker_id)
            if task is None:
                break
            claimed.append(task)

        if not claimed:
            return []

        logger.info(
            "Dispatching %d %s tasks in parallel (worker=%s)",
            len(claimed), task_type, self._worker_id,
        )

        # Submit to executor with heartbeats
        futures: List[tuple] = []  # (Future, QueuedTask, heartbeat_stop_event)
        for task in claimed:
            self._start_heartbeat(task.task_id)

            def _run(t=task):
                start = time.monotonic()
                try:
                    ticket = ticket_lookup(t.ticket_id)
                    if ticket is None:
                        return DispatchResult(
                            task_id=t.task_id, ticket_id=t.ticket_id,
                            task_type=task_type, success=False,
                            error=f"Ticket {t.ticket_id} not found",
                        )
                    success = worker_fn(ticket)
                    duration = time.monotonic() - start
                    return DispatchResult(
                        task_id=t.task_id, ticket_id=t.ticket_id,
                        task_type=task_type, success=success,
                        duration_s=round(duration, 2),
                    )
                except Exception as exc:
                    duration = time.monotonic() - start
                    return DispatchResult(
                        task_id=t.task_id, ticket_id=t.ticket_id,
                        task_type=task_type, success=False,
                        duration_s=round(duration, 2),
                        error=str(exc),
                    )

            # Use the executor's submit method
            if hasattr(executor, '_investigation_pool') and task_type == "investigate":
                fut = executor._investigation_pool.submit(_run)
            elif hasattr(executor, '_development_pool') and task_type == "develop":
                fut = executor._development_pool.submit(_run)
            else:
                fut = executor.submit(_run)

            futures.append((fut, task))

        # Collect results
        results = []
        for fut, task in futures:
            try:
                result = fut.result(timeout=timeout_s)
                # Report back to queue
                if result.success:
                    self._queue.complete(task.task_id, {
                        "duration_s": result.duration_s,
                        "ticket_id": task.ticket_id,
                    })
                else:
                    self._queue.fail(task.task_id, result.error or "Worker failure")
                results.append(result)
            except Exception as exc:
                logger.exception("Task %s timed out or failed: %s", task.task_id, exc)
                self._queue.fail(task.task_id, f"Executor error: {exc}")
                results.append(DispatchResult(
                    task_id=task.task_id, ticket_id=task.ticket_id,
                    task_type=task_type, success=False,
                    error=str(exc),
                ))
            finally:
                self._stop_heartbeat(task.task_id)

        return results

    def queue_depth(self, task_type: Optional[str] = None) -> int:
        """Return number of queued tasks."""
        return self._queue.queue_depth(task_type)

    def get_dead_letter_tasks(self, limit: int = 10) -> List[QueuedTask]:
        """Return dead-letter tasks for monitoring/alerting."""
        return self._queue.get_dead_letter(limit)

    def health(self) -> Dict[str, Any]:
        """Return health status of the dispatch system."""
        with self._lock:
            active_heartbeats = len(self._active_heartbeats)

        return {
            "queue_healthy": self._queue.health_check(),
            "queue_name": self._queue.name,
            "worker_id": self._worker_id,
            "active_heartbeats": active_heartbeats,
            "investigate_depth": self._queue.queue_depth("investigate"),
            "develop_depth": self._queue.queue_depth("develop"),
            "dead_letter_count": len(self._queue.get_dead_letter(limit=100)),
        }
