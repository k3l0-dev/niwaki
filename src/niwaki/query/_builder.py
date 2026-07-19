"""Synchronous ACI query builder.

:class:`Query` adds sync execution methods on top of the shared builder state
in :class:`~niwaki.query._base._QueryBase`.

Typical usage::

    from niwaki import Niwaki
    from niwaki.query import eq, wcard

    with Niwaki(...) as aci:
        # Jargon navigation — no class name needed
        bds = aci.root.tenant("prod").bd().where(name="web").fetch()

        # Class-level global query
        for bd in aci.query(fvBD).where(wcard("name", "prod-*")).stream():
            print(bd.name)

        # Unregistered / read-only class by string
        nodes = aci.query("topSystem").naming_only().fetch()
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypeVar, cast

from niwaki.models.base import ManagedObject
from niwaki.query._base import _QueryBase  # pyright: ignore[reportPrivateUsage]
from niwaki.transport.session import ApicSession

_T = TypeVar("_T", bound=ManagedObject)


class Query(_QueryBase[_T]):
    """Fluent ACI query builder — synchronous variant.

    Build queries by chaining scope, filter, and enrichment methods, then
    execute with :meth:`fetch`, :meth:`first`, :meth:`count`, or :meth:`stream`.

    Every accumulator method returns a **new** :class:`Query` instance so
    partial queries can be safely stored and reused.

    Created by :meth:`~niwaki.Niwaki.query` and
    :meth:`~niwaki.NiwakiNode.query`, or by jargon navigation without
    a name argument (e.g. ``aci.root.tenant("prod").bd()``).

    Args:
        cls:      ACI class type or plain string class name.
        session:  Authenticated :class:`~niwaki.transport.session.ApicSession`.
        scope_dn: Optional DN to scope the query.

    Example::

        with Niwaki("https://apic.example.com", "admin", "secret") as aci:
            # All BDs in tenant "prod"
            bds = aci.root.tenant("prod").bd().fetch()

            # First BD matching a name pattern
            bd = aci.query(fvBD).where(name="web").first()

            # Count BDs with flood enabled
            n = aci.query(fvBD).where(arpFlood=True).count()

            # Stream a very large result set
            for bd in aci.query(fvBD).stream():
                process(bd)
    """

    def __init__(
        self,
        cls: type[_T] | str,
        session: ApicSession,
        *,
        scope_dn: str | None = None,
    ) -> None:
        super().__init__(cls, scope_dn=scope_dn)
        self._session = session

    # ── Execution ─────────────────────────────────────────────────────────────

    def fetch(self) -> list[_T]:
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

            bds = aci.root.tenant("prod").bd().fetch()
        """
        from niwaki.utils.response import parse_imdata

        if self._limit is not None:
            return list(self.stream())
        path, params = self.build()
        raw = self._session._get_all_pages(path, params, page_size=self._page_size)
        return cast(list[_T], parse_imdata({"imdata": raw}))

    def __iter__(self) -> Iterator[_T]:
        """Iterate the query lazily — ``for obj in query`` streams page by page.

        Equivalent to :meth:`stream`; it also makes ``list(query)`` and a
        ``query[:n]`` slice work directly, honouring any limit set by slicing.

        Yields:
            Typed :class:`~niwaki.models.base.ManagedObject` instances.
        """
        return self.stream()

    def first(self) -> _T | None:
        """Execute the query and return the first matching object, or ``None``.

        More efficient than ``fetch()[0]`` — internally requests only a single
        object (``page=0&page-size=1``).

        Returns:
            First matching instance, or ``None`` when the result set is empty.

        Raises:
            AuthError: Session not authenticated.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            bd = aci.root.tenant("prod").bd().where(name="web").first()
            if bd is None:
                print("not found")
        """
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        params = {**params, "page": "0", "page-size": "1"}
        raw = self._session._get_imdata(path, params)
        objects = parse_imdata({"imdata": raw})
        return cast(_T, objects[0]) if objects else None

    def one(self) -> _T:
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

            bd = aci.query(fvBD).where(name="web").one()
        """
        from niwaki.exceptions._query import MultipleResultsError, NoResultError
        from niwaki.utils.response import parse_imdata

        path, params = self.build()
        params = {**params, "page": "0", "page-size": "2"}
        raw = self._session._get_imdata(path, params)
        objects = parse_imdata({"imdata": raw})
        if not objects:
            raise NoResultError(f"one() matched no {self._aci_class} object")
        if len(objects) > 1:
            raise MultipleResultsError(
                f"one() matched more than one {self._aci_class} object; narrow the "
                "query or use first()/fetch()"
            )
        return cast(_T, objects[0])

    def exists(self) -> bool:
        """Return whether any object matches — a single lightweight request.

        Returns:
            ``True`` when at least one object matches, ``False`` otherwise.

        Example::

            if aci.query(fvBD).where(name="web").exists():
                ...
        """
        return self.count() > 0

    def count(self) -> int:
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

            n = aci.query(fvBD).under("uni/tn-prod").count()
            print(f"{n} BDs in tenant prod")
        """
        if self._limit == 0:
            return 0
        path, params = self.build()
        # A minimal one-object page still carries the full totalCount —
        # unlike "count-only", this composes with any query and every
        # APIC version (6.0 rejects the count-only argument).
        params = {**params, "page": "0", "page-size": "1"}
        data: dict[str, Any] = self._session._request_checked(path, params).json()
        total = int(data.get("totalCount", 0))
        # A sliced query (q[:n]) counts what it would actually yield.
        return min(total, self._limit) if self._limit is not None else total

    def stream(self) -> Iterator[_T]:
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

            for bd in aci.query(fvBD).stream():
                process(bd)
        """
        from niwaki.utils.response import parse_imdata

        limit = self._limit
        if limit == 0:
            return
        path, params = self.build()
        yielded = 0
        for page in self._session._iter_pages(path, params, page_size=self._effective_page_size()):
            for obj in cast(list[_T], parse_imdata({"imdata": page})):
                yield obj
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def execute_raw(self, path: str, params: dict[str, str]) -> list[ManagedObject]:
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

        Example::

            path, params = aci.query(fvBD).build()
            params["rsp-subtree-include"] = "count"
            objs = aci.query(fvBD).execute_raw(path, params)
        """
        from niwaki.utils.response import parse_imdata

        raw = self._session._get_all_pages(path, params, page_size=self._page_size)
        return parse_imdata({"imdata": raw})
