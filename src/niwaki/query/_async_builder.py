"""Asynchronous ACI query builder.

:class:`AsyncQuery` mirrors :class:`~niwaki.query.Query` for async contexts.
All execution methods are coroutines (``fetch``, ``first``, ``count``) or
async generators (``stream``).

Typical usage::

    from niwaki import AsyncNiwaki
    from niwaki.query import wcard

    async with AsyncNiwaki(...) as aci:
        # Jargon navigation — no class name required
        bds = await aci.root.tenant("prod").bd().fetch()

        # Global class query with filter
        bds = await aci.query(fvBD).where(wcard("name", "prod-*")).fetch()

        # Concurrent queries via gather()
        tenants, bds = await aci.gather(
            aci.query(fvTenant).fetch(),
            aci.root.tenant("prod").bd().fetch(),
        )

        # Async streaming
        async for bd in aci.query(fvBD).stream():
            await process(bd)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TypeVar, cast

from niwaki.models.base import ManagedObject
from niwaki.query._base import _QueryBase  # pyright: ignore[reportPrivateUsage]
from niwaki.transport.session_async import AsyncApicSession

_T = TypeVar("_T", bound=ManagedObject)


class AsyncQuery(_QueryBase[_T]):
    """Fluent ACI query builder — asynchronous variant.

    Mirrors :class:`~niwaki.query.Query` with ``async def`` execution methods.
    Every accumulator method is synchronous and returns a new :class:`AsyncQuery`
    (same immutable-builder pattern as the sync variant).

    Created by :meth:`~niwaki.AsyncNiwaki.query` and
    :meth:`~niwaki.AsyncNiwakiNode.query`, or by jargon navigation without
    a name argument on an :class:`~niwaki.AsyncNiwakiNode`.

    Args:
        cls:      ACI class type or plain string class name.
        session:  Authenticated :class:`~niwaki.transport.session_async.AsyncApicSession`.
        scope_dn: Optional DN to scope the query.

    Example::

        async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
            # All BDs in tenant "prod"
            bds = await aci.root.tenant("prod").bd().fetch()

            # First matching
            bd = await aci.query(fvBD).where(name="web").first()

            # Count
            n = await aci.query(fvBD).under("uni/tn-prod").count()

            # Concurrent reads
            tenants, bds = await aci.gather(
                aci.query(fvTenant).fetch(),
                aci.root.tenant("prod").bd().fetch(),
            )
    """

    def __init__(
        self,
        cls: type[_T] | str,
        session: AsyncApicSession,
        *,
        scope_dn: str | None = None,
    ) -> None:
        super().__init__(cls, scope_dn=scope_dn)
        self._session = session

    # ── Async execution ───────────────────────────────────────────────────────

    async def fetch(self) -> list[_T]:
        """Execute the query and return all matching objects.

        Transparently paginates through all APIC pages.  For very large result
        sets consider :meth:`stream` to process objects incrementally.

        Returns:
            List of typed :class:`~niwaki.models.base.ManagedObject` instances.
            Empty list when no objects match.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient APIC privileges.
            ServerError: APIC server-side error.
            ConnectionError: Network error after all retry attempts.

        Example::

            bds = await aci.root.tenant("prod").bd().fetch()
        """
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        raw = await self._session._get_all_pages(path, params, page_size=self._page_size)
        return cast(list[_T], parse_imdata({"imdata": raw}))

    async def first(self) -> _T | None:
        """Execute the query and return the first matching object, or ``None``.

        Requests only a single object (``page=0&page-size=1``) — more efficient
        than ``(await fetch())[0]`` for large result sets.

        Returns:
            First matching instance, or ``None`` when the result set is empty.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            bd = await aci.root.tenant("prod").bd().where(name="web").first()
        """
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        params = {**params, "page": "0", "page-size": "1"}
        raw = await self._session._get_imdata(path, params)
        objects = parse_imdata({"imdata": raw})
        return cast(_T, objects[0]) if objects else None

    async def count(self) -> int:
        """Return the count of matching objects without fetching them.

        Issues a single one-object page and reads the APIC ``totalCount``.

        Returns:
            Integer count of objects matching the current query.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            n = await aci.query(fvBD).under("uni/tn-prod").count()
        """
        path, params = self.build()
        # A minimal one-object page still carries the full totalCount —
        # unlike "count-only", this composes with any query and every
        # APIC version (6.0 rejects the count-only argument).
        params = {**params, "page": "0", "page-size": "1"}
        data: dict[str, Any] = (await self._session._request_checked(path, params)).json()
        return int(data.get("totalCount", 0))

    async def stream(self) -> AsyncIterator[_T]:
        """Yield objects one page at a time — O(page_size) memory footprint.

        Preferred over :meth:`fetch` for large result sets.  Each ``yield``
        returns one page of parsed objects.

        Yields:
            Typed :class:`~niwaki.models.base.ManagedObject` instances in
            APIC-returned order.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            async for bd in aci.query(fvBD).stream():
                await process(bd)
        """
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        async for page in self._session._aiter_pages(path, params, page_size=self._page_size):
            for obj in cast(list[_T], parse_imdata({"imdata": page})):
                yield obj
