# Working with an existing fabric

Real fabrics are never blank. Years of GUI clicks, other tools, colleagues
who left — the APIC is full of configuration your scripts did not create.
This page states exactly how a niwaki design behaves in that world. The
whole contract fits in three rules:

1. **What your design declares, it manages** — no matter who created it.
2. **What your design does not declare, it never touches.**
3. **A push never deletes anything.** Deleting is a separate, explicit call.

The boundary is *declaration*, not provenance: the APIC identifies objects
by DN and keeps no record of who created them. Declaring an object that
already exists means taking it over — there is no import ceremony.

## Adopting configuration created in the GUI

Say `uni/tn-prod/BD-web` was clicked together years ago. Declare it — same
class, same names, therefore same DN — and it is yours:

```python
from niwaki import Niwaki
from niwaki.design import tenant

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

# simulate the pre-existing GUI state on the fake APIC
legacy = tenant("prod")
legacy.bd("web", arp_flooding=False, description="do not touch???")
legacy.push(aci)
```

```python
cfg = tenant("prod")
cfg.bd("web", arp_flooding=True)

plan = cfg.push(aci, mode="plan")
assert plan.creates == []  # the BD already exists
assert "uni/tn-prod/BD-web" in plan.updates  # one field would change
assert plan.updates["uni/tn-prod/BD-web"] == {"arp_flooding": (False, True)}

cfg.push(aci)
bd = aci.tenant("prod").bd("web").read()
assert bd.arp_flooding is True
assert bd.description == "do not touch???"  # undeclared → untouched
```

Two things happened, and one deliberately did not:

- the declared field changed;
- every field you did *not* declare kept its APIC value — a push merges
  attribute by attribute, it does not replace objects;
- nothing else in the tenant was even looked at.

## Day-2: change one setting, touch nothing else

Declaring only the field you care about is the idiomatic day-2 operation.
The parent chain travels as attribute-less upserts that modify nothing:

```python
from niwaki.design import infra

infra().cdp_policy("CDP-ON", admin_state="disabled").push(aci)
```

One setting changed; the `infra` node above it was only a path.

## What a push will never do

If the tenant also holds `BD-legacy` and a forgotten test EPG, a push of
your three declared BDs passes them by without reading them. There is no
reconciliation mode: a design is a stencil, and the push only paints
inside the cutouts — {func}`~niwaki.design.reconcile` reports what lives
outside them, and never deletes. Removing an object is always a separate,
explicit call on the observation side:

```python
aci.node("uni/tn-prod/BD-web").delete()
```

This split is deliberate. It makes partial adoption safe: you can manage
one tenant from code while the rest of the fabric stays under the GUI,
another team, or another tool — and no run of your script can widen its
own blast radius.

## Referencing existing objects without managing them

A design can lean on objects it does not want to own — the infra team's
VLAN pool, a discovered fabric port — with `bind_dn` and the literal-DN
makers. Those raw DNs are the one thing the closed world cannot check, and
the APIC *accepts* a relation whose target does not exist (the relation
just stays unformed). Verify them before anything is written:

```python
from niwaki import exceptions
from niwaki.design import design

dom = design().phys_dom("prod-phys")
dom.bind_dn(vlan_pool="uni/infra/vlanns-[missing]-static")

try:
    dom.push(aci, verify_refs=True)
except exceptions.DanglingReferenceError as exc:
    assert exc.failures[0].status == "missing"  # caught before the wire
```

See {doc}`push-modes` for the full `verify_refs` contract.

## The full circle

Adoption also works wholesale: a capture of the fabric can become a
design, the design can become Python source, and that source is yours to
keep in git. The pieces — {func}`~niwaki.design.to_design`,
{meth}`~niwaki.design.Cursor.slice`, {func}`~niwaki.design.to_code`,
{func}`~niwaki.design.merge` — are pure functions over data: no session
is involved until you decide to plan or push the result.

On a real fabric you would start from {func}`~niwaki.snapshot.take`
(`snap = snapshot.take(aci)`). A capture is plain data — wire class
names, wire attributes — so this page builds a small one inline:

```python
from niwaki.design import to_design
from niwaki.snapshot import Snapshot

snap = Snapshot(
    scope="uni",
    tree={
        "class": "polUni",
        "rn": "uni",
        "attributes": {},
        "children": [
            {
                "class": "fvTenant",
                "rn": "tn-prod",
                "attributes": {"name": "prod"},
                "children": [
                    {
                        "class": "fvBD",
                        "rn": "BD-web",
                        "attributes": {"name": "web", "arpFlood": "yes"},
                        "children": [
                            {
                                "class": "fvRsCtx",
                                "rn": "rsctx",
                                "attributes": {"tnFvCtxName": "main"},
                                "children": [],
                            },
                        ],
                    },
                    {
                        "class": "fvCtx",
                        "rn": "ctx-main",
                        "attributes": {"name": "main"},
                        "children": [],
                    },
                ],
            },
        ],
    },
)

config = to_design(snap)
```

`to_design` prefers the curated vocabulary and proves each inversion: the
captured `fvRsCtx` relation object comes back as the `bind(vrf="main")`
that would have created it. Anything it cannot prove rides the wire-name
doors (`raw()` / `raw_set()`) — a fallback, never a guess. The proof is
easiest to see through {func}`~niwaki.design.to_code`, which renders any
design as the DSL source that replays it:

```python
from niwaki.design import to_code

source = to_code(config)
print(source)
```

```text
from niwaki.design import design

cfg = design()
tenant_prod = cfg.tenant('prod')
tenant_prod.bd('web', arp_flooding='yes').bind(vrf='main')
tenant_prod.vrf('main')
```

The emitted source is not an approximation — executing it rebuilds a
design with the exact same payload:

```python
replayed: dict = {}
exec(source, replayed)
assert replayed["cfg"].to_payload() == config.to_payload()
```

From here the imported design composes like any other.
{meth}`~niwaki.design.Cursor.slice` carves one subtree into a fresh
design (ancestors ride along as attribute-less upserts, so pushing the
slice never touches them), and {func}`~niwaki.design.merge` combines
designs, failing loud on any contradiction:

```python
from niwaki.design import merge

web_only = config.slice("uni/tn-prod/BD-web")

extra = tenant("prod")
extra.vrf("main")
extra.bd("db").bind(vrf="main")

combined = merge(web_only, extra)
dns = [node.dn for node in combined.view()]
assert "uni/tn-prod/BD-web" in dns
assert "uni/tn-prod/BD-db" in dns
```

One subtlety worth knowing: the slice kept the BD's VRF relation even
though its target now lives outside the slice — pinned as an explicit
relation child carrying the exact wire name (`raw("fvRsCtx",
tnFvCtxName="main")`), so the slice's wire payload stays faithful. A
reference is never silently dropped.

To *see* everything the fabric holds regardless of origin, the
observation surface remains — {doc}`observing` and {doc}`discovery` read
every class the APIC serves, whoever created it.
