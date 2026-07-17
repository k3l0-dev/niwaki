# Onboard a new tenant

**Problem** — a new line of business, *commerce*, is coming onto the fabric.
Before any application can land it needs the basics: a tenant to hold it, a
VRF for its routing table, and a bridge domain with a gateway for each network
segment.  This is the "hello fabric" — the smallest complete thing you can
push, and the foundation every later recipe builds on.

Throughout the cookbook this same deployment grows: the `commerce` tenant, a
`prod` VRF, and a `10.30.0.0/16` address plan.

## The design

A design is a detached, in-memory description — building it touches nothing.
Declare the tenant, its VRF, and one bridge domain per segment; each BD binds
its VRF and carries the subnet that is its default gateway:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("commerce", description="Retail commerce platform")
config.vrf("prod")

segments = {
    "bd-web": "10.30.10.1/24",   # public-facing web tier
    "bd-app": "10.30.20.1/24",   # application tier
    "bd-db": "10.30.30.1/24",    # database tier
}
for name, gateway in segments.items():
    config.bd(name, unicast_routing=True).bind(vrf="prod").subnet(gateway)
```

Two things are worth pausing on.  `bind(vrf="prod")` records a **lazy
reference**: at push time the resolver finds the declared VRF and builds the
`fvRsCtx` relation for you — you never write a relation class or a `tnFvCtxName`
string.  And the bind sits on the **BD itself**: bind an alias on the object
that owns it, then descend to its children (`.subnet(...)`).

## Plan

`plan` reads the current fabric and diffs the design against it — nothing is
written.  On an empty fabric every DN is new:

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"{len(plan.creates)} objects to create")
assert plan.has_changes is True
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

## Push

One atomic POST — the tenant lands entirely or not at all:

```python
report = config.push(aci)
assert report.request_count == 1
```

## Verify

Read the fabric back through the facade, in operator vocabulary:

```python
bd = aci.tenant("commerce").bd("bd-web").read()
assert bd.unicast_routing is True

bds = aci.tenant("commerce").query("fvBD").fetch()
assert {b.name for b in bds} == {"bd-web", "bd-app", "bd-db"}

assert config.push(aci, mode="plan").has_changes is False
```

That last line is the declarative payoff: the same object that provisioned the
tenant is also its drift detector.  A converged design plans as a no-op.

## Variations & pitfalls

- **VRF placement** — one VRF per tenant is the common shape, but a shared
  services VRF can live in `tn-common` and be referenced by DN with
  `bind_dn(vrf="uni/tn-common/ctx-shared")` ({doc}`../guide/design-dsl`).
- **Unicast routing is a choice** — a BD with `unicast_routing=False` is a pure
  L2 segment (no gateway).  Set it deliberately; the default follows the APIC.
- **The subnet is the gateway** — `10.30.10.1/24` is the BD's pervasive gateway
  address, not a host route.  One gateway per BD is typical; add more subnets to
  the same BD for secondary gateways.
- **Growing later is a smaller design** — adding a fourth segment is
  `tenant("commerce").bd("bd-cache", unicast_routing=True).bind(vrf="prod").subnet("10.30.40.1/24").push(aci)`;
  the parent chain rides along as attribute-less upserts ({doc}`day-2-changes`).
