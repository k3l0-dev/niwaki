"""Typed object-subscription events — the query layer's view of a push.

:mod:`niwaki.transport._subscription_socket` speaks in raw wire shapes
(:class:`~niwaki.transport._subscription_socket.RawSubscriptionEvent` /
:class:`~niwaki.transport._subscription_socket.SubscriptionGap` /
:class:`~niwaki.transport._subscription_socket.SubscriptionRefreshFailed`).
This module turns one of those into a single typed
:class:`SubscriptionEvent`, deserialising a real push through
:meth:`~niwaki.models.ManagedObject.from_event` exactly like a normal
read goes through ``from_apic`` — reading is uniform whether the object came
from a GET or a push.

Two of the five :class:`EventKind` values — ``GAP`` and ``REFRESH_FAILED`` —
are not failures of the object being watched; they are stream-level
conditions of the *subscription itself* (a reconnect happened, a refresh was
rejected). They are delivered as data through this same event type rather
than raised, because "keep going, but reconcile" is not a stream-ending
condition — see :mod:`niwaki.exceptions._subscription` for the mirror image
of this asymmetry: a subscription that truly cannot be recovered raises
``SubscriptionLostError`` out of iteration instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from niwaki.models.base import ManagedObject
from niwaki.transport._subscription_socket import (
    RawPushItem,
    RawSubscriptionEvent,
    SubscriptionGap,
)


class EventKind(StrEnum):
    """What a :class:`SubscriptionEvent` represents.

    ``CREATED``/``MODIFIED``/``DELETED`` values equal the wire ``status``
    string a push carries, so ``EventKind(raw_status)`` maps directly.
    ``GAP``/``REFRESH_FAILED`` have no wire equivalent — they are synthesised
    by the transport layer for conditions of the subscription itself, not the
    watched object.
    """

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    GAP = "gap"
    REFRESH_FAILED = "refresh_failed"


@dataclass(frozen=True, slots=True)
class SubscriptionEvent[T: ManagedObject]:
    """One item from a :class:`~niwaki.query._subscription.Subscription`'s live stream.

    Attributes:
        kind: What happened — see :class:`EventKind`.
        mo: The typed object, deserialised via
            :meth:`~niwaki.models.ManagedObject.from_event` — its
            ``model_fields_set`` reports exactly the fields *this event's
            payload* carried (empty for a ``DELETED`` event, the full set for
            a ``CREATED`` one). ``None`` for ``GAP``/``REFRESH_FAILED``, which
            describe the subscription itself, not a watched object.
        subscription_ids: Every wire ``subscriptionId`` this push satisfied.
            Empty for ``GAP``/``REFRESH_FAILED``, which are per-subscription
            already (delivered only into the affected subscription's stream).
        raw: The transport-layer item this event was built from — a
            :class:`~niwaki.transport._subscription_socket.SubscriptionGap` or
            :class:`~niwaki.transport._subscription_socket.SubscriptionRefreshFailed`
            for those two kinds, holding their own detail (old/new
            subscription id, timestamps); the originating
            :class:`~niwaki.transport._subscription_socket.RawSubscriptionEvent`
            otherwise.
    """

    kind: EventKind
    mo: T | None
    subscription_ids: tuple[str, ...]
    raw: RawPushItem

    @property
    def dn(self) -> str | None:
        """``self.mo.dn``, or ``None`` when there is no object (gap/refresh-failed)."""
        return self.mo.dn if self.mo is not None else None

    @property
    def class_name(self) -> str | None:
        """The ACI wire class name, or ``None`` when there is no object."""
        return None if self.mo is None else self.mo._wire_class


def event_from_raw(item: RawPushItem) -> SubscriptionEvent[ManagedObject]:
    """Turn one transport-layer push item into a typed :class:`SubscriptionEvent`.

    Args:
        item: A raw item pulled off a
            :class:`~niwaki.transport._subscription_socket.RawSubscription`.

    Returns:
        The typed event. For a :class:`~niwaki.transport._subscription_socket.RawSubscriptionEvent`,
        the wire attributes are reconstructed into an envelope and run through
        :meth:`~niwaki.models.ManagedObject.from_event`; an unrecognised
        ``status`` value falls back to ``MODIFIED`` (matching the transport
        layer's own fallback) rather than raising, since a reader loop must
        not crash on an APIC value it has not seen before.
    """
    if isinstance(item, RawSubscriptionEvent):
        envelope = {item.class_name: {"attributes": item.attributes}}
        mo = ManagedObject.from_event(envelope)
        try:
            kind = EventKind(item.status)
        except ValueError:
            kind = EventKind.MODIFIED
        return SubscriptionEvent(kind=kind, mo=mo, subscription_ids=item.subscription_ids, raw=item)
    if isinstance(item, SubscriptionGap):
        return SubscriptionEvent(kind=EventKind.GAP, mo=None, subscription_ids=(), raw=item)
    return SubscriptionEvent(kind=EventKind.REFRESH_FAILED, mo=None, subscription_ids=(), raw=item)
