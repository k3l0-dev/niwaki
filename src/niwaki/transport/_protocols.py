"""Transport boundary protocols — what the upper layers may ask of a session.

The facade and the design push engine depend on these structural types
instead of the concrete session classes, so any object implementing the
methods is a valid session (test stubs included) and the boundary needs no
private-attribute reach-through.

Protocols:
    :class:`MoWriter` / :class:`AsyncMoWriter` — ``post_mo`` / ``delete_mo``.
    :class:`MoReader` / :class:`AsyncMoReader` — typed single-MO ``get_mo``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from niwaki.models.base import ManagedObject


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
