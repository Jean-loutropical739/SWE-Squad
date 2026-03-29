"""
NotificationProvider interface — pluggable alerting backend.

Implement this to swap Telegram for Slack, PagerDuty, email, webhooks, etc.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class NotificationProvider(Protocol):

    @property
    def name(self) -> str: ...

    def send_alert(self, message: str, *, level: str = "info") -> bool:
        """Send an alert message. Returns True on success."""
        ...

    def send_daily_summary(self, summary: str) -> bool:
        """Send the daily summary report."""
        ...

    def send_hitl_escalation(self, ticket_id: str, message: str) -> bool:
        """Escalate a ticket to a human. Returns True on success."""
        ...

    def health_check(self) -> bool: ...
