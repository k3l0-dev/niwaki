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
        objects page-by-page without holding everything in memory at once.

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

        path, params = self.build()
        raw = self._session._get_all_pages(path, params, page_size=self._page_size)
        return cast(list[_T], parse_imdata({"imdata": raw}))

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

    def count(self) -> int:
        """Return the count of matching objects without fetching them.

        Uses the APIC ``count-only=yes`` mode — a single lightweight request
        that returns only the ``totalCount`` header.

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
        path, params = self.build()
        # A minimal one-object page still carries the full totalCount —
        # unlike "count-only", this composes with any query and every
        # APIC version (6.0 rejects the count-only argument).
        params = {**params, "page": "0", "page-size": "1"}
        data: dict[str, Any] = self._session._request_checked(path, params).json()
        return int(data.get("totalCount", 0))

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

        path, params = self.build()
        for page in self._session._iter_pages(path, params, page_size=self._page_size):
            yield from cast(list[_T], parse_imdata({"imdata": page}))
