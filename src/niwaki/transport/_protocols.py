"""Transport boundary protocols — what the upper layers may ask of a session.

They document the shape of the session surface the upper layers rely on:
the design engine's wave runner consumes :class:`AsyncMoWriter`; the other
protocols serve as checkable contracts for test stubs and as the reference
of what each capability requires.

Protocols:
    :class:`MoWriter` / :class:`AsyncMoWriter` — ``post_mo`` / ``delete_mo``.
    :class:`MoReader` / :class:`AsyncMoReader` — typed single-MO ``get_mo``.
    :class:`MoSubscriber` / :class:`AsyncMoSubscriber` — object-subscription
        (WebSocket push).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from niwaki.models.base import ManagedObject
from niwaki.transport._subscription_socket import (
    _DEFAULT_MAX_PENDING,
    RawSubscription,
    SubscriptionInfo,
    _QueueItem,
)
from niwaki.transport._subscription_socket_async import AsyncRawSubscription


class _Closable(Protocol):
    """What the sync subscription finalizer does to a connection: close it.

    The finalizer handle holds only this capability, so a test double needs
    nothing more than a ``close()``.
    """

    def close(self) -> None: ...


class _ShutdownCapable(Protocol):
    """What the async subscription finalizer does to a raw socket: shut it down.

    The finalizer handle holds only this capability, so a test double needs
    nothing more than a ``shutdown(how)``.
    """

    def shutdown(self, how: int, /) -> None: ...


class _ItemSink(Protocol):
    """The enqueue surface ``queue.Queue`` and ``asyncio.Queue`` share.

    ``put_nowait`` is the only enqueue both offer. On the registrations'
    queues, which are always unbounded (the bound lives in
    :func:`~niwaki.transport._subscription_socket._offer`), it delivers
    exactly like the blocking ``queue.Queue.put``: ``block`` only matters
    while waiting for a free slot, and an unbounded queue always has one.
    """

    def qsize(self) -> int: ...

    def put_nowait(self, item: _QueueItem, /) -> None: ...


class _RegistrationState(Protocol):
    """The registration fields the shared helpers read or write.

    Satisfied structurally by both
    :class:`~niwaki.transport._subscription_socket._Registration` and the
    async module's ``_AsyncRegistration`` (identical fields, a different
    queue type). Only the backpressure bookkeeping is writable here; everything
    else is read-only to the shared helpers.
    """

    @property
    def path(self) -> str: ...

    @property
    def params(self) -> dict[str, str]: ...

    @property
    def refresh_timeout(self) -> int | None: ...

    @property
    def wire_id(self) -> str: ...

    @property
    def queue(self) -> _ItemSink: ...

    @property
    def next_refresh_at(self) -> float: ...

    @property
    def consecutive_refresh_failures(self) -> int: ...

    @property
    def max_pending(self) -> int: ...

    dropped_total: int
    overflow_open: bool


@runtime_checkable
class MoWriter(Protocol):
    """Structural type for synchronous ACI write transports."""

    def post_mo(self, dn: str, payload: Mapping[str, object]) -> None:
        """POST an APIC envelope to the given DN.

        Args:
            dn: Full Distinguished Name of the target object.
            payload: APIC envelope dict (``{"fvBD": {"attributes": {...}}}``).
        """
        ...

    def delete_mo(self, dn: str) -> None:
        """DELETE the ACI object at the given DN.

        Args:
            dn: Full Distinguished Name of the target object.
        """
        ...


@runtime_checkable
class AsyncMoWriter(Protocol):
    """Structural type for asynchronous ACI write transports.

    Example — minimal test stub::

        class FakeSession:
            async def post_mo(self, dn: str, payload: Mapping[str, object]) -> None:
                print(f"POST {dn}")

            async def delete_mo(self, dn: str) -> None:
                print(f"DELETE {dn}")
    """

    async def post_mo(self, dn: str, payload: Mapping[str, object]) -> None:
        """POST an APIC envelope to the given DN.

        Args:
            dn: Full Distinguished Name of the target object.
            payload: APIC envelope dict (``{"fvBD": {"attributes": {...}}}``).
        """
        ...

    async def delete_mo(self, dn: str) -> None:
        """DELETE the ACI object at the given DN.

        Args:
            dn: Full Distinguished Name of the target object.
        """
        ...


@runtime_checkable
class MoReader(Protocol):
    """Structural type for synchronous typed single-MO reads."""

    def get_mo[T: ManagedObject](self, dn: str, cls: type[T]) -> T:
        """Fetch one MO by DN, typed as *cls*.

        Raises:
            NotFoundError: No object exists at *dn*.
        """
        ...


@runtime_checkable
class AsyncMoReader(Protocol):
    """Structural type for asynchronous typed single-MO reads."""

    async def get_mo[T: ManagedObject](self, dn: str, cls: type[T]) -> T:
        """Fetch one MO by DN, typed as *cls*.

        Raises:
            NotFoundError: No object exists at *dn*.
        """
        ...


@runtime_checkable
class MoSubscriber(Protocol):
    """Structural type for synchronous ACI object-subscription transports."""

    def subscribe(
        self,
        path: str,
        params: dict[str, str],
        *,
        refresh_timeout: int | None = None,
        max_pending: int = _DEFAULT_MAX_PENDING,
    ) -> RawSubscription:
        """Subscribe to push notifications for a query.

        Args:
            path: API path relative to base URL, exactly as passed to a
                normal GET (e.g. ``"/api/class/fvBD.json"``).
            params: Query string parameters (filters/scoping). ``subscription``
                and ``refresh-timeout`` are added internally.
            refresh_timeout: Override the APIC's default 60 s subscription
                timeout. The subscription refreshes itself automatically
                regardless of this value.
            max_pending: Bound on buffered, not-yet-consumed events for this
                subscription. Past it, incoming events are dropped (other
                subscriptions are never affected) and the stream receives one
                ``SubscriptionOverflow`` marker per overload episode —
                reconcile with a fresh read, exactly like a gap.

        Returns:
            A :class:`~niwaki.transport._subscription_socket.RawSubscription`.

        Raises:
            SubscribeRejectedError: The APIC rejected the subscribe request.
        """
        ...

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List every subscription currently tracked, or ``[]`` if none was ever opened."""
        ...

    def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand."""
        ...

    def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open."""
        ...


@runtime_checkable
class AsyncMoSubscriber(Protocol):
    """Structural type for asynchronous ACI object-subscription transports."""

    async def subscribe(
        self,
        path: str,
        params: dict[str, str],
        *,
        refresh_timeout: int | None = None,
        max_pending: int = _DEFAULT_MAX_PENDING,
    ) -> AsyncRawSubscription:
        """Subscribe to push notifications for a query.

        Args:
            path: API path relative to base URL, exactly as passed to a
                normal GET (e.g. ``"/api/class/fvBD.json"``).
            params: Query string parameters (filters/scoping). ``subscription``
                and ``refresh-timeout`` are added internally.
            refresh_timeout: Override the APIC's default 60 s subscription
                timeout. The subscription refreshes itself automatically
                regardless of this value.
            max_pending: Bound on buffered, not-yet-consumed events for this
                subscription. Past it, incoming events are dropped (other
                subscriptions are never affected) and the stream receives one
                ``SubscriptionOverflow`` marker per overload episode —
                reconcile with a fresh read, exactly like a gap.

        Returns:
            An :class:`~niwaki.transport._subscription_socket_async.AsyncRawSubscription`.

        Raises:
            SubscribeRejectedError: The APIC rejected the subscribe request.
        """
        ...

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List every subscription currently tracked, or ``[]`` if none was ever opened."""
        ...

    async def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand."""
        ...

    async def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open."""
        ...
