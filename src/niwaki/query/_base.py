"""Shared query builder state and accumulator methods.

:class:`_QueryBase` holds all mutable state and exposes the full chainable API.
The sync :class:`~niwaki.query.Query` and async :class:`~niwaki.query.AsyncQuery`
subclasses inherit everything here and only add execution methods (``fetch``,
``first``, ``count``, ``stream``).

This split avoids code duplication while keeping execution-layer concerns
(sync vs async) cleanly separated.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from niwaki.models.base import ManagedObject
from niwaki.query._filters import (  # pyright: ignore[reportPrivateUsage]
    FilterExpr,
    FilterValue,
    _kwarg_to_expr,
    _qualify,
    and_,
)


class SubtreeInclude(StrEnum):
    """A response-subtree facet an APIC query can embed (``rsp-subtree-include``).

    The exhaustive set the APIC offers.  Pass any of these to
    ``include_subtree()``; the ``with_*`` builder methods are ergonomic
    shortcuts for the common ones.
    """

    FAULTS = "faults"
    HEALTH = "health"
    STATS = "stats"
    RELATIONS = "relations"
    COUNT = "count"
    REQUIRED = "required"
    SUBTREE = "subtree"
    NO_SCOPED = "no-scoped"
    AUDIT_LOGS = "audit-logs"
    EVENT_LOGS = "event-logs"
    FAULT_RECORDS = "fault-records"
    HEALTH_RECORDS = "health-records"
    TASKS = "tasks"


class _QueryBase[T: ManagedObject]:
    """Immutable-style query builder shared by sync and async variants.

    Every mutating method returns a *new* :class:`_QueryBase` (or subclass)
    instance, leaving the original unchanged.  This allows partial queries to be
    stored and reused safely::

        base = aci.root.tenant("prod").bd()
        web_bds = base.where(name="web").fetch()
        all_bds = base.fetch()   # unaffected by the where() above

    This base holds accumulator state only — the HTTP session lives on the
    executing subclasses (:class:`~niwaki.query.Query` /
    :class:`~niwaki.query.AsyncQuery`), which own the transport calls.

    Args:
        cls:      ACI class (subclass of :class:`~niwaki.models.base.ManagedObject`)
                  or plain string class name for classes not in the generated set.
        scope_dn: When provided, scopes the query to the subtree rooted at this
                  Distinguished Name.  When ``None``, the query targets the global
                  class index (``/api/class/{cls}.json``).
    """

    def __init__(
        self,
        cls: type[T] | str,
        *,
        scope_dn: str | None = None,
    ) -> None:
        self._aci_class: str = cls if isinstance(cls, str) else cls.__name__
        self._scope_dn = scope_dn
        self._query_target: Literal["self", "children", "subtree"] = "subtree"
        self._also_classes: list[str] = []
        self._filter_expr: FilterExpr | None = None
        self._rsp_subtree: Literal["no", "children", "full"] = "no"
        self._rsp_subtree_classes: list[str] = []
        self._rsp_subtree_filter: FilterExpr | None = None
        self._rsp_subtree_include: list[str] = []
        self._rsp_prop_include: Literal["all", "naming-only", "config-only"] = "all"
        self._order_by: list[str] = []
        self._page_size: int = 500
        self._limit: int | None = None

    def _copy(self) -> Self:
        """Return a shallow copy with mutable list fields independently copied."""
        other = object.__new__(type(self))
        other.__dict__.update(self.__dict__)
        other._also_classes = list(self._also_classes)
        other._rsp_subtree_classes = list(self._rsp_subtree_classes)
        other._rsp_subtree_include = list(self._rsp_subtree_include)
        other._order_by = list(self._order_by)
        return other

    # ── Scope ─────────────────────────────────────────────────────────────────

    def under(self, dn: str) -> Self:
        """Scope the query to the subtree rooted at *dn*.

        Converts a global class query into a DN-scoped subtree query.
        When called on an already-scoped query the scope DN is replaced.

        Args:
            dn: APIC Distinguished Name (e.g. ``"uni/tn-prod"``).

        Returns:
            New query with ``scope_dn`` set.

        Example::

            # Global → scoped
            aci.query(fvBD).under("uni/tn-prod").fetch()
            # GET /api/mo/uni/tn-prod.json?query-target=subtree&target-subtree-class=fvBD
        """
        q = self._copy()
        q._scope_dn = dn
        return q

    def children(self) -> Self:
        """Limit to direct children of the scope DN (one level deep).

        Sets ``query-target=children``.  Only meaningful when a scope DN is set
        via :meth:`under` or jargon navigation.

        Returns:
            New query with ``query-target=children``.
        """
        q = self._copy()
        q._query_target = "children"
        return q

    def subtree(self) -> Self:
        """Include all descendants of the scope DN (unlimited depth).

        Sets ``query-target=subtree``.  This is the default when a scope DN is
        set — call this explicitly only after a preceding :meth:`children` call.

        Returns:
            New query with ``query-target=subtree``.
        """
        q = self._copy()
        q._query_target = "subtree"
        return q

    def self_only(self) -> Self:
        """Return only the scoped object itself (``query-target=self``).

        Only meaningful with a scope DN (:meth:`under` or jargon navigation):
        the MO at that DN is returned with no descendants.  A no-op on a global
        class query, which already addresses a single class.

        Returns:
            New query with ``query-target=self``.
        """
        q = self._copy()
        q._query_target = "self"
        return q

    def also(self, *classes: type[ManagedObject] | str) -> Self:
        """Return additional ACI classes alongside the queried one (scoped only).

        Adds to ``target-subtree-class`` so a DN-scoped subtree/children query
        returns several types at once (e.g. BDs *and* their subnets) in a single
        request.  Results are polymorphic — each object deserialises to its own
        type.  Only affects a scoped query (:meth:`under`); it is ignored on a
        global class query, which addresses a single class by URL.

        Args:
            *classes: Extra ACI class type(s) or string class name(s) to return.

        Returns:
            New query with the extra target classes registered.

        Example::

            # Every BD and every subnet under the tenant, in one request
            objs = aci.query(fvBD).under("uni/tn-prod").also(fvSubnet).fetch()
        """
        q = self._copy()
        for cls in classes:
            name = cls if isinstance(cls, str) else cls.__name__
            if name != q._aci_class and name not in q._also_classes:
                q._also_classes.append(name)
        return q

    # ── Filters ───────────────────────────────────────────────────────────────

    def where(self, *exprs: FilterExpr, **kwargs: FilterValue) -> Self:
        """Add a filter to the query.

        Accepts explicit :class:`~niwaki.query.FilterExpr` objects (built with
        :func:`~niwaki.query.eq`, :func:`~niwaki.query.wcard`, …) **and** a
        keyword-argument shorthand where each ``prop=value`` pair becomes an
        equality check auto-prefixed with the queried class name.

        Multiple arguments — whether positional or keyword — are combined with
        a logical AND.  Calling ``.where()`` multiple times chains the filters
        with AND.

        The keyword form is the quickest path for simple equality checks: no
        import of operator functions required, and no class name to remember —
        the property is qualified with the queried class automatically.
        Explicit :class:`~niwaki.query.FilterExpr` objects are passed through
        verbatim: qualify their properties (``"fvBD.name"``) or build them
        with ``cls_name=``.

        Args:
            *exprs:   One or more :class:`~niwaki.query.FilterExpr` objects,
                      with class-qualified property names.
            **kwargs: Equality shortcuts — ``name="web"`` becomes
                      ``eq(ClassName.name,"web")`` automatically.

        Returns:
            New query with the filter applied.

        Example::

            from niwaki.query import wcard, and_

            # Keyword shorthand — simplest, auto-qualified
            aci.root.tenant("prod").bd().where(name="web").fetch()

            # Explicit expression — qualified property
            aci.query(fvBD).where(wcard("fvBD.name", "prod-*")).fetch()

            # Chained (implicit AND)
            aci.query(fvBD).where(wcard("fvBD.name", "prod-*")).where(arpFlood=True).fetch()

            # Combined in one call
            aci.query(fvBD).where(
                wcard("fvBD.name", "prod-*"),
                arpFlood=True,
            ).fetch()
        """
        all_exprs: list[FilterExpr] = list(exprs)
        for prop, value in kwargs.items():
            all_exprs.append(_kwarg_to_expr(prop, value, self._aci_class))

        if not all_exprs:
            return self

        combined = all_exprs[0] if len(all_exprs) == 1 else and_(*all_exprs)

        q = self._copy()
        q._filter_expr = and_(q._filter_expr, combined) if q._filter_expr is not None else combined
        return q

    # ── Response enrichment ───────────────────────────────────────────────────

    def include(self, *classes: type[ManagedObject] | str) -> Self:
        """Include children of the given ACI class(es) in each response object.

        Sets ``rsp-subtree=children`` and ``rsp-subtree-class`` on the APIC
        request.  The matching child objects are accessible via the ``.children``
        attribute of each returned instance.

        Args:
            *classes: ACI class type(s) or string class name(s) to include.

        Returns:
            New query with child inclusion set.

        Example::

            # Fetch BDs with their subnets embedded
            bds = aci.query(fvBD).include(fvSubnet).fetch()
            for bd in bds:
                for subnet in bd.children:
                    print(subnet.model_extra.get("ip"))
        """
        q = self._copy()
        q._rsp_subtree = "children"
        for cls in classes:
            name = cls if isinstance(cls, str) else cls.__name__
            if name not in q._rsp_subtree_classes:
                q._rsp_subtree_classes.append(name)
        return q

    def subtree_full(self) -> Self:
        """Embed the entire subtree of each object (``rsp-subtree=full``).

        Unlike :meth:`include`, which embeds only the named direct children,
        this returns every descendant at unlimited depth, reachable through the
        ``.children`` tree of each result.

        Returns:
            New query with ``rsp-subtree=full``.
        """
        q = self._copy()
        q._rsp_subtree = "full"
        return q

    def _subtree_filter_class(self) -> str:
        """The class to qualify a :meth:`subtree_where` keyword against.

        A ``subtree_where(prop=value)`` keyword filters the *embedded* children —
        the class(es) named by :meth:`include` — not the top-level query class.
        Qualifying with the query class produces a filter the APIC **rejects**
        (verified live: ``eq(fvBD.scope,…)`` on ``fvSubnet`` children → HTTP 301)
        or silently mis-matches.  So the keyword form needs exactly one included
        class; anything else must be an explicitly-qualified expression.

        Raises:
            ValueError: No included class is set, or more than one is.
        """
        classes = self._rsp_subtree_classes
        if len(classes) == 1:
            return classes[0]
        detail = (
            "no include() class is set" if not classes else f"several are ({', '.join(classes)})"
        )
        raise ValueError(
            "subtree_where(prop=value) qualifies the property with the included "
            f"subtree class, but {detail} — call include(OneClass) first, or pass an "
            'explicitly-qualified expression, e.g. subtree_where(eq("fvSubnet.ip", "10.*"))'
        )

    def subtree_where(self, *exprs: FilterExpr, **kwargs: FilterValue) -> Self:
        """Filter the *included subtree* children by an attribute expression.

        Sets ``rsp-subtree-filter`` on the APIC request.  This is a
        *response-level* filter that restricts which child objects are embedded
        in the response — unlike :meth:`where` which filters the top-level
        objects.  Requires :meth:`include` to be called first (or
        :meth:`with_faults` / :meth:`with_health`) so that ``rsp-subtree`` is
        not ``"no"``.

        Accepts the same expression DSL as :meth:`where`: explicit
        :class:`~niwaki.query.FilterExpr` objects **and** keyword shortcuts
        ``prop=value`` auto-prefixed with the queried class name.

        Args:
            *exprs:   One or more :class:`~niwaki.query.FilterExpr` objects.
            **kwargs: Equality shortcuts applied to the query's class, e.g.
                      ``ip="10.0.0.1/24"`` becomes ``eq(fvSubnet.ip,"10.0.0.1/24")``.

        Returns:
            New query with ``rsp-subtree-filter`` set.

        Raises:
            ValueError: Called with no arguments.

        Example::

            # Fetch BDs, but embed only subnets in the 10.0.0.0/8 scope
            from niwaki.query import wcard
            bds = (
                aci.query(fvBD)
                .include(fvSubnet)
                .subtree_where(wcard("fvSubnet.ip", "10.*"))
                .fetch()
            )

            # Keyword form
            bds = aci.query(fvBD).include(fvSubnet).subtree_where(ip="10.0.0.1/24").fetch()
        """
        all_exprs: list[FilterExpr] = list(exprs)
        if kwargs:
            subtree_class = self._subtree_filter_class()
            for prop, value in kwargs.items():
                all_exprs.append(_kwarg_to_expr(prop, value, subtree_class))

        if not all_exprs:
            raise ValueError("subtree_where() requires at least one filter expression.")

        combined = all_exprs[0] if len(all_exprs) == 1 else and_(*all_exprs)

        q = self._copy()
        q._rsp_subtree_filter = (
            and_(q._rsp_subtree_filter, combined) if q._rsp_subtree_filter is not None else combined
        )
        return q

    def _add_subtree_include(self, *values: str) -> Self:
        """Append ``rsp-subtree-include`` facets, de-duplicated (shared helper)."""
        q = self._copy()
        for value in values:
            if value not in q._rsp_subtree_include:
                q._rsp_subtree_include.append(value)
        return q

    def include_subtree(self, *kinds: SubtreeInclude) -> Self:
        """Embed one or more response-subtree facets (``rsp-subtree-include``).

        The typed, exhaustive entry point: every facet the APIC offers is a
        member of :class:`SubtreeInclude` (faults, health, stats, relations,
        count, the audit/event/fault/health record streams, tasks, …).  The
        ``with_*`` methods are ergonomic shortcuts for the common ones.

        Args:
            *kinds: One or more :class:`SubtreeInclude` facets.

        Returns:
            New query with the facets added.

        Example::

            from niwaki.query import SubtreeInclude

            aci.query(fvBD).include_subtree(
                SubtreeInclude.FAULT_RECORDS, SubtreeInclude.AUDIT_LOGS
            ).fetch()
        """
        return self._add_subtree_include(*(str(kind) for kind in kinds))

    def with_faults(self) -> Self:
        """Embed fault objects in the subtree response (``rsp-subtree-include=faults``).

        By default this embeds faults on *every* returned object, faulted or
        not.  Chain :meth:`only_faulted` to restrict the result to objects that
        actually carry a fault.

        Returns:
            New query with faults embedded.

        Example::

            # Every BD, faults embedded where present
            aci.root.tenant("prod").bd().with_faults().fetch()
            # Only the faulted BDs
            aci.root.tenant("prod").bd().with_faults().only_faulted().fetch()
        """
        return self._add_subtree_include("faults")

    def only_faulted(self) -> Self:
        """Restrict results to objects that carry the embedded subtree items.

        Adds the ``required`` modifier to ``rsp-subtree-include``: only
        top-level objects that actually have the requested facet — typically the
        faults embedded by :meth:`with_faults` — are returned.

        Returns:
            New query with ``required`` set.
        """
        return self._add_subtree_include("required")

    def with_health(self) -> Self:
        """Embed health score data in the subtree response (``rsp-subtree-include=health``).

        Returns:
            New query with health data embedded.
        """
        return self._add_subtree_include("health")

    def with_stats(self) -> Self:
        """Embed statistics counters in the subtree response (``rsp-subtree-include=stats``).

        Returns:
            New query with stats data embedded.
        """
        return self._add_subtree_include("stats")

    def with_relations(self) -> Self:
        """Embed relation objects (Rs/Rt) — ``rsp-subtree-include=relations``.

        Returns:
            New query with relations embedded.
        """
        return self._add_subtree_include("relations")

    def config_only(self) -> Self:
        """Return only configurable properties, omitting read-only APIC metadata.

        Sets ``rsp-prop-include=config-only``.  Reduces payload size for
        write-oriented inventory queries.

        Returns:
            New query with ``rsp-prop-include=config-only``.
        """
        q = self._copy()
        q._rsp_prop_include = "config-only"
        return q

    def naming_only(self) -> Self:
        """Return only naming properties (name/DN).

        Sets ``rsp-prop-include=naming-only``.  Ideal for large-scale inventory
        queries where only identifiers are needed and payload size matters.

        Returns:
            New query with ``rsp-prop-include=naming-only``.
        """
        q = self._copy()
        q._rsp_prop_include = "naming-only"
        return q

    # ── Sort ──────────────────────────────────────────────────────────────────

    def order_by(self, prop: str, *, desc: bool = False) -> Self:
        """Sort results by a property (chain for multi-key ordering).

        The property name is auto-prefixed with the ACI class name when it does
        not already contain a dot.  Calling :meth:`order_by` more than once
        appends additional sort keys, applied left to right.

        Args:
            prop: Property name (e.g. ``"name"`` → qualified as
                  ``"fvBD.name|asc"``).
            desc: Sort descending when ``True``.  Default: ascending.

        Returns:
            New query with the ordering key appended.

        Example::

            aci.query(fvBD).order_by("name").fetch()
            aci.query(fvBD).order_by("name", desc=True).fetch()
            # Multi-key: most severe first, then by code
            aci.query("faultInst").order_by("severity", desc=True).order_by("code").fetch()
        """
        q = self._copy()
        direction = "desc" if desc else "asc"
        q._order_by.append(f"{_qualify(prop, self._aci_class)}|{direction}")
        return q

    def page_size(self, n: int) -> Self:
        """Override the page size for auto-pagination.

        The default page size is 500 objects per page.  Decrease for slow
        connections or very large objects; increase if the APIC supports it.

        Args:
            n: Objects per page.  Must be greater than zero.

        Returns:
            New query with the page size set.

        Raises:
            ValueError: *n* is zero or negative.
        """
        if n <= 0:
            raise ValueError(f"page_size must be > 0, got {n!r}")
        q = self._copy()
        q._page_size = n
        return q

    # ── Limit / slicing ─────────────────────────────────────────────────────────

    def _effective_page_size(self) -> int:
        """The page size to request, capped to the result limit when one is set.

        With a small limit (``q[:5]``) there is no point fetching pages of 500 —
        capping the page size makes the limit **server-side**: the first page
        already carries at most *limit* objects.
        """
        if self._limit is None:
            return self._page_size
        return max(1, min(self._page_size, self._limit))

    def __getitem__(self, index: slice) -> Self:
        """Cap the number of results with a leading slice — ``q[:50]``.

        Returns a **new lazy query** that yields at most *stop* objects; nothing
        executes until the query is iterated or an executor is called.  Only a
        leading ``[:n]`` is supported: the APIC has no result offset, so a
        non-zero start, a step, or a negative bound is rejected, and an integer
        index (``q[0]``) is deliberately unsupported — use :meth:`first`.

        Args:
            index: A ``[:n]`` slice with a non-negative ``stop``.  ``q[:]`` is an
                unlimited copy.

        Returns:
            New query limited to at most *n* results.

        Raises:
            TypeError: *index* is not a slice (e.g. an integer index).
            ValueError: The slice has a step, a non-zero start, or a negative or
                non-integer stop.

        Example::

            first_fifty = aci.query("fvCEp")[:50]
            for endpoint in aci.query("fvCEp").where(ip="10.0.0.5")[:10]:
                ...
        """
        if not isinstance(index, slice):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                "a query is sliced, not indexed — use q[:n] to limit the result, "
                f"or .first() for a single object, not q[{index!r}]"
            )
        if index.step is not None:
            raise ValueError("query slicing does not support a step (q[:n] only)")
        if index.start not in (None, 0):
            raise ValueError(
                "query slicing supports only a leading q[:n] — the APIC has no result offset"
            )
        stop = index.stop
        if stop is None:
            return self._copy()
        if not isinstance(stop, int) or isinstance(stop, bool) or stop < 0:
            raise ValueError(f"query slice stop must be a non-negative integer, got {stop!r}")
        q = self._copy()
        q._limit = stop
        return q

    def __bool__(self) -> bool:
        """Guard ``if query:`` — a query object is not a result check.

        A builder is always truthy on its own, so ``if aci.query(...)...:`` would
        pass whether or not anything matches — a classic footgun.  Fail loud:
        use :meth:`~niwaki.query.Query.exists` to test for matches, or ``.count()``.

        Raises:
            TypeError: Always — a query has no boolean meaning.
        """
        raise TypeError(
            "a query has no boolean value — use .exists() to test for matches, "
            "or .count() for how many"
        )

    # ── URL building ──────────────────────────────────────────────────────────

    def build(self) -> tuple[str, dict[str, str]]:
        """Translate accumulated builder state into an APIC path and param dict.

        Returns the APIC-relative path and a flat query-string dict that together
        represent this query.  Useful for inspection, debugging, and testing
        without executing an HTTP request.

        Returns:
            ``(path, params)`` tuple where *path* is relative to the APIC base
            URL (e.g. ``"/api/class/fvBD.json"``) and *params* maps APIC
            parameter names to string values.

        Example::

            path, params = aci.query(fvBD).where(arpFlood=True).build()
            # path  → "/api/class/fvBD.json"
            # params → {"query-target-filter": 'eq(fvBD.arpFlood,"yes")'}
        """
        params: dict[str, str] = {}

        if self._also_classes and not (
            self._scope_dn and self._query_target in ("subtree", "children")
        ):
            raise ValueError(
                "also() adds classes to a DN-scoped subtree/children query's "
                "target-subtree-class; it has no effect on a global class query or a "
                "self_only() query — scope with under(dn) first, without self_only()"
            )

        if self._scope_dn:
            path = f"/api/mo/{self._scope_dn}.json"
            params["query-target"] = self._query_target
            if self._query_target in ("subtree", "children"):
                # Restrict to this query's class(es) so that a subtree-scoped
                # query on a parent DN returns only the requested type(s), not
                # everything under the parent.  ``also()`` adds extra classes.
                params["target-subtree-class"] = ",".join([self._aci_class, *self._also_classes])
        else:
            path = f"/api/class/{self._aci_class}.json"

        if self._filter_expr is not None:
            params["query-target-filter"] = str(self._filter_expr)
        if self._rsp_subtree != "no":
            params["rsp-subtree"] = self._rsp_subtree
        if self._rsp_subtree_classes:
            params["rsp-subtree-class"] = ",".join(self._rsp_subtree_classes)
        if self._rsp_subtree_filter is not None:
            params["rsp-subtree-filter"] = str(self._rsp_subtree_filter)
        if self._rsp_subtree_include:
            params["rsp-subtree-include"] = ",".join(self._rsp_subtree_include)
        if self._rsp_prop_include != "all":
            params["rsp-prop-include"] = self._rsp_prop_include
        if self._order_by:
            params["order-by"] = ",".join(self._order_by)

        return path, params
