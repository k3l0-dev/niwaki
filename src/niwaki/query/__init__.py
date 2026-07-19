"""Niwaki query builder — full APIC REST query model as a fluent API.

This module provides two things:

1. **Filter expression DSL** — operator functions that produce typed
   :class:`FilterExpr` objects serialisable to APIC filter strings.

2. **Query builders** — :class:`Query` (sync) and :class:`AsyncQuery` (async)
   that accumulate query parameters lazily and execute on demand.

Quick-start
-----------

**Reading objects without knowing class names** (via jargon navigation)::

    with Niwaki(...) as aci:
        # All BDs in tenant "prod" — no class import needed
        bds = aci.root.tenant("prod").bd().fetch()

        # Filter with keyword shorthand
        bds = aci.root.tenant("prod").bd().where(arpFlood=True).fetch()

        # Subtree included in response
        bds = aci.query(fvBD).include(fvSubnet).fetch()

**Filter DSL**::

    from niwaki.query import eq, ne, wcard, and_, or_, not_

    # Keyword shorthand (simplest — no import needed)
    query.where(name="web")

    # Explicit operators
    query.where(wcard("name", "prod-*"))
    query.where(and_(wcard("name", "prod-*"), eq("arpFlood", True)))
    query.where(~eq("name", "infra"))  # NOT

**Scoping**::

    # Global class query (entire fabric)
    aci.query(fvBD).fetch()

    # Scoped to a DN
    aci.query(fvBD).under("uni/tn-prod").fetch()

    # Via jargon navigation (DN inferred automatically)
    aci.root.tenant("prod").bd().fetch()

**Unregistered classes (read-only, operational, stats…)**::

    # 15 000+ APIC classes accessible by string name
    nodes = aci.query("topSystem").naming_only().fetch()
    # Uniform read access, generated class or not: .dn and obj["wireName"]
    for node in nodes:
        print(node.dn, node["role"])

**Response enrichment**::

    aci.root.tenant("prod").bd().with_faults().fetch()  # faults embedded on each BD
    aci.query(fvBD).with_health().fetch()               # include health
    aci.query(fvBD).include(fvSubnet).fetch()           # embed children

**Execution methods**::

    query.fetch()          # list[T] — all pages, in memory
    query.first()          # T | None — page-size=1 optimisation
    query.count()          # int — one lightweight request, no objects transferred
    query.stream()         # Iterator[T] / AsyncIterator[T] — page-by-page

Public API
----------
"""

from niwaki.query._async_builder import AsyncQuery
from niwaki.query._base import SubtreeInclude  # pyright: ignore[reportPrivateUsage]
from niwaki.query._builder import Query
from niwaki.query._filters import (
    FilterExpr,
    FilterValue,
    allbit,
    and_,
    any_of,
    anybit,
    between,
    bw,
    eq,
    ge,
    gt,
    le,
    like,
    lt,
    ne,
    not_,
    or_,
    raw,
    wcard,
    xor,
)

__all__ = [
    "AsyncQuery",
    "FilterExpr",
    "FilterValue",
    "Query",
    "SubtreeInclude",
    "allbit",
    "and_",
    "any_of",
    "anybit",
    "between",
    "bw",
    "eq",
    "ge",
    "gt",
    "le",
    "like",
    "lt",
    "ne",
    "not_",
    "or_",
    "raw",
    "wcard",
    "xor",
]
