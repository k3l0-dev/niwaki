"""Shared query builder state and accumulator methods.

:class:`_QueryBase` holds all mutable state and exposes the full chainable API.
The sync :class:`~niwaki.query.Query` and async :class:`~niwaki.query.AsyncQuery`
subclasses inherit everything here and only add execution methods (``fetch``,
``first``, ``count``, ``stream``).

This split avoids code duplication while keeping execution-layer concerns
(sync vs async) cleanly separated.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from niwaki.models.base import ManagedObject
from niwaki.query._filters import (  # pyright: ignore[reportPrivateUsage]
    FilterExpr,
    _coerce_value,
    _qualify,
    and_,
)


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
        self._filter_expr: FilterExpr | None = None
        self._rsp_subtree: Literal["no", "children", "full"] = "no"
        self._rsp_subtree_classes: list[str] = []
        self._rsp_subtree_filter: FilterExpr | None = None
        self._rsp_subtree_include: list[str] = []
        self._rsp_prop_include: Literal["all", "naming-only", "config-only"] = "all"
        self._order_by_prop: str | None = None
        self._order_by_dir: Literal["asc", "desc"] = "asc"
        self._page_size: int = 500

    def _copy(self) -> Self:
        """Return a shallow copy with mutable list fields independently copied."""
        other = object.__new__(type(self))
        other.__dict__.update(self.__dict__)
        other._rsp_subtree_classes = list(self._rsp_subtree_classes)
        other._rsp_subtree_include = list(self._rsp_subtree_include)
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

    # ── Filters ───────────────────────────────────────────────────────────────

    def where(self, *exprs: FilterExpr, **kwargs: Any) -> Self:
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
            qprop = _qualify(prop, self._aci_class)
            all_exprs.append(FilterExpr(f'eq({qprop},"{_coerce_value(value)}")'))

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

    def subtree_where(self, *exprs: FilterExpr, **kwargs: Any) -> Self:
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
        for prop, value in kwargs.items():
            qprop = _qualify(prop, self._aci_class)
            all_exprs.append(FilterExpr(f'eq({qprop},"{_coerce_value(value)}")'))

        if not all_exprs:
            raise ValueError("subtree_where() requires at least one filter expression.")

        combined = all_exprs[0] if len(all_exprs) == 1 else and_(*all_exprs)

        q = self._copy()
        q._rsp_subtree_filter = (
            and_(q._rsp_subtree_filter, combined) if q._rsp_subtree_filter is not None else combined
        )
        return q

    def with_faults(self) -> Self:
        """Include fault objects in the subtree response.

        Sets ``rsp-subtree-include=faults,required``.  Only objects that
        *have* faults are returned (``required`` option).

        Returns:
            New query with faults included.

        Example::

            faulted = aci.root.tenant("prod").bd().with_faults().fetch()
        """
        q = self._copy()
        for opt in ("faults", "required"):
            if opt not in q._rsp_subtree_include:
                q._rsp_subtree_include.append(opt)
        return q

    def with_health(self) -> Self:
        """Include health score data in the subtree response.

        Sets ``rsp-subtree-include=health``.

        Returns:
            New query with health data included.
        """
        q = self._copy()
        if "health" not in q._rsp_subtree_include:
            q._rsp_subtree_include.append("health")
        return q

    def with_stats(self) -> Self:
        """Include statistics counters in the subtree response.

        Sets ``rsp-subtree-include=stats``.

        Returns:
            New query with stats data included.
        """
        q = self._copy()
        if "stats" not in q._rsp_subtree_include:
            q._rsp_subtree_include.append("stats")
        return q

    def with_relations(self) -> Self:
        """Include relation objects (Rs/Rt) in the subtree response.

        Sets ``rsp-subtree-include=relations``.

        Returns:
            New query with relations included.
        """
        q = self._copy()
        if "relations" not in q._rsp_subtree_include:
            q._rsp_subtree_include.append("relations")
        return q

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
        """Sort results by a property.

        The property name is auto-prefixed with the ACI class name when it does
        not already contain a dot.

        Args:
            prop: Property name (e.g. ``"name"`` → qualified as
                  ``"fvBD.name|asc"``).
            desc: Sort descending when ``True``.  Default: ascending.

        Returns:
            New query with the ordering set.

        Example::

            aci.query(fvBD).order_by("name").fetch()
            aci.query(fvBD).order_by("name", desc=True).fetch()
        """
        q = self._copy()
        q._order_by_prop = _qualify(prop, self._aci_class)
        q._order_by_dir = "desc" if desc else "asc"
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

        if self._scope_dn:
            path = f"/api/mo/{self._scope_dn}.json"
            params["query-target"] = self._query_target
            if self._query_target in ("subtree", "children"):
                # Restrict to this query's class so that a subtree-scoped
                # query on a parent DN returns only the requested type, not
                # everything under the parent.
                params["target-subtree-class"] = self._aci_class
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
        if self._order_by_prop:
            params["order-by"] = f"{self._order_by_prop}|{self._order_by_dir}"

        return path, params
