"""
Unified Guardrails Coordinator.

Single entry point for all safety gates: circuit breaker, stability gate,
usage governor, throttle, and RBAC. Eliminates fragmented gate evaluation
by centralizing the decision into one call.

Usage::

    guardrails = GuardrailsCoordinator(config)
    guardrails.set_circuit_breaker(circuit_breaker)
    guardrails.set_stability_gate(ralph_gate)
    guardrails.set_usage_governor(governor)
    guardrails.set_throttle(throttle_policy)

    decision = guardrails.can_proceed(
        task_type="investigate",
        ticket_severity="CRITICAL",
    )
    if not decision.allowed:
        logger.warning("Blocked: %s", decision.reason)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardrailDecision:
    """Result of a unified guardrail evaluation."""

    allowed: bool
    reason: str
    gate: str  # which gate blocked (or "all_clear")
    details: Dict[str, Any] = field(default_factory=dict)
    evaluated_gates: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass
class GuardrailHealth:
    """Health snapshot of all guardrail components."""

    circuit_breaker_paused: bool = False
    circuit_breaker_failure_rate: float = 0.0
    stability_verdict: str = "unknown"
    governor_allow_new_work: bool = True
    governor_max_agents: int = 5
    throttle_multiplier: float = 1.0
    queue_depth: int = 0
    dead_letter_count: int = 0


class GuardrailsCoordinator:
    """Unified coordinator for all safety gates.

    Evaluates gates in strict priority order:
    1. Circuit breaker (hard block if paused — system is unhealthy)
    2. Usage governor (quota/concurrency limits)
    3. Stability gate (bug count thresholds)
    4. Throttle (time/capacity/demand adjustments)

    Each gate is optional — if not set, it's skipped. This allows
    incremental adoption: start with just circuit breaker, add gates
    as they become available.
    """

    def __init__(self) -> None:
        self._circuit_breaker: Any = None
        self._stability_gate: Any = None
        self._usage_governor: Any = None
        self._throttle_policy: Any = None
        self._queued_dispatcher: Any = None

    def set_circuit_breaker(self, cb: Any) -> None:
        self._circuit_breaker = cb

    def set_stability_gate(self, gate: Any) -> None:
        self._stability_gate = gate

    def set_usage_governor(self, gov: Any) -> None:
        self._usage_governor = gov

    def set_throttle(self, policy: Any) -> None:
        self._throttle_policy = policy

    def set_queued_dispatcher(self, dispatcher: Any) -> None:
        self._queued_dispatcher = dispatcher

    def can_proceed(
        self,
        task_type: str = "investigate",
        ticket_severity: str = "MEDIUM",
        current_agents: int = 0,
    ) -> GuardrailDecision:
        """Evaluate all guardrails and return a unified decision.

        Parameters
        ----------
        task_type:
            "investigate", "develop", "triage", "deploy"
        ticket_severity:
            "CRITICAL", "HIGH", "MEDIUM", "LOW"
        current_agents:
            Number of agents currently running.

        Returns
        -------
        GuardrailDecision with allowed=True/False and which gate blocked.
        """
        evaluated = []

        # ── Gate 1: Circuit Breaker ────────────────────────────────
        if self._circuit_breaker is not None:
            evaluated.append("circuit_breaker")
            if self._circuit_breaker.is_paused:
                return GuardrailDecision(
                    allowed=False,
                    reason=f"Circuit breaker paused (failure rate {self._circuit_breaker.failure_rate:.0%})",
                    gate="circuit_breaker",
                    details={
                        "failure_rate": self._circuit_breaker.failure_rate,
                        "paused_until": getattr(self._circuit_breaker, "_paused_until", None),
                    },
                    evaluated_gates=evaluated,
                )

        # ── Gate 2: Usage Governor ─────────────────────────────────
        if self._usage_governor is not None:
            evaluated.append("usage_governor")
            try:
                decision = self._usage_governor.get_concurrency_decision()
                if not decision.allow_new_work:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: new work blocked ({decision.audit_trail})",
                        gate="usage_governor",
                        details={
                            "max_agents": decision.max_agents,
                            "priority_floor": decision.priority_floor,
                            "audit_trail": decision.audit_trail,
                        },
                        evaluated_gates=evaluated,
                    )
                # Check if severity meets priority floor
                severity_priority = {
                    "CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4,
                }
                sev_num = severity_priority.get(ticket_severity, 2)
                if sev_num > decision.priority_floor:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: ticket severity {ticket_severity} below priority floor {decision.priority_floor}",
                        gate="usage_governor",
                        details={"priority_floor": decision.priority_floor},
                        evaluated_gates=evaluated,
                    )
                # Check agent count
                if current_agents >= decision.max_agents:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: {current_agents} agents running (max {decision.max_agents})",
                        gate="usage_governor",
                        details={"current": current_agents, "max": decision.max_agents},
                        evaluated_gates=evaluated,
                    )
            except Exception as exc:
                logger.warning("Usage governor check failed: %s — failing open for now", exc)

        # ── Gate 3: Stability Gate ─────────────────────────────────
        if self._stability_gate is not None and task_type in ("deploy", "creative"):
            evaluated.append("stability_gate")
            try:
                report = self._stability_gate.evaluate()
                if report.verdict == "BLOCK":
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Stability gate BLOCK: {report.reason}",
                        gate="stability_gate",
                        details={
                            "verdict": report.verdict,
                            "reason": report.reason,
                            "open_critical": getattr(report, "open_critical", 0),
                            "open_high": getattr(report, "open_high", 0),
                        },
                        evaluated_gates=evaluated,
                    )
            except Exception as exc:
                logger.warning("Stability gate check failed: %s", exc)

        # ── Gate 4: Throttle ───────────────────────────────────────
        if self._throttle_policy is not None:
            evaluated.append("throttle")
            # Throttle adjusts limits but doesn't hard-block; it's informational
            # The actual enforcement happens via adjusted cycle config

        evaluated.append("all_clear")
        return GuardrailDecision(
            allowed=True,
            reason="All guardrails passed",
            gate="all_clear",
            evaluated_gates=evaluated,
        )

    def health(self) -> GuardrailHealth:
        """Return a health snapshot of all guardrail components."""
        h = GuardrailHealth()

        if self._circuit_breaker is not None:
            h.circuit_breaker_paused = self._circuit_breaker.is_paused
            h.circuit_breaker_failure_rate = self._circuit_breaker.failure_rate

        if self._usage_governor is not None:
            try:
                decision = self._usage_governor.get_concurrency_decision()
                h.governor_allow_new_work = decision.allow_new_work
                h.governor_max_agents = decision.max_agents
            except Exception:
                pass

        if self._stability_gate is not None:
            try:
                report = self._stability_gate.evaluate()
                h.stability_verdict = report.verdict
            except Exception:
                pass

        if self._queued_dispatcher is not None:
            try:
                qh = self._queued_dispatcher.health()
                h.queue_depth = qh.get("investigate_depth", 0) + qh.get("develop_depth", 0)
                h.dead_letter_count = qh.get("dead_letter_count", 0)
            except Exception:
                pass

        return h
