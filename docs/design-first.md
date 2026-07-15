# Design-first architecture

niwaki gives configuration exactly **one way in**: you *describe* the desired
configuration with the design DSL, `push()` *applies* it, and the facade
*observes* the result.  One mental model covers the whole `uni` subtree —
tenants, access policies, fabric policies, controller policies — with the same
vocabulary and the same verbs everywhere.  This page explains the principles
behind that shape.

## Three trades, one path each

| Trade | The one path | What it contains |
| --- | --- | --- |
| **Describe** | {mod}`niwaki.design` | roots `design()` / `tenant()` / `infra()` / `fabric()` / `controller()`, typed cursors, one curated vocabulary, `set()`, lazy `bind()` / `provide()` / `consume()`, closed-world validation |
| **Apply** | the push engine | `strict` (one atomic POST), `staged` (DN-depth waves), `plan` (dry-run diff) — see {doc}`guide/push-modes` |
| **Observe** | the facade + query builder | vocabulary navigation **read-only**, `read()`, `query()`, `delete()` — see {doc}`guide/observing` |

The facade deliberately has **no write surface**.  A single write path means a
single set of semantics to learn, a single validation story, and payloads you
can always predict — every design can be inspected with `to_payload()` before
anything touches the network.

Day-2 changes are not `update()` calls; they are smaller designs — declare the
field you want, and the parent chain rides along as attribute-less upserts
(harmless by construction):

```python
from niwaki import Niwaki
from niwaki.design import infra

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    infra().cdp_policy("cdp-on", admin_state="disabled").push(aci)
```

Deletion stays an explicit, imperative act (`aci.tenant("x").delete()`):
a design never removes what it does not declare, and there is **no
desired-state reconciliation** by design.  Pruning would turn every partial
design into a loaded weapon.

## Structure is literal, vocabulary is translated

Every maker maps 1:1 to a real APIC child class — `.subject()` *is* a
`vzSubj`, `.pim()` *is* a `pimCtxP`.  The DSL never invents intermediate
objects and never hides structure; what it translates is the **language**:
names (`arp_flooding` instead of `arpFlood`), typed parameters
(`entry("rest", tcp=8080)` compiles to `etherT/prot/dFromPort/dToPort`), and
reference resolution.  You can always map a design line back to the APIC
object it produces.

## Closed-world references, two flavors

`bind()`, `provide()` and `consume()` are **lazy**: they resolve at push
time, so forward references are fine, and a typo fails *before any request*
with a did-you-mean.  Under the hood the resolver handles the two reference
flavors ACI actually uses:

- the tenant world links by **name** (`tnFvCtxName`-style properties);
- the infra/fabric world links mostly by **DN** (`tDn` properties), whose
  schema targets are often *abstract* classes — the resolver matches concrete
  targets against them (`bind(domain=...)` accepts a `physDomP` as well as an
  `l3extDomP`).

The world is closed by default: a reference must point at something declared
in the same design.  Two explicit escape hatches cross that boundary:

- `bind_dn(alias="uni/phys-legacy")` — reference an object that already
  exists on the APIC, by raw DN, without validation;
- `static_path(dn, ...)` — the one relation whose target lives *outside* the
  `uni` subtree (`fvRsPathAtt`), modeled as a literal-DN maker.

## Why a curated vocabulary

niwaki generates 2,222 model classes, but the DSL vocabulary is **curated by
hand** — {{ positions }} positions across tenant, access, fabric and
controller policies, growing by demand (see the coverage matrix).
Generating makers for every class would bury the useful names under thousands
of unreadable ones and offer no ergonomic gain over the raw models.  Curation
keeps the operator vocabulary honest — and everything outside it stays reachable
through `.mo(AnyClass, ...)`, which accepts any of the generated models at any
position.  Coverage grows by demand: missing positions are exactly what the
*vocabulary request* issue template is for.

## Where this leaves the models

The generated Pydantic models carry **data and validation only**.  They
validate eagerly at the call site, navigate cleanly, and serialise to the
wire format; they never write.  Writing belongs to the design DSL, so the
model layer stays small, predictable, and safe to hold anywhere.

## Hardware-dependent integrations

Some parts of the vocabulary configure the ACI side of an integration that only
comes to life with real equipment behind it.  Pushing them lands the intended
configuration on the APIC and re-plans cleanly, exactly like any other object —
but the feature itself needs the hardware:

- **VMM domains** (`vmm_provider(...).vmm_dom(...)`, controllers, credentials,
  vSwitch policies) push their APIC-side config, but a reachable **vCenter /
  SCVMM / Kubernetes** controller is required before inventory actually syncs.
- **Service graphs** (L4-L7) define the abstract graph and logical device on the
  APIC, but rendering a graph needs a real **L4-L7 appliance** to attach to.

Niwaki treats these like everything else — typed makers, closed-world
references, plan/push — and its live verification confirms the config lands and
round-trips.  It does not, and on a bare fabric cannot, verify the downstream
integration; that is the operator's device to provide.

## Reading further

- {doc}`guide/design-dsl` — the DSL in practice
- {doc}`inside-the-dsl` — how the DSL is built, and how to grow it
- {doc}`guide/push-modes` — strict, staged, plan
- {doc}`guide/observing` — the read side
- {doc}`why` — how this compares with the official `cobra` SDK
