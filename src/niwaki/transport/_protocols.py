"""Transport boundary protocols — what the upper layers may ask of a session.

The facade and the design push engine depend on these structural types
instead of the concrete session classes, so any object implementing the
methods is a valid session (test stubs included) and the boundary needs no
private-attribute reach-through.

Protocols:
    :class:`MoWriter` / :class:`AsyncMoWriter` — ``post_mo`` / ``delete_mo``.
    :class:`MoReader` / :class:`AsyncMoReader` — typed single-MO ``get_mo``.
    :class:`MoSubscriber` / :class:`AsyncMoSubscriber` — object-subscription
        (WebSocket push).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from niwaki.models.base import ManagedObject
from niwaki.transport._subscription_socket import RawSubscription, SubscriptionInfo
from niwaki.transport._subscription_socket_async import AsyncRawSubscription


@runtime_checkable
class MoWriter(Protocol):
    """Structural type for synchronous ACI write transports."""

    def post_mo(self, dn: str, payload: dict[str, Any]) -> None:
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
            async def post_mo(self, dn: str, payload: dict[str, Any]) -> None:
                print(f"POST {dn}")

            async def delete_mo(self, dn: str) -> None:
                print(f"DELETE {dn}")
    """

    async def post_mo(self, dn: str, payload: dict[str, Any]) -> None:
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
        self, path: str, params: dict[str, str], *, refresh_timeout: int | None = None
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
        self, path: str, params: dict[str, str], *, refresh_timeout: int | None = None
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
