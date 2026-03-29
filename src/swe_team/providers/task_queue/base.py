"""
TaskQueue interface — pluggable task dispatch backend.

Implement this to swap ThreadPoolExecutor-based dispatch for any
message queue backend (Redis, RabbitMQ, SQS, etc.) without changing
any core orchestrator or runner logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class QueuedTask:
    """A single unit of work placed on the task queue."""

    task_id: str
    ticket_id: str
    task_type: str           # "investigate", "develop", "triage", "review"
    priority: int            # 0 = highest priority
    payload: Dict            # arbitrary JSON-serialisable data for the worker
    status: str              # "queued" | "claimed" | "completed" | "failed" | "dead_letter"
    created_at: float        # unix timestamp
    claimed_at: Optional[float] = None
    claimed_by: Optional[str] = None
    attempts: int = 0
    max_retries: int = 3
    retry_delay_seconds: float = 30.0
    next_retry_at: Optional[float] = None
    result: Optional[Dict] = None
    error: Optional[str] = None


@runtime_checkable
class TaskQueueProvider(Protocol):
    """
    Interface all task queue backends must implement.

    Registered in swe_team.yaml under providers.task_queue.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'memory', 'redis', 'rabbitmq')."""
        ...

    def enqueue(
        self,
        task_type: str,
        ticket_id: str,
        payload: dict,
        *,
        priority: int = 50,
    ) -> QueuedTask:
        """
        Place a new task on the queue.

        Args:
            task_type: Category of work (investigate/develop/triage/review).
            ticket_id: The ticket this task is associated with.
            payload:   Arbitrary data the worker needs to execute the task.
            priority:  Lower number = higher priority (default 50).

        Returns:
            The newly-created QueuedTask with status="queued".
        """
        ...

    def claim(self, task_type: str, worker_id: str) -> Optional[QueuedTask]:
        """
        Atomically claim the highest-priority queued task of the given type.

        Only tasks whose status is "queued" and whose next_retry_at (if set)
        is <= now() are eligible.  Stale "claimed" tasks whose lease has
        expired (no heartbeat for > 90 s) are also eligible for reclaim.

        Args:
            task_type: Filter to this task type only.
            worker_id: Identifier of the worker claiming the task.

        Returns:
            The claimed QueuedTask, or None if nothing is available.
        """
        ...

    def complete(self, task_id: str, result: dict) -> None:
        """
        Mark a task as successfully completed.

        Args:
            task_id: ID of the task being completed.
            result:  Structured result data from the worker.
        """
        ...

    def fail(self, task_id: str, error: str) -> None:
        """
        Mark a task as failed.

        If attempts < max_retries the task is re-queued with exponential
        back-off (30 s, 60 s, 120 s …).  Once max_retries is exhausted the
        task moves to the dead-letter queue (status="dead_letter").

        Args:
            task_id: ID of the task that failed.
            error:   Human-readable error message / traceback.
        """
        ...

    def heartbeat(self, task_id: str) -> None:
        """
        Renew the lease on a claimed task to prevent it being reclaimed.

        Workers processing long-running tasks must call this at least once
        every 60 s.

        Args:
            task_id: ID of the task being worked on.
        """
        ...

    def get_dead_letter(self, limit: int = 10) -> List[QueuedTask]:
        """
        Return up to *limit* tasks from the dead-letter queue (oldest first).

        Dead-letter tasks are not removed by this call.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of QueuedTask instances with status="dead_letter".
        """
        ...

    def queue_depth(self, task_type: Optional[str] = None) -> int:
        """
        Return the number of queued (not yet claimed) tasks.

        Args:
            task_type: If provided, count only tasks of this type.
                       If None, count all task types.

        Returns:
            Integer count of tasks with status="queued".
        """
        ...

    def health_check(self) -> bool:
        """Return True if the queue backend is reachable and operational."""
        ...
