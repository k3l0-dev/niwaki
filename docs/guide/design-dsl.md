# The design DSL

`niwaki.design` is THE configuration path.  A design is a detached, in-memory
tree: building it performs no I/O, and every name and attribute is validated
by the Pydantic models at the call site — a typo fails **before** any request.

Two rules govern the whole surface:

- **Structure is literal** — every maker maps 1:1 to a real APIC child class.
  Nothing is silently created or hidden.
- **Verbatim is translated** — names and parameters are the ones operators
  use (`entry("api", tcp=8080)`, `scope="vrf"`); the SDK translates them to
  ACI classes, enum values and wire attribute names.

The examples on this page share one connected client:

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
```

## Roots

Every design is rooted at `polUni`.  {func}`~niwaki.design.design` opens an
empty multi-domain design; the other factories declare the first domain and
return its cursor:

```python
from niwaki.design import design

cfg = design()
cfg.fabric().datetime_policy("prod-ntp").ntp_provider("10.0.0.1")
cfg.infra().vlan_pool("prod", "static").range("vlan-100", "vlan-199")
cfg.tenant("prod").vrf("main")
cfg.push(aci)                    # the three domains in ONE atomic POST
```

`tenant(...)` is exactly `design().tenant(...)` — sibling domains stay one
maker call away from any cursor.

## Makers and implicit pop

A **maker** declares one child object and returns its cursor.  A maker that
belongs to an ancestor level walks up the path and creates there — that is
what makes the fluent chained style work:

```python
from niwaki.design import tenant

config = (
    tenant("prod")
    .bd("web")
        .subnet("10.0.1.1/24")     # child of the BD
        .bind(vrf="main")          # declared on the BD (alias found by walking up)
    .vrf("main")                   # pops back to the tenant
)  # fmt: skip
```

The indentation is purely visual (Python ignores it inside parentheses); the
`# fmt: skip` trailer keeps the formatter from flattening it.  Cursors are
plain values too — capture them, use loops; the chain is never mandatory.

Every maker, `set()` field and `bind()` alias is generated with a **typed
signature per position**: autocompletion and mypy cover the entire curated
vocabulary (see {doc}`../reference/vocabulary/index`).

Makers are documented straight from the APIC schemas: hovering one in your
IDE shows Cisco's definition of the class and an Args section with each
parameter's meaning, allowed enum values, and default — the DSL documents
ACI while you type:

```python
from niwaki.design import tenant

doc = type(tenant("acme")).bd.__doc__
assert "unique layer 2 forwarding domain" in doc  # Cisco's own definition
```

## References: `bind()` and friends

References are **lazy and closed-world**: `bind(alias=name)` records an
intent, resolved at push time against the objects declared in the design.
Forward references are fine.  The relation class, its direction, and how it
targets (by name or by DN) all come from the schemas — you never write an
`fvRsCtx` or a `tDn` by hand.  Given a design holding the usual cursors:

```python
cfg = design()
tn = cfg.tenant("prod")
epg = tn.app("shop").epg("web")
vrf = tn.vrf("main")
inf = cfg.infra()
aaep = inf.aaep("prod-aaep")
port_selector = inf.access_port_profile("leaf101").port_selector("esxi", "range")
```

every reference is one alias away:

```python
epg.bind(bd="web")                  # name-flavor Rs (tnFvBDName)
aaep.bind(domain="prod-phys")       # dn-flavor Rs, abstract target: matches the
                                    # declared phys_dom / l3_dom / ... by name
port_selector.bind(policy_group="esxi-vpc")  # abstract → access_group or port_channel
vrf.bind(l3out="ext")               # inverse edge: the Rs lands on the L3Out side
```

Contract verbs are the EPG shorthand: `epg.provide("web-api")` /
`epg.consume("web-api")`.

Three escapes when the closed world is not enough:

`bind_dn(alias=dn)`
: Same aliases, raw DN, **no lookup** — for objects that exist on the APIC
  but not in this design.  Only dn-flavor aliases qualify; name-flavor ones
  are refused with an explanation.

`static_path(dn, encap=..., mode=...)`
: A literal-DN maker (`fvRsPathAtt`): the path lives outside `uni`, so it is
  structural, not a bind.

`.mo(Class, **kwargs)`
: Declare a child of any of the 2,222 generated classes, curated or not.
  Containment is still validated against the schema.

## Day-2: declare the desired state

There is no `update()` — a day-2 change is a smaller design.  Only the fields
you `set()` travel in the payload; parents declared without attributes are
upserts that touch nothing:

```python
patch = tenant("prod").bd("backend").set(description="patched")
patch.push(aci, mode="plan")   # exactly one field change reported
patch.push(aci)
```

The `plan` mode is the declarative replacement for "diff then write": push
only when the plan says something drifts.

## Errors are eager and pedagogical

- Unknown maker → `UnknownMakerError` with the available makers and a
  did-you-mean.
- Unknown attribute, or an ACI wire name where the Python field name is
  expected → `DesignError` pointing at the right spelling.
- Duplicate declaration of the same object → `DuplicateDeclarationError`.
- Unresolvable or ambiguous reference at push time →
  `UnresolvedReferenceError` / `AmbiguousBindError`, listing what *is*
  declared.
