"""
TelegramNotificationProvider — wraps src.swe_team.telegram + notifier.

Concrete implementation of NotificationProvider that delegates to the
existing standalone Telegram Bot API client.  All config is received
via constructor — no os.environ reads inside this class.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.swe_team.providers.notification.base import NotificationProvider

logger = logging.getLogger(__name__)


class TelegramNotificationProvider:
    """NotificationProvider backed by the Telegram Bot API.

    Satisfies the ``NotificationProvider`` protocol defined in
    ``src/swe_team/providers/notification/base.py``.
    """

    def __init__(self, *, token: str = "", chat_id: str = "") -> None:
        self._token = token
        self._chat_id = chat_id

    # -- Protocol properties / methods ------------------------------------

    @property
    def name(self) -> str:
        return "telegram"

    def send_alert(self, message: str, *, level: str = "info") -> bool:
        """Send an alert via the Telegram Bot API (or OpenClaw gateway)."""
        from src.swe_team.notifier import _send
        try:
            return _send(message)
        except Exception:  # noqa: BLE001
            logger.warning("TelegramNotificationProvider.send_alert failed", exc_info=True)
            return False

    def send_daily_summary(self, summary: str) -> bool:
        """Send a pre-formatted daily summary string."""
        return self.send_alert(summary, level="info")

    def send_hitl_escalation(self, ticket_id: str, message: str) -> bool:
        """Send an HITL escalation alert."""
        return self.send_alert(message, level="critical")

    def health_check(self) -> bool:
        """Return True if token and chat_id are configured."""
        return bool(self._token and self._chat_id)


# Runtime check (asserts the class satisfies the protocol)
assert isinstance(TelegramNotificationProvider(token="t", chat_id="c"), NotificationProvider)
