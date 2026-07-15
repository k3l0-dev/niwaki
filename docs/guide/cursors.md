# Cursors: the operations, and when to use each

Everything you build with the design DSL, you build through a **cursor**.  This
page defines what a cursor is and, for every operation one offers, says plainly
*when* and *why* you reach for it.

## What a cursor is

A cursor is a **typed position in a design tree**.  It wraps one node of the
detached, in-memory description you are building — nothing more.  Two facts
follow from that, and they explain everything else on this page:

- **A maker returns the cursor of the child it declared.**  `tenant("prod")`
  hands you a cursor at the tenant; `.bd("web")` declares a bridge domain
  under it and hands you *the BD's* cursor.  Chaining is just using each
  returned cursor to declare the next thing.
- **A cursor performs no I/O.**  Building and validating happen in memory, at
  the call site — a bad name or value raises *before* any request.  Only
  `push()` talks to the APIC.

```python
from niwaki.design import tenant

tn = tenant("prod")          # a cursor at the tenant
bd = tn.bd("web")            # a cursor at the new BD — a different position
assert bd.dn == "uni/tn-prod/BD-web"
```

A cursor's short methods are the **curated build vocabulary** for its position
(a `.node()` maker under a vPC pair, a `.subnet()` under a BD); the handful of
methods below are the same on *every* cursor.  They fall into two groups:
operations that **build** the tree, and operations that **act on** it.

## Building the tree

### Makers — declare a child object

**When:** whenever you need an object to exist.  **Why:** a maker maps 1:1 to a
real APIC child class, so declaring `.bd("web")` is exactly declaring one
`fvBD` — nothing is invented or hidden.  A maker takes the object's naming
argument(s) and, optionally, its attributes:

```python
tn = tenant("prod")
tn.bd("web", arp_flooding=True, unicast_routing=False)   # declare + configure
```

Makers are the backbone; every other build operation configures or connects
what a maker declared.

### `set()` — configure this object's attributes

**What it does:** sets the scalar attributes of the object the cursor points at
— using the readable Python names, with sugar applied and the value validated
by the Pydantic model immediately.  Calls **merge** (last one wins).

There is **one attribute schema**, reachable at **two moments**:

- **On the maker — configure at birth.**  `.bd("web", arp_flooding=True)` is the
  concise way when you already know the values.
- **Via `set()` — configure an object already declared.**  This is the *only*
  way to add or adjust attributes after the maker call, because a design
  declares each object exactly once:

```python
tn = tenant("prod")
bd = tn.bd("web")
bd.set(arp_flooding=True)
bd.set(unicast_routing=False)   # merges — both attributes are now set
```

Re-calling the maker to add an attribute is a mistake the SDK catches for you,
and points you at `set()`:

```python
from niwaki.exceptions import DuplicateDeclarationError

tn = tenant("prod")
tn.bd("web", arp_flooding=True)
try:
    tn.bd("web", unicast_routing=False)   # NOT how you add an attribute
except DuplicateDeclarationError as exc:
    assert "already declared" in str(exc)  # the message points at set()
```

**Reach for `set()` (rather than maker kwargs) when:**

- you have already declared the object and want to add or change an attribute;
- you are building incrementally — in a loop, or conditionally;
- you are writing a **day-2 patch**: a small design whose only job is to change
  a field, so only what you `set()` travels in the payload:

```python
patch = tenant("prod").bd("web").set(description="patched")
# push(mode="plan") reports exactly one field change; parents touch nothing.
```

**Use maker kwargs when** you know the values at declaration time — it is the
same schema, just fewer keystrokes.

### `bind()` — reference another object by name

**When:** the object needs to point at *another object declared in this design*
— a BD at its VRF, an EPG at its BD.  **Why:** you name the target, and the
schema decides the rest (which relation class, its direction, whether it targets
by name or DN).  The reference is **lazy and closed-world**: resolved at push
time, so forward references are fine, and a name that resolves to nothing fails
with the list of what *is* declared.

```python
tn = tenant("prod")
tn.bd("web").bind(vrf="main")   # the fvRsCtx relation, resolved to the VRF below
tn.vrf("main")                  # declared after — closed world, not ordering
```

You never write an `fvRsCtx` or a `tnFvCtxName` by hand — that is the schema's
job.

### Verbs — a relation whose class is named upfront

**When:** the vocabulary needs to reach a relation that automatic resolution
*cannot* pick on its own — because the same owner has **two relations to the
same target class**.  An EPG both provides and consumes contracts; there is no
way to infer which from the target alone.  **Why they exist:** a verb names its
relation class in the vocabulary, so `provide` and `consume` stay distinct:

```python
epg = tenant("prod").app("shop").epg("web")
epg.provide("web-api")     # fvRsProv
epg.consume("db")          # fvRsCons
```

A bind lets the schema choose the relation class; a verb is used exactly when
that choice would be ambiguous.

### `ref()` — when the relationship itself carries configuration

**When:** the relation object has fields of its own — an EPG-to-domain
attachment's resolution immediacy, a subject filter's `directives` (contract
logging).  **Why:** wrap the target in `ref()` and set those fields where a
plain name would go; they are validated against the relation class on the spot.

```python
from niwaki.design import ref

epg = tenant("prod").app("shop").epg("web")
epg.bind(domain=ref("prod-phys", resolution_immediacy="immediate"))
```

`ref()` works anywhere a name does — in `bind()`, `bind_dn()`, and the verbs.

### `bind_dn()` — reference an object outside the design

**When:** the target exists on the APIC but is **not declared in this design**
(a shared pool, a fabric-discovered node).  **Why:** the closed world cannot
resolve it, so you give the raw DN and the SDK trusts it — no lookup.  Only
DN-flavored aliases qualify; a name-flavored one is refused with an explanation.

```python
from niwaki.design import design

cfg = design()
dom = cfg.phys_dom("prod-phys")          # the VLAN pool is not declared here
dom.bind_dn(vlan_pool="uni/infra/vlanns-[shared]-static")
```

### `mo()` — declare a child of any generated class

**When:** you need a class the vocabulary has **not curated yet** (VMM, service
graphs, a corner your team knows).  **Why:** the escape hatch keeps all 2,222
generated classes one call away, and containment is still validated against the
schema, so you never leave the safety of the models:

```python
from niwaki.models.mgmt.mgmtMgmtP import mgmtMgmtP  # any generated class

tn = tenant("mgmt")
tn.mo(mgmtMgmtP, name="default")   # a curated maker may not exist; the class does
```

If you find yourself reaching for `mo()` often, that is the signal to
[request the vocabulary](https://github.com/k3l0-dev/niwaki/issues/new/choose).

## Acting on the tree

### `push()` — apply the design

**When:** the description is complete and you want it on the fabric.  **Why:**
this is the one operation that performs I/O.  It takes the mode
(`strict` / `staged` / `plan` — see {doc}`push-modes`) and can be called from
any cursor; the whole design is pushed, not just the subtree:

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

config = tenant("prod")
config.bd("web").bind(vrf="main")
config.vrf("main")
report = config.push(aci, mode="plan")   # what would change, no write
```

### `to_payload()` — see the compiled wire payload

**When:** you want to inspect what a design compiles to, in a test or at the
REPL, without an APIC.  **Why:** it returns the exact `polUni` envelope the push
would send — the fastest way to confirm a design is shaped the way you expect:

```python
config = tenant("prod")
config.bd("web", arp_flooding=True)
payload = config.to_payload()

bd = payload["polUni"]["children"][0]["fvTenant"]["children"][0]["fvBD"]
assert bd["attributes"] == {"name": "web", "arpFlood": "true"}
```

### `dn` and `design_node` — introspect

**When:** you need the DN the object will occupy (`dn`), or a structural handle
to the underlying node (`design_node`).  **Why:** they are read-only views —
useful for assertions and tooling, never required to build.

## Which gesture, when

| You want to… | Use |
| --- | --- |
| Create an object | a **maker** (`.bd()`, `.epg()`, …) |
| Set attributes while declaring it | maker **kwargs** (`.bd("web", arp_flooding=True)`) |
| Set attributes on an object already declared, or a day-2 patch | **`set()`** |
| Point at another object **in this design** | **`bind()`** |
| Choose between two relations to the same class | a **verb** (`provide` / `consume`) |
| Configure the relationship object itself | **`ref()`** inside a bind or verb |
| Point at an object **not in this design** | **`bind_dn()`** |
| Declare a class the vocabulary has not curated | **`mo()`** |
| Apply the design | **`push()`** |
| Inspect the compiled payload | **`to_payload()`** |

## Next steps

- {doc}`design-dsl` — the describe surface end to end
- {doc}`push-modes` — `strict`, `staged`, `plan`
- {doc}`../reference/vocabulary/index` — every position and keyword argument
