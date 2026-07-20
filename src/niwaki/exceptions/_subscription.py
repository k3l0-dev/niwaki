"""APIC object-subscription (WebSocket push) exceptions.

Two failure modes on a live subscription are deliberately **not** exceptions —
a refresh rejection and a reconnect-induced gap are carried as
``EventKind.REFRESH_FAILED`` / ``EventKind.GAP`` items in the event stream
itself (see :mod:`niwaki.query._events`), because "keep going, but reconcile"
is not a stream-ending condition. Only a subscription that cannot be
recovered at all raises out of iteration (:class:`SubscriptionLostError`).
This split — some failures are exceptions, some are stream data — is
intentional; keep it that way when extending this hierarchy.
"""

from __future__ import annotations

from enum import StrEnum

from niwaki.exceptions._api import APIError
from niwaki.exceptions._base import NiwakiError


class SubscriptionError(NiwakiError):
    """Base class for every object-subscription failure."""


class StatsClassNotSubscribableError(SubscriptionError):
    """A subscription targeted a class the APIC can never push for.

    Raised before any network I/O. Stats classes (``isStat`` in the read
    catalogue) bypass the APIC's internal event manager entirely — Cisco's own
    documentation states updates are "too frequent and not scalable" to route
    through it — so a subscription would be silently accepted and never push
    anything. This is an architectural fact, unlike ``isObservable`` (see
    :attr:`~niwaki.catalog.ClassDoc.is_observable`), which was empirically
    found *not* to gate subscribability and is therefore never enforced here.
    """


class SubscribeRejectedError(SubscriptionError, APIError):
    """The APIC rejected a ``subscription=yes`` request.

    Multiply inherits :class:`~niwaki.exceptions.APIError` for the familiar
    ``status_code``/``apic_message`` attributes (precedent:
    ``UnknownMakerError(DesignError, AttributeError)``), so a caller can
    inspect the HTTP status while still catching every subscription failure
    with a single ``except SubscriptionError``.
    """


class SubscriptionLostReason(StrEnum):
    """Which recovery path was exhausted before :class:`SubscriptionLostError` was raised.

    Distinguishes three distinct fatal paths that would otherwise be
    indistinguishable from the error message alone:

    - ``RECONNECT_EXHAUSTED``: the shared WebSocket itself could not be
      reconnected — every tracked subscription on the socket receives this.
    - ``RESUBSCRIBE_FAILED``: the socket reconnected, but the APIC rejected
      *this* subscription's resubscribe — sibling subscriptions on the same
      socket may still be fine.
    - ``REFRESH_ESCALATION``: two consecutive scheduled refreshes failed, and
      the recovery resubscribe attempted for *this* subscription alone also
      failed — the socket connection itself was never affected.
    """

    RECONNECT_EXHAUSTED = "reconnect_exhausted"
    RESUBSCRIBE_FAILED = "resubscribe_failed"
    REFRESH_ESCALATION = "refresh_escalation"


class SubscriptionLostError(SubscriptionError):
    """A subscription could not be recovered.

    Raised out of the subscription's iterator (``__next__``/``__anext__``),
    terminating it. This is the one truly fatal outcome — everything short of
    it (a missed refresh, a reconnect that *did* succeed) is represented as
    data in the event stream instead, because there is something left to
    reconcile toward. See :attr:`reason` for which recovery path was
    exhausted.

    Attributes:
        reason: Which recovery path failed — see :class:`SubscriptionLostReason`.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: SubscriptionLostReason = SubscriptionLostReason.RECONNECT_EXHAUSTED,
    ) -> None:
        super().__init__(message)
        self.reason = reason
