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
from typing import Any, Never, TypeVar, cast

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

        if self._limit is not None:
            return [obj async for obj in self.stream()]
        path, params = self.build()
        raw = await self._session._get_all_pages(path, params, page_size=self._page_size)
        return cast(list[_T], parse_imdata({"imdata": raw}))

    def __aiter__(self) -> AsyncIterator[_T]:
        """Async-iterate the query lazily — ``async for obj in query``.

        Equivalent to :meth:`stream`; it also makes ``[x async for x in query]``
        and a ``query[:n]`` slice work directly, honouring any limit set by
        slicing.

        Yields:
            Typed :class:`~niwaki.models.base.ManagedObject` instances.
        """
        return self.stream()

    def __iter__(self) -> Never:
        """Reject synchronous iteration — an async query needs ``async for``.

        Raises:
            TypeError: Always — use ``async for obj in query`` or
                ``await query.fetch()``.
        """
        raise TypeError(
            "AsyncQuery is not synchronously iterable — use 'async for obj in query' "
            "or 'await query.fetch()'"
        )

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

    async def one(self) -> _T:
        """Execute the query and return the single matching object.

        For queries that must resolve to exactly one object.  Fetches at most two
        objects (``page=0&page-size=2``) so it can tell "none", "one" and "more
        than one" apart in a single request.

        Returns:
            The one matching instance.

        Raises:
            NoResultError: No object matched — use :meth:`first` when *no match*
                is acceptable.
            MultipleResultsError: More than one object matched — narrow the query
                or use :meth:`first` / :meth:`fetch`.

        Example::

            bd = await aci.query(fvBD).where(name="web").one()
        """
        from niwaki.exceptions._query import MultipleResultsError, NoResultError
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        params = {**params, "page": "0", "page-size": "2"}
        raw = await self._session._get_imdata(path, params)
        objects = parse_imdata({"imdata": raw})
        if not objects:
            raise NoResultError(f"one() matched no {self._aci_class} object")
        if len(objects) > 1:
            raise MultipleResultsError(
                f"one() matched more than one {self._aci_class} object; narrow the "
                "query or use first()/fetch()"
            )
        return cast(_T, objects[0])

    async def exists(self) -> bool:
        """Return whether any object matches — a single lightweight request.

        Returns:
            ``True`` when at least one object matches, ``False`` otherwise.

        Example::

            if await aci.query(fvBD).where(name="web").exists():
                ...
        """
        return await self.count() > 0

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
        if self._limit == 0:
            return 0
        path, params = self.build()
        # A minimal one-object page still carries the full totalCount —
        # unlike "count-only", this composes with any query and every
        # APIC version (6.0 rejects the count-only argument).
        params = {**params, "page": "0", "page-size": "1"}
        data: dict[str, Any] = (await self._session._request_checked(path, params)).json()
        total = int(data.get("totalCount", 0))
        # A sliced query (q[:n]) counts what it would actually yield.
        return min(total, self._limit) if self._limit is not None else total

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

        limit = self._limit
        if limit == 0:
            return
        path, params = self.build()
        yielded = 0
        async for page in self._session._aiter_pages(
            path, params, page_size=self._effective_page_size()
        ):
            for obj in cast(list[_T], parse_imdata({"imdata": page})):
                yield obj
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    async def execute_raw(self, path: str, params: dict[str, str]) -> list[ManagedObject]:
        """Run raw APIC query params through the typed, paginated pipeline.

        The escape hatch for anything :meth:`build` cannot express yet: pass an
        APIC path and parameter dict (often derived from ``build()`` and then
        tweaked) and get back typed, fully-paginated objects — unlike the
        transport's raw ``get`` helper, which returns a single unparsed page.

        Args:
            path:   APIC-relative path (e.g. ``"/api/class/fvBD.json"``).
            params: APIC query-string parameters.

        Returns:
            All matching objects across every page, typed via ``REGISTRY``.
        """
        from niwaki.utils.response import parse_imdata

        raw = await self._session._get_all_pages(path, params, page_size=self._page_size)
        return parse_imdata({"imdata": raw})
