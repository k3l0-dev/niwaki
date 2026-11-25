# Observing the fabric

The facade is the SDK's read side: navigation in operator vocabulary, typed
reads, the query builder, and deletion.  It never configures — writing is the
design DSL's job.

The examples below observe a small tenant pushed by its design:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("prod")
config.bd("web", unicast_routing=True).bind(vrf="main")
config.vrf("main")

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
config.push(aci)
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`connection`.

## Vocabulary navigation

Every node is a DN-scoped handle.  Navigate with the curated design
vocabulary — the same names the design DSL uses (for uncurated classes the
name derives from the schema label, or the pkg-prefixed class name when the
label is not identifier-shaped); the DN is computed for you:

```python
bd = aci.tenant("prod").bd("web")  # NiwakiNode at uni/tn-prod/BD-web
assert bd.dn == "uni/tn-prod/BD-web"
mo = bd.read()  # typed fvBD instance
assert mo.unicast_routing is True  # human-readable field names
```

Navigation names come from the same curated vocabulary as the design DSL, so
what you type to describe a bridge domain is what you type to read one.  When
a navigation name has to change, the old one keeps resolving with a
`DeprecationWarning` naming its replacement for at least one minor release —
see {doc}`../project/versioning` and the
{doc}`deprecated navigation names <../reference/vocabulary/deprecated-navigation>`
reference page.

`aci.node(dn)` reaches any explicit DN.  Its class is resolved from the DN
itself — walked from `uni`, locally, with no request — so the node offers that
class's vocabulary, and only it; `aci.root` (DN `uni`) is the `polUni` node
whose vocabulary is the top level:

```python
from niwaki.models.fv.fvTenant import fvTenant

prod = aci.node("uni/tn-prod")  # resolved: an fvTenant node
assert prod.cls is fvTenant
assert prod.bd("web").dn == "uni/tn-prod/BD-web"
assert aci.node("uni/tn-prod/BD-web").read().unicast_routing is True
assert "bd" in dir(prod)  # IPython/Jupyter complete the valid names
```

Some DNs yield a node without a model class.  It still reads, queries and
deletes, but offers no navigation, and naming a vocabulary entry on it raises
`AttributeError` that names the DN and says which case applies:

- **ambiguous** — a segment several classes fit (`uni/infra/nodecfgcont/node-101`
  is both an `infraNodeCfg` and a `fabricNodeCfg` by the catalogue's DN
  grammar): the error names every candidate, and you state the one you mean;
- **unresolvable** — a DN outside `uni`, or a segment no class fits;
- **no generated model** — the DN resolves to a class the SDK only knows from
  its catalogue (`faultInst`, `aaaAppUser`), which the error names.

Stating the class, `aci.node(dn, Class)`, keeps it, after checking it against
the DN: a class the DN contradicts raises `ValueError`.  `.mo(Class, **naming)`
descends one level for classes outside the curated vocabulary.

```python
node = aci.node("uni/infra/nodecfgcont/node-101")  # ambiguous: no navigation
try:
    node.tenant
except AttributeError as error:
    assert "ambiguous" in str(error) and "infraNodeCfg" in str(error)
```

### Typed navigation

Navigation is fully typed for mypy and pyright: your editor completes the
names valid under each node, and every step has a precise type.  A name with
an argument is a node; the same call with no argument is a query over that
class; `read()` returns the concrete model:

```python
from niwaki.models.fv.fvBD import fvBD

bd = aci.tenant("prod").bd("web")  # checker: fvBDNode
web = bd.read()  # checker: fvBD — fields complete, typos are errors
assert isinstance(web, fvBD)
subnets = bd.subnet()  # checker: Query[fvSubnet]
```

A child with a fixed RN — one that exists at most once under its parent, such
as a relation (`vrf_binding`, `custom_qos_policy`) or a singleton policy
(`pim`, `infra`) — has nothing to name: its call takes no argument and is
always a node.  Passing a name anyway raises `TypeError` before any request
(and is a type error), rather than silently pointing at the one child whatever
the name — a `delete()` on it would otherwise remove a relation you did not
mean.  To see which object a relation targets, read it:

```python
import pytest

link = bd.vrf_binding()  # the BD's VRF relation: a node, not a query
assert link.dn == "uni/tn-prod/BD-web/rsctx"

with pytest.raises(TypeError, match="takes no name"):
    bd.vrf_binding("prod")  # the RN is fixed: "prod" would be ignored
```

Naming arguments are checked too.  Multi-prop children take their naming
props as keywords, under their Python names (`subnet(subnet="10.0.0.1/24")`,
`static_path(target_dn=...)`); the APIC attribute name is accepted at runtime
but is a type error.  A name that is not a navigation edge is a type error as
well, on the client and on every node:

```text
aci.tenantt("prod")                  # mypy: "Niwaki" has no attribute "tenantt"
aci.tenant("prod").bdd("web")        # mypy: "NotANavigationName" not callable
bd.subnet(ip="10.0.0.1/24")          # mypy: Unexpected keyword argument "ip"
```

Only naming properties go into the call — on a name edge, a keyword edge and
`.mo(Class, **naming)` alike.  Any other keyword is refused with `TypeError`
before any request, and the message names the class, every offending key and
the naming properties it does take, in the words of the call you made
(`subnet() takes only…`, `mo(fvSubnet) takes only…`).  So is a naming property
given by both of its spellings, `subnet=` and `ip=` — one of the two would be
dropped.  A node is addressed by its DN alone, so
such a keyword (a configurable property like `scope`, a typo, a name given to
a class with a fixed RN) could only be dropped — and a `delete()` on the node
would remove an object you did not describe.  Configuration goes through the
design DSL:

```python
from niwaki.models.fv.fvRsCtx import fvRsCtx
from niwaki.models.fv.fvSubnet import fvSubnet

with pytest.raises(TypeError, match="scope"):
    bd.subnet(subnet="10.0.0.1/24", scope="public")  # scope is configuration, not a name
with pytest.raises(TypeError, match="takes no name"):
    bd.mo(fvRsCtx, name="prod")  # the VRF relation has a fixed RN
with pytest.raises(TypeError, match="twice"):
    bd.subnet(subnet="10.0.0.1/24", ip="10.0.0.1/24")  # one spelling per property
with pytest.raises(TypeError, match="arp_flooding"):
    aci.tenant("prod").bd("web", arp_flooding=True)  # a name edge takes the name only

assert bd.mo(fvSubnet, subnet="10.0.0.1/24").dn == "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]"
```

The two explicit-class entry points are typed the same way: `aci.node(dn, Class)`
and `.mo(Class, **naming)` return the very node type vocabulary navigation
returns for `Class` (`aci.node(dn, fvBD)` is an `fvBDNode`, `aci.node("uni",
polUni)` the root node), so their names are checked and complete too.  Only
`aci.node(dn)` without a class stays dynamic: the class is known at runtime
only, so the checker sees a `NiwakiNode[ManagedObject]` and accepts any further
name, resolved at runtime.

```python
from niwaki.models.fv.fvBD import fvBD

web = aci.node("uni/tn-prod/BD-web", fvBD)  # checker: fvBDNode
assert web.read().unicast_routing is True  # checker: fvBD
```

The types exist for the checker only: at runtime, navigation resolves exactly
as before and every node is a plain {class}`~niwaki.NiwakiNode`.

Type-checking a project that uses niwaki with mypy costs about four extra
seconds on a cold run, because mypy analyses the model classes the navigation
types refer to; the result is cached, so later runs pay nothing.  Parallel checking
(`mypy -n auto`) shortens the cold run; pyright adds well under a second.

## The query builder

{meth}`~niwaki.Niwaki.query` targets a class fabric-wide; `node.query(...)`
scopes it to a subtree.  Accumulate, then execute:

```python
from niwaki.models.fv.fvBD import fvBD

# Filters, scoping, counting — keyword filters take the readable field
# name (arp_flooding) or the APIC wire name (arpFlood), interchangeably
bds = aci.query(fvBD).where(arp_flooding=True).under("uni/tn-prod").fetch()
n = aci.tenant("prod").query(fvBD).count()

# Filter expressions when kwargs are not enough — qualify the
# property (or build with cls_name="fvAEPg")
from niwaki.query import gt, or_, eq

epgs = aci.query("fvAEPg").where(or_(eq("fvAEPg.name", "web"), gt("fvAEPg.pcTag", "10000"))).fetch()

# Enrichment — health and faults embedded on every returned object
# (chain .only_faulted() to keep only the objects that carry a fault)
enriched = aci.query(fvBD).with_health().with_faults().fetch()

# Streaming for large result sets
for bd in aci.query(fvBD).stream():
    ...
```

### Iterate, slice, and fetch exactly one

A query is a lazy iterable — loop it, `list()` it, or take a leading `[:n]`
slice without an explicit `.fetch()`.  Use `.one()` when exactly one object is
expected (a typed `NoResultError` / `MultipleResultsError` otherwise), and
`.exists()` for a cheap presence check:

```python
# Iterate lazily — every result reads the same, generated class or not:
# .dn and obj["wireName"] work uniformly across all ~15,000 APIC classes.
for bd in aci.query(fvBD):
    print(bd.dn, bd["name"])

# A leading slice caps the result (server-side page size), lazily:
first_page = list(aci.query(fvBD)[:50])

# Exactly one object, or a typed error:
web = aci.query(fvBD).where(name="web").one()
assert web["name"] == "web"

# Cheap existence check:
assert aci.query(fvBD).where(name="web").exists()
```

### Smart keyword filters

In `where(prop=value)`, the value's *type* chooses the APIC operator, and
`prop` is either the APIC wire name or the readable field name of the queried
class (for `subtree_where`, of the included class).  Only the name is
translated — the value renders exactly as it would under the wire name.  A
name that is the wire name of one property *and* the readable name of another
(`version` on `firmwareFwP`) raises `ValueError`; use a qualified expression
such as `eq("firmwareFwP.version", ...)` instead.  Inspect the compiled filter
with `.build()` without touching the network:

```python
from niwaki.query import any_of, anybit

# list -> membership OR
_, params = aci.query(fvBD).where(name=["web", "db"]).build()
assert params["query-target-filter"] == 'or(eq(fvBD.name,"web"),eq(fvBD.name,"db"))'

# a "*" makes a wildcard
_, params = aci.query(fvBD).where(name="prod-*").build()
assert params["query-target-filter"] == 'wcard(fvBD.name,"prod-*")'

# a set stays a bitmask equality (Flags fields), never a membership OR
_, params = aci.query("fvSubnet").where(scope={"public", "shared"}).build()
assert params["query-target-filter"] == 'eq(fvSubnet.scope,"public,shared")'

# readable and wire names compile to the same filter
_, readable = aci.query(fvBD).where(arp_flooding=True).build()
_, wire = aci.query(fvBD).where(arpFlood=True).build()
assert readable == wire
assert readable["query-target-filter"] == 'eq(fvBD.arpFlood,"yes")'

# explicit helpers: any_of(...), and anybit(...) to match a single bit of a mask
faults = aci.query("faultInst").where(code=any_of("F0467", "F1394")).fetch()
open_subnets = aci.query("fvSubnet").where(anybit("fvSubnet.scope", "shared")).fetch()
```

Any of the ~15,000 APIC classes is queryable **by name** — including
read-only operational classes outside the generated set:

```python
nodes = aci.query("topSystem").naming_only().fetch()
```

`build()` returns the URL and parameters without executing — the read-side
mirror of `to_payload()`.

## Deleting

Deletion is the **one imperative operation** on the facade — a design never
removes what it does not declare, so removals are always an explicit act:

```python
retired = tenant("prod-old")
retired.push(aci)  # a tenant to retire

aci.tenant("prod-old").delete()  # removes the object AND its subtree
```

Deleting a DN removes the whole subtree beneath it, exactly as in the GUI.
A `delete()` on a DN that does not exist raises
{class}`~niwaki.exceptions.NotFoundError` — deletion is never silently a
no-op:

```python
import pytest

from niwaki.exceptions import NotFoundError

with pytest.raises(NotFoundError):
    aci.tenant("prod-old").read()  # gone, subtree included
```

Day-2 removal of a single child (a subnet, a static path) is the same
gesture one level deeper: navigate to it, `delete()` it, and keep the design
in sync by no longer declaring it.

## Async

{class}`~niwaki.AsyncNiwaki` mirrors everything on this page —
accumulators stay synchronous, executors become awaitable, and `gather()`
runs reads concurrently.  See {doc}`async` for the concurrency model.

## Next steps

- {doc}`models` — what the typed reads give you back
- {doc}`async` — the same API, concurrent
- {doc}`../cookbook/fabric-audit` — the read side at work
