"""Tests for QueuedDispatcher — queue-backed task dispatch bridge."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.queued_dispatcher import (
    DispatchResult,
    QueuedDispatcher,
    SEVERITY_PRIORITY,
)
from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue


@dataclass
class FakeTicket:
    ticket_id: str = "T-100"
    severity: Optional[str] = "HIGH"
    title: str = "Test ticket"
    fingerprint: str = "fp-abc"
    fix_plan: str = "Fix the thing"


@pytest.fixture
def queue():
    return InMemoryTaskQueue()


@pytest.fixture
def dispatcher(queue):
    return QueuedDispatcher(queue, worker_id="test-worker")


@pytest.fixture
def ticket():
    return FakeTicket()


# ── Enqueue ────────────────────────────────────────────────────


class TestEnqueue:
    def test_enqueue_investigation(self, dispatcher, ticket):
        task = dispatcher.enqueue_investigation(ticket)
        assert task.task_type == "investigate"
        assert task.ticket_id == "T-100"
        assert task.status == "queued"
        assert task.priority == SEVERITY_PRIORITY["HIGH"]

    def test_enqueue_development(self, dispatcher, ticket):
        task = dispatcher.enqueue_development(ticket)
        assert task.task_type == "develop"
        assert task.ticket_id == "T-100"

    def test_enqueue_with_custom_priority(self, dispatcher, ticket):
        task = dispatcher.enqueue_investigation(ticket, priority=5)
        assert task.priority == 5

    def test_enqueue_critical_gets_highest_priority(self, dispatcher):
        ticket = FakeTicket(severity="CRITICAL")
        task = dispatcher.enqueue_investigation(ticket)
        assert task.priority == 10

    def test_enqueue_low_gets_low_priority(self, dispatcher):
        ticket = FakeTicket(severity="LOW")
        task = dispatcher.enqueue_investigation(ticket)
        assert task.priority == 70

    def test_enqueue_development_with_worktree(self, dispatcher, ticket):
        task = dispatcher.enqueue_development(ticket, worktree_path="/tmp/wt-1")
        assert task.payload["worktree_path"] == "/tmp/wt-1"

    def test_enqueue_increments_depth(self, dispatcher, ticket):
        assert dispatcher.queue_depth("investigate") == 0
        dispatcher.enqueue_investigation(ticket)
        assert dispatcher.queue_depth("investigate") == 1
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-200"))
        assert dispatcher.queue_depth("investigate") == 2

    def test_enqueue_with_enum_severity(self, dispatcher):
        """Severity as an enum-like object with .name attribute."""
        class SevEnum:
            name = "CRITICAL"
        ticket = FakeTicket()
        ticket.severity = SevEnum()
        task = dispatcher.enqueue_investigation(ticket)
        assert task.priority == 10


# ── Dispatch One ───────────────────────────────────────────────


class TestDispatchOne:
    def test_dispatch_empty_queue(self, dispatcher):
        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result is None

    def test_dispatch_success(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result is not None
        assert result.success is True
        assert result.ticket_id == "T-100"
        assert result.task_type == "investigate"
        assert result.duration_s >= 0

    def test_dispatch_failure(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: False,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result.success is False

    def test_dispatch_exception(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)

        def failing_worker(t):
            raise RuntimeError("boom")

        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=failing_worker,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result.success is False
        assert "boom" in result.error

    def test_dispatch_ticket_not_found(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: None,
        )
        assert result.success is False
        assert "not found" in result.error

    def test_dispatch_respects_task_type(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        # Try to dispatch development — should get nothing
        result = dispatcher.dispatch_one(
            "develop",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result is None
        # Investigation should still be there
        assert dispatcher.queue_depth("investigate") == 1


# ── Dispatch Batch ─────────────────────────────────────────────


class TestDispatchBatch:
    def test_batch_empty(self, dispatcher):
        results = dispatcher.dispatch_batch(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert results == []

    def test_batch_processes_all(self, dispatcher):
        for i in range(3):
            dispatcher.enqueue_investigation(FakeTicket(ticket_id=f"T-{i}"))

        results = dispatcher.dispatch_batch(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
            max_tasks=5,
        )
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_batch_respects_max(self, dispatcher):
        for i in range(5):
            dispatcher.enqueue_investigation(FakeTicket(ticket_id=f"T-{i}"))

        results = dispatcher.dispatch_batch(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
            max_tasks=2,
        )
        assert len(results) == 2
        # 3 remaining in queue
        assert dispatcher.queue_depth("investigate") == 3

    def test_batch_mixed_success_failure(self, dispatcher):
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-ok"))
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-fail"))

        call_count = [0]
        def worker(t):
            call_count[0] += 1
            return call_count[0] == 1  # First succeeds, second fails

        results = dispatcher.dispatch_batch(
            "investigate",
            worker_fn=worker,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False


# ── Dispatch Parallel ──────────────────────────────────────────


class TestDispatchParallel:
    def test_parallel_empty(self, dispatcher):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dispatcher.dispatch_parallel(
                "investigate",
                worker_fn=lambda t: True,
                ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
                executor=pool,
            )
        assert results == []

    def test_parallel_success(self, dispatcher):
        for i in range(3):
            dispatcher.enqueue_investigation(FakeTicket(ticket_id=f"T-{i}"))

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = dispatcher.dispatch_parallel(
                "investigate",
                worker_fn=lambda t: True,
                ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
                executor=pool,
                max_tasks=3,
            )
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_parallel_max_tasks(self, dispatcher):
        for i in range(5):
            dispatcher.enqueue_investigation(FakeTicket(ticket_id=f"T-{i}"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dispatcher.dispatch_parallel(
                "investigate",
                worker_fn=lambda t: True,
                ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
                executor=pool,
                max_tasks=2,
            )
        assert len(results) == 2
        assert dispatcher.queue_depth("investigate") == 3

    def test_parallel_with_failure(self, dispatcher):
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-ok"))
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-fail"))

        call_idx = [0]
        lock = threading.Lock()
        def worker(t):
            with lock:
                idx = call_idx[0]
                call_idx[0] += 1
            return idx == 0

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dispatcher.dispatch_parallel(
                "investigate",
                worker_fn=worker,
                ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
                executor=pool,
            )
        assert len(results) == 2
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1

    def test_parallel_exception_in_worker(self, dispatcher):
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-boom"))

        def worker(t):
            raise ValueError("kaboom")

        with ThreadPoolExecutor(max_workers=1) as pool:
            results = dispatcher.dispatch_parallel(
                "investigate",
                worker_fn=worker,
                ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
                executor=pool,
            )
        assert len(results) == 1
        assert results[0].success is False
        assert "kaboom" in results[0].error


# ── Priority Ordering ──────────────────────────────────────────


class TestPriorityOrdering:
    def test_critical_dispatched_before_low(self, dispatcher):
        # Enqueue low first, then critical
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-low", severity="LOW"))
        dispatcher.enqueue_investigation(FakeTicket(ticket_id="T-crit", severity="CRITICAL"))

        dispatched_order = []
        def worker(t):
            dispatched_order.append(t.ticket_id)
            return True

        dispatcher.dispatch_batch(
            "investigate",
            worker_fn=worker,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
            max_tasks=2,
        )
        # Critical should be dispatched first (lower priority number)
        assert dispatched_order == ["T-crit", "T-low"]

    def test_same_priority_fifo(self, dispatcher):
        for i in range(3):
            dispatcher.enqueue_investigation(
                FakeTicket(ticket_id=f"T-{i}", severity="HIGH")
            )

        dispatched_order = []
        def worker(t):
            dispatched_order.append(t.ticket_id)
            return True

        dispatcher.dispatch_batch(
            "investigate",
            worker_fn=worker,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
            max_tasks=3,
        )
        assert dispatched_order == ["T-0", "T-1", "T-2"]


# ── Dead Letter ────────────────────────────────────────────────


class TestDeadLetter:
    def test_repeated_failures_go_to_dead_letter(self, dispatcher):
        queue = dispatcher.queue
        # Enqueue with max_retries=0 so it goes to dead letter immediately
        task = queue.enqueue(
            task_type="investigate",
            ticket_id="T-doomed",
            payload={},
            priority=50,
        )
        # Manually set max_retries to 0
        task.max_retries = 0

        def failing_worker(t):
            raise RuntimeError("always fails")

        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=failing_worker,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result.success is False
        # After max_retries exhausted, task should be in dead letter
        dead = dispatcher.get_dead_letter_tasks()
        assert len(dead) >= 1
        assert any(t.ticket_id == "T-doomed" for t in dead)


# ── Heartbeat ──────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_starts_and_stops(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)

        called = threading.Event()
        orig_heartbeat = dispatcher._queue.heartbeat
        def track_heartbeat(task_id):
            called.set()
            return orig_heartbeat(task_id)

        dispatcher._queue.heartbeat = track_heartbeat
        dispatcher._heartbeat_interval = 0.05  # 50ms for fast test

        result = dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: (time.sleep(0.15), True)[-1],
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        assert result.success is True
        assert called.is_set()  # Heartbeat was called at least once
        # After dispatch completes, heartbeat should be stopped
        assert len(dispatcher._active_heartbeats) == 0


# ── Health ─────────────────────────────────────────────────────


class TestHealth:
    def test_health_check(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        dispatcher.enqueue_development(FakeTicket(ticket_id="T-dev"))

        health = dispatcher.health()
        assert health["queue_healthy"] is True
        assert health["queue_name"] == "memory"
        assert health["worker_id"] == "test-worker"
        assert health["investigate_depth"] == 1
        assert health["develop_depth"] == 1
        assert health["dead_letter_count"] == 0

    def test_health_after_dispatch(self, dispatcher, ticket):
        dispatcher.enqueue_investigation(ticket)
        dispatcher.dispatch_one(
            "investigate",
            worker_fn=lambda t: True,
            ticket_lookup=lambda tid: FakeTicket(ticket_id=tid),
        )
        health = dispatcher.health()
        assert health["investigate_depth"] == 0


# ── Severity Priority Map ─────────────────────────────────────


class TestSeverityPriority:
    def test_all_severities_mapped(self):
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
            assert sev in SEVERITY_PRIORITY

    def test_critical_highest(self):
        assert SEVERITY_PRIORITY["CRITICAL"] < SEVERITY_PRIORITY["HIGH"]
        assert SEVERITY_PRIORITY["HIGH"] < SEVERITY_PRIORITY["MEDIUM"]
        assert SEVERITY_PRIORITY["MEDIUM"] < SEVERITY_PRIORITY["LOW"]
        assert SEVERITY_PRIORITY["LOW"] < SEVERITY_PRIORITY["INFO"]


# ── DispatchResult ─────────────────────────────────────────────


class TestDispatchResult:
    def test_fields(self):
        r = DispatchResult(
            task_id="t1", ticket_id="T-1", task_type="investigate",
            success=True, duration_s=1.5,
        )
        assert r.task_id == "t1"
        assert r.error is None

    def test_with_error(self):
        r = DispatchResult(
            task_id="t1", ticket_id="T-1", task_type="develop",
            success=False, error="oops",
        )
        assert r.success is False
        assert r.error == "oops"
