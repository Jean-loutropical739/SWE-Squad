"""
In-memory TaskQueue implementation.

Thread-safe, priority-ordered queue backed by heapq.  Suitable for
single-process deployments and testing.  Drop-in replacement for any
future Redis/RabbitMQ backend.

Features:
- Thread-safe via threading.Lock
- Priority ordering (lower int = higher priority)
- Auto-retry with exponential back-off (30 s, 60 s, 120 s …)
- Dead-letter queue after max_retries exhausted
- 90-second lease timeout: tasks not heartbeated are reclaimed
"""
from __future__ import annotations

import heapq
import logging
import time
import uuid
from threading import Lock
from typing import Dict, List, Optional

from src.swe_team.providers.task_queue.base import QueuedTask

logger = logging.getLogger(__name__)

# How long a claimed task's lease lasts without a heartbeat (seconds).
_LEASE_TIMEOUT_SECONDS: float = 90.0


class InMemoryTaskQueue:
    """
    Priority-ordered, thread-safe in-memory task queue.

    The heap stores tuples of (priority, created_at, task_id) so that
    equal-priority tasks are broken by insertion order (FIFO).
    """

    def __init__(self) -> None:
        self._lock = Lock()
        # heap entries: (priority, created_at, task_id)
        self._heap: List[tuple] = []
        # All tasks by task_id — single source of truth
        self._tasks: Dict[str, QueuedTask] = {}
        # Dead-letter tasks in insertion order
        self._dead_letter: List[QueuedTask] = []

    # ------------------------------------------------------------------
    # Protocol: name
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "memory"

    # ------------------------------------------------------------------
    # Protocol: enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        task_type: str,
        ticket_id: str,
        payload: dict,
        *,
        priority: int = 50,
    ) -> QueuedTask:
        now = time.time()
        task_id = str(uuid.uuid4())
        task = QueuedTask(
            task_id=task_id,
            ticket_id=ticket_id,
            task_type=task_type,
            priority=priority,
            payload=payload,
            status="queued",
            created_at=now,
        )
        with self._lock:
            self._tasks[task_id] = task
            heapq.heappush(self._heap, (priority, now, task_id))
        logger.debug(
            "Enqueued task %s (type=%s ticket=%s priority=%d)",
            task_id,
            task_type,
            ticket_id,
            priority,
        )
        return task

    # ------------------------------------------------------------------
    # Protocol: claim
    # ------------------------------------------------------------------

    def claim(self, task_type: str, worker_id: str) -> Optional[QueuedTask]:
        """
        Atomically claim the highest-priority task of *task_type*.

        Eligibility rules:
        1. status == "queued" AND task.task_type == task_type
           AND (next_retry_at is None OR next_retry_at <= now)
        2. OR status == "claimed" AND lease expired (no heartbeat > 90 s)
           AND task.task_type == task_type
        """
        now = time.time()
        with self._lock:
            # First: reclaim any stale "claimed" tasks whose lease has expired.
            for task in self._tasks.values():
                if (
                    task.status == "claimed"
                    and task.task_type == task_type
                    and task.claimed_at is not None
                    and (now - task.claimed_at) > _LEASE_TIMEOUT_SECONDS
                ):
                    logger.warning(
                        "Reclaiming stale task %s (claimed by %s %.0f s ago)",
                        task.task_id,
                        task.claimed_by,
                        now - task.claimed_at,
                    )
                    task.status = "queued"
                    task.claimed_at = None
                    task.claimed_by = None
                    heapq.heappush(
                        self._heap, (task.priority, task.created_at, task.task_id)
                    )

            # Work through the heap to find the best eligible task.
            # We may need to skip invalidated / wrong-type entries.
            skipped: List[tuple] = []
            chosen: Optional[QueuedTask] = None

            while self._heap:
                entry = heapq.heappop(self._heap)
                _, _, task_id = entry
                task = self._tasks.get(task_id)

                if task is None:
                    # Task was removed; discard heap entry.
                    continue

                if task.status != "queued":
                    # Already claimed/completed/failed — stale heap entry.
                    continue

                if task.task_type != task_type:
                    skipped.append(entry)
                    continue

                if task.next_retry_at is not None and task.next_retry_at > now:
                    # Not yet eligible for retry.
                    skipped.append(entry)
                    continue

                # Found an eligible task.
                chosen = task
                break

            # Put back anything we skipped.
            for entry in skipped:
                heapq.heappush(self._heap, entry)

            if chosen is None:
                return None

            chosen.status = "claimed"
            chosen.claimed_at = now
            chosen.claimed_by = worker_id
            chosen.attempts += 1
            logger.debug(
                "Worker %s claimed task %s (attempt %d)",
                worker_id,
                chosen.task_id,
                chosen.attempts,
            )
            return chosen

    # ------------------------------------------------------------------
    # Protocol: complete
    # ------------------------------------------------------------------

    def complete(self, task_id: str, result: dict) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")
            if task.status != "claimed":
                raise ValueError(
                    f"Cannot complete task {task_id!r} with status={task.status!r}"
                )
            task.status = "completed"
            task.result = result
        logger.debug("Task %s completed", task_id)

    # ------------------------------------------------------------------
    # Protocol: fail
    # ------------------------------------------------------------------

    def fail(self, task_id: str, error: str) -> None:
        """
        Fail a task.  Auto-retries with exponential back-off; DLQ after
        max_retries is exhausted.

        Back-off schedule: attempt n → delay = retry_delay_seconds * 2^(n-1)
        e.g. default 30 s base → 30, 60, 120 s for attempts 1, 2, 3.
        """
        now = time.time()
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")
            if task.status != "claimed":
                raise ValueError(
                    f"Cannot fail task {task_id!r} with status={task.status!r}"
                )

            task.error = error

            if task.attempts >= task.max_retries:
                # Exhausted retries — move to dead-letter.
                task.status = "dead_letter"
                self._dead_letter.append(task)
                logger.warning(
                    "Task %s sent to dead-letter after %d attempts: %s",
                    task_id,
                    task.attempts,
                    error,
                )
            else:
                # Schedule a retry with exponential back-off.
                delay = task.retry_delay_seconds * (2 ** (task.attempts - 1))
                task.next_retry_at = now + delay
                task.status = "queued"
                task.claimed_at = None
                task.claimed_by = None
                heapq.heappush(
                    self._heap, (task.priority, task.created_at, task.task_id)
                )
                logger.info(
                    "Task %s re-queued for retry in %.0f s (attempt %d/%d)",
                    task_id,
                    delay,
                    task.attempts,
                    task.max_retries,
                )

    # ------------------------------------------------------------------
    # Protocol: heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self, task_id: str) -> None:
        """Renew the claimed_at timestamp to extend the lease."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task {task_id!r} not found")
            if task.status != "claimed":
                raise ValueError(
                    f"Cannot heartbeat task {task_id!r} with status={task.status!r}"
                )
            task.claimed_at = time.time()
        logger.debug("Heartbeat renewed for task %s", task_id)

    # ------------------------------------------------------------------
    # Protocol: get_dead_letter
    # ------------------------------------------------------------------

    def get_dead_letter(self, limit: int = 10) -> List[QueuedTask]:
        with self._lock:
            return list(self._dead_letter[:limit])

    # ------------------------------------------------------------------
    # Protocol: queue_depth
    # ------------------------------------------------------------------

    def queue_depth(self, task_type: Optional[str] = None) -> int:
        with self._lock:
            count = 0
            for task in self._tasks.values():
                if task.status != "queued":
                    continue
                if task_type is not None and task.task_type != task_type:
                    continue
                count += 1
            return count

    # ------------------------------------------------------------------
    # Protocol: health_check
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        # In-memory queue is always healthy as long as the process is alive.
        return True
