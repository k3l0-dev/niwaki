"""The user-facing object-subscription handle.

:class:`AsyncSubscription` wraps a transport-level
:class:`~niwaki.transport._subscription_socket_async.AsyncRawSubscription`
with the typed event layer (:mod:`niwaki.query._events`): the raw wire shapes
never reach a caller directly.  The event layer does no I/O, so every
subscription handle shares it verbatim.
"""

from __future__ import annotations

from typing import cast

from niwaki.models.base import ManagedObject
from niwaki.query._events import SubscriptionEvent, event_from_raw
from niwaki.transport._subscription_socket import (
    # unasync: sync: RawSubscription,
    SubscriptionInfo,
)
from niwaki.transport._subscription_socket_async import AsyncRawSubscription  # unasync: async-only
from niwaki.utils.response import parse_envelopes


class AsyncSubscription[T: ManagedObject]:
    """A live object-subscription — iterate it for typed push events.

    Returned by :meth:`~niwaki.query.AsyncQuery.subscribe`. Refresh and
    reconnect-and-resubscribe are handled automatically in the background —
    nothing here needs a caller-driven loop beyond iterating the stream.

    A subscription is both an iterator and a context manager, used as below::

        async with aci.query(fvBD).under("uni/tn-prod").subscribe() as sub:
            for bd in sub.initial:
                print("already there:", bd.dn)
            async for event in sub:
                print(event.kind, event.dn, event.mo.model_fields_set if event.mo else None)

    Attributes:
        initial: The subscribe response's own synchronous snapshot — a single,
            un-paginated page (not the exhaustive read
            :meth:`~niwaki.query.AsyncQuery.fetch` would give you), typed via
            the normal :meth:`~niwaki.models.ManagedObject.from_apic` path
            since it is a real read, not a push event.
    """

    def __init__(self, raw: AsyncRawSubscription) -> None:
        self._raw = raw
        self.initial: list[T] = cast(list[T], parse_envelopes(raw.initial))

    @property
    def subscription_id(self) -> str:
        """The current wire ``subscriptionId`` — for observability/debugging only.

        This changes across a reconnect (delivered to the stream as a
        :class:`~niwaki.query._events.EventKind.GAP` event); it is not a
        stable identity to key application state on.
        """
        return self._raw.subscription_id

    @property
    def info(self) -> SubscriptionInfo:
        """Current diagnostic snapshot of this subscription.

        See :meth:`~niwaki.query.AsyncQuery.subscribe`'s session-level
        counterpart for listing every subscription at once; this is the
        single-item equivalent.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        return self._raw.info

    async def refresh_now(self) -> SubscriptionInfo:
        """Force an immediate refresh of this subscription, outside its schedule.

        A manual refresh never triggers the automatic recovery escalation on
        failure — only the scheduled background refresh does — but a success
        resets the failure counter exactly like a scheduled refresh
        succeeding would.

        Returns:
            The updated :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`.

        Raises:
            SubscriptionError: This subscription has already been closed.
        """
        return await self._raw.refresh_now()

    def __aiter__(self) -> AsyncSubscription[T]:
        return self

    async def __anext__(self) -> SubscriptionEvent[T]:
        """Wait for the next event, gap marker, or refresh-failure marker.

        Raises:
            StopAsyncIteration: :meth:`close` was called (from anywhere).
            SubscriptionLostError: Reconnect-and-resubscribe was exhausted —
                this subscription cannot be recovered.
        """
        return cast(SubscriptionEvent[T], event_from_raw(await self._raw.__anext__()))

    async def close(self) -> None:
        """Stop this subscription: local bookkeeping only.

        No server-side "unsubscribe" endpoint is known to exist — this ends
        the local iterator and stops routing pushes to it. It does not close
        the session's shared WebSocket, which stays open for any other active
        subscription.
        """
        await self._raw.close()

    async def __aenter__(self) -> AsyncSubscription[T]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
