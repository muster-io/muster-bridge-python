# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The role gateway: the calls a bridge to a phone or SMS provider implements, and the acknowledgement it reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from muster_bridge.models import NotificationInput, NotificationSent

__all__ = ["AcknowledgedNotification", "Gateway"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AcknowledgedNotification:
    """A notification_acknowledged event the bridge reported, as Gateway.answer is given it."""

    id: str
    """The id of the event, which answerEvent names."""
    notification_id: str
    """The notification_id that Gateway.send_notification answered."""
    data: str
    """The action.data of the notification, unchanged."""
    chat: str | None
    """What the bridge passed with the report, such as a call id; it is never sent to Muster."""
    occurred_at: datetime
    """When it happened, in UTC."""


@runtime_checkable
class Gateway(Protocol):
    """What a bridge in the role gateway implements: the calls to its provider.

    Each method is given the bot whose token Muster sent, by its name in Config.bots. It raises a BridgeError to
    answer its class (Invalid for a contact the provider refuses for good, Forbidden for credentials it refuses); any
    other exception is answered transient with a generic detail. The library validates every request before it calls
    a method and every answer before it sends it.
    """

    async def send_notification(self, bot: str, notification: NotificationInput) -> NotificationSent:
        """Reach the person at notification.contact and read out or send notification.text.

        With notification.action the person can acknowledge, as action.label tells them: the bridge then reports it
        with BridgeApp.acknowledge, which is valid only once this method has returned: the library keeps the action
        when it does. The answer's notification_id names the notification there. Muster's
        Idempotency-Key makes a repeated request answer the first result again, without a call to this method.
        """
        ...

    async def answer(self, bot: str, event: AcknowledgedNotification, text: str) -> None:
        """Show text to the person who acknowledged, during the call when it is a call.

        Muster sends it while the person waits, so a gateway whose person is on the phone reads it out at once.
        """
        ...
