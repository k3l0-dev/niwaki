"""ACI query builder — the execution methods.

:class:`AsyncQuery` adds the execution methods (``fetch``, ``first``, ``one``,
``exists``, ``count``, ``stream``, ``subscribe``, ``execute_raw``) on top of
the shared builder state in :class:`~niwaki.query._base._QueryBase`.

Typical usage::

    from niwaki import AsyncNiwaki
    from niwaki.query import wcard

    async with AsyncNiwaki(...) as aci:
        # Jargon navigation — no class name needed
        bds = await aci.root.tenant("prod").bd().where(name="web").fetch()

        # Class-level global query, streamed page by page
        async for bd in aci.query(fvBD).where(wcard("name", "prod-*")).stream():
            print(bd.name)

        # Unregistered / read-only class by string
        nodes = await aci.query("topSystem").naming_only().fetch()
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import (
    TYPE_CHECKING,
    Never,  # unasync: async-only
    TypeVar,
    cast,
)

from niwaki.models.base import ManagedObject
from niwaki.query._base import _QueryBase  # pyright: ignore[reportPrivateUsage]
from niwaki.transport._errors import json_data, total_count
from niwaki.transport._subscription_socket import _DEFAULT_MAX_PENDING
from niwaki.transport.session_async import AsyncApicSession

if TYPE_CHECKING:
    from niwaki.query._async_subscription import AsyncSubscription

_T = TypeVar("_T", bound=ManagedObject)


class AsyncQuery(_QueryBase[_T]):
    """Fluent ACI query builder.

    Build queries by chaining scope, filter, and enrichment methods, then
    execute with :meth:`fetch`, :meth:`first`, :meth:`one`, :meth:`exists`,
    :meth:`count`, or :meth:`stream` — or iterate the query itself.  Only the
    execution methods touch the network.

    Every accumulator method returns a **new** :class:`AsyncQuery` instance so
    partial queries can be safely stored and reused.

    Created by :meth:`~niwaki.AsyncNiwaki.query` and
    :meth:`~niwaki.AsyncNiwakiNode.query`, or by jargon navigation without
    a name argument (e.g. ``aci.root.tenant("prod").bd()``).

    Args:
        cls:      ACI class type or plain string class name.
        session:  Authenticated :class:`~niwaki.transport.session_async.AsyncApicSession`.
        scope_dn: Optional DN to scope the query.

    Example::

        async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
            # All BDs in tenant "prod"
            bds = await aci.root.tenant("prod").bd().fetch()

            # First BD matching a name
            bd = await aci.query(fvBD).where(name="web").first()

            # Count BDs with flood enabled
            n = await aci.query(fvBD).where(arpFlood=True).count()

            # Stream a very large result set
            async for bd in aci.query(fvBD).stream():
                await process(bd)
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

    # ── Execution ─────────────────────────────────────────────────────────────

    async def fetch(self) -> list[_T]:
        """Execute the query and return all matching objects.

        Transparently paginates through all APIC pages.  For very large result
        sets (tens of thousands of objects) consider :meth:`stream` to process
        objects page-by-page without holding everything in memory at once.  When
        the query is limited by a ``[:n]`` slice, only that many objects are
        fetched.

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
        from niwaki.utils.response import parse_envelopes

        if self._limit is not None:
            return [obj async for obj in self.stream()]
        path, params = self.build()
        raw = await self._session._get_all_pages(path, params, page_size=self._page_size)
        return cast(list[_T], parse_envelopes(raw))

    def __aiter__(self) -> AsyncIterator[_T]:
        """Iterate the query lazily — ``async for obj in query`` streams page by page.

        Equivalent to :meth:`stream`; it also makes ``[obj async for obj in query]``
        and a ``query[:n]`` slice work directly, honouring any limit set by
        slicing.

        Yields:
            Typed :class:`~niwaki.models.base.ManagedObject` instances.
        """
        return self.stream()

    # unasync: async-only-begin
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

    # unasync: async-only-end

    async def first(self) -> _T | None:
        """Execute the query and return the first matching object, or ``None``.

        More efficient than indexing the result of :meth:`fetch` — internally
        requests only a single object (``page=0&page-size=1``).

        Returns:
            First matching instance, or ``None`` when the result set is empty.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            bd = await aci.root.tenant("prod").bd().where(name="web").first()
            if bd is None:
                print("not found")
        """
        from niwaki.utils.response import parse_envelopes

        if self._limit == 0:
            # A q[:0] slice matches nothing anywhere else (fetch, count,
            # exists) — first() must agree instead of quietly ignoring it.
            return None
        path, params = self.build()
        params = {**params, "page": "0", "page-size": "1"}
        raw = await self._session._get_imdata(path, params)
        objects = parse_envelopes(raw)
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
        from niwaki.utils.response import parse_envelopes

        if self._limit == 0:
            raise NoResultError(f"one() matched no {self._aci_class} object (limit 0)")
        path, params = self.build()
        # A q[:1] slice caps the result set at one, so the two-object probe
        # must not out-read it and report a MultipleResultsError the sliced
        # query cannot produce.
        probe = "1" if self._limit == 1 else "2"
        params = {**params, "page": "0", "page-size": probe}
        raw = await self._session._get_imdata(path, params)
        objects = parse_envelopes(raw)
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

        Issues a single one-object page and reads the APIC ``totalCount`` — this
        composes with any query and works on every APIC version (6.0 rejects the
        ``count-only`` argument).

        Returns:
            Integer count of objects matching the current query.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            n = await aci.query(fvBD).under("uni/tn-prod").count()
            print(f"{n} BDs in tenant prod")
        """
        if self._limit == 0:
            return 0
        path, params = self.build()
        # A minimal one-object page still carries the full totalCount —
        # unlike "count-only", this composes with any query and every
        # APIC version (6.0 rejects the count-only argument).
        params = {**params, "page": "0", "page-size": "1"}
        resp = await self._session._request_checked(path, params)
        total = total_count(resp, json_data(resp))
        # A sliced query (q[:n]) counts what it would actually yield.
        return min(total, self._limit) if self._limit is not None else total

    async def stream(self) -> AsyncIterator[_T]:
        """Yield objects one page at a time — O(page_size) memory footprint.

        Preferred over :meth:`fetch` for large result sets where loading
        everything into a list would consume excessive memory.

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
        from niwaki.utils.response import parse_envelopes

        limit = self._limit
        if limit == 0:
            return
        path, params = self.build()
        yielded = 0
        async for page in self._session._aiter_pages(
            path, params, page_size=self._effective_page_size()
        ):
            for obj in cast(list[_T], parse_envelopes(page)):
                yield obj
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    async def subscribe(
        self, *, refresh_timeout: int | None = None, max_pending: int = _DEFAULT_MAX_PENDING
    ) -> AsyncSubscription[_T]:
        """Subscribe to push notifications for this query.

        A subscription is a query plus ``subscription=yes`` at the wire
        level, so every accumulator that maps onto the same GET mechanism —
        :meth:`where`, :meth:`under`, :meth:`self_only`/:meth:`children`,
        :meth:`naming_only`/:meth:`config_only` — carries over unchanged.
        State with no meaning on an open-ended push stream (``order_by()``, a
        slice limit, subtree enrichment, ``also()``) is rejected up front,
        before any network I/O — see :meth:`~niwaki.query._base._QueryBase._reject_unstreamable`
        for exactly why each one is rejected. ``page_size()`` is silently
        ignored (it never reaches a request parameter here — the subscribe
        response is a single page).

        The APIC multiplexes every subscription for a session over **one**
        WebSocket, opened lazily on the first call to this method on the
        session; refresh and reconnect-and-resubscribe run automatically in
        the background, so nothing here needs a caller-driven loop.

        Args:
            refresh_timeout: Override the APIC's default 60 s subscription
                timeout. The subscription refreshes itself automatically on a
                schedule derived from this value regardless.
            max_pending: Bound on buffered, not-yet-consumed events for this
                subscription. Past it, incoming events are dropped (other
                subscriptions are never affected) and the stream receives one
                ``SubscriptionOverflow`` marker per overload episode —
                reconcile with a fresh read, exactly like a gap.

        Returns:
            The :class:`~niwaki.query._async_subscription.AsyncSubscription`
            handle — ``.initial`` for the synchronous snapshot, then iterate it
            for live push events.

        Raises:
            ValueError: The query carries state with no meaning on a live
                stream (``order_by()``, a slice limit, subtree enrichment,
                ``also()``).
            StatsClassNotSubscribableError: The queried class is a statistics
                class — the APIC never pushes for it.
            SubscribeRejectedError: The APIC rejected the subscribe request.

        Example::

            async with aci.query(fvBD).under("uni/tn-prod").subscribe() as sub:
                async for event in sub:
                    print(event.kind, event.dn)
        """
        from niwaki.query._async_subscription import AsyncSubscription

        path, params = self._subscription_build()
        raw = await self._session.subscribe(
            path, params, refresh_timeout=refresh_timeout, max_pending=max_pending
        )
        return AsyncSubscription(raw)

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

        To count matching objects, use :meth:`count` — it is the only counting
        idiom this SDK trusts.  This method returns objects, so a parameter that
        makes the APIC answer with a tally instead yields one ``moCount``
        envelope where the caller expected their class.

        Example::

            # Start from build() so scoping and filters are already right, then
            # set whatever the query grammar does not model yet and run it
            # through the same typed, paginated path.
            path, params = aci.query(fvBD).under("uni/tn-prod").build()
            params["some-apic-parameter"] = "value"
            objs = await aci.query(fvBD).execute_raw(path, params)
        """
        from niwaki.utils.response import parse_envelopes

        raw = await self._session._get_all_pages(path, params, page_size=self._page_size)
        return parse_envelopes(raw)
