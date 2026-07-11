# Why niwaki — a comparison with cobra

Cisco ships an official Python SDK for ACI:
[cobra](https://cobra.readthedocs.io/) (`acicobra` + `acimodel`).  It is the
authoritative binding of the APIC object model, and this page treats it with
the respect it deserves — every claim below comes from its
[official documentation](https://cobra.readthedocs.io/).  But cobra was
designed in the Python 2 era, and its model of the world is the APIC's, not
the operator's.  niwaki exists to close that gap.

## Distribution and lifecycle

cobra is not on PyPI.  Per its
[install guide](https://cobra.readthedocs.io/en/latest/install.html), you
download **two wheels from a running APIC**
(`https://<apic>/cobra/_downloads/`), install `acicobra` first and
`acimodel` second, and the wheel filenames encode the firmware they match
(`acicobra-4.2_2j-…`).  The documented Python support is *"Python 2.7 or
Python 3.6"*, with a dependency on `future`.

Consequences in practice: your automation environment is **coupled to a
firmware version**, upgrading the fabric means re-provisioning every
virtualenv, and dependency resolution knows nothing about it.

niwaki is one standard wheel with PEP 621/639 metadata, `py.typed`, and no
runtime dependency on any APIC: the 2,222 generated models ship inside the
package.  A single environment can talk to any 6.x fabric; the schema
version is a property of the codegen, tracked in the changelog, not of your
venv.

## The writing model

cobra is imperative.  From its
[examples](https://cobra.readthedocs.io/en/latest/api-examples/index.html),
verbatim:

```python
from cobra.model.fv import Tenant, Ctx, BD, RsCtx, Ap, AEPg, RsBd
from cobra.mit.request import ConfigRequest

fvTenantMo = Tenant(uniMo, 'ExampleCorp')
Ctx(fvTenantMo, 'private-net1')
fvBDMo = BD(fvTenantMo, 'bridge-domain1')
RsCtx(fvBDMo, tnFvCtxName='private-net1')
fvApMo = Ap(fvTenantMo, 'WebApp')
fvAEPgMo = AEPg(fvApMo, 'WebEPG')
RsBd(fvAEPgMo, tnFvBDName='bridge-domain1')

configReq = ConfigRequest()
configReq.addMo(fvTenantMo)
moDir.commit(configReq)
```

To write this you must know: the ACI class names (`fv.Tenant`, `fv.AEPg`),
the **relation classes** (`RsCtx`, `RsBd`) and which side owns them, the
wire attribute carrying each target (`tnFvCtxName`, `tnFvBDName`), and the
parent-MO plumbing.  Nothing checks that `'private-net1'` matches the `Ctx`
you created three lines earlier — a typo becomes an APIC error after the
POST, or worse, a dangling reference.

The same configuration in niwaki:

```python
from niwaki.design import tenant

config = (
    tenant("ExampleCorp")
    .vrf("private-net1")
    .bd("bridge-domain1").bind(vrf="private-net1")
    .app("WebApp")
        .epg("WebEPG").bind(bd="bridge-domain1")
)  # fmt: skip

config.push(aci)
```

No relation class, no `tn*Name`, no parent plumbing: `bind(vrf=...)` is
resolved at push time against the objects declared in the design —
**closed world** — and `bind(vrf="private-net2")` fails *before any
request* with `no fvCtx named 'private-net2' is declared in this design.
Declared: private-net1. Did you mean 'private-net1'?`.

Three push modes complete the model: `strict` (one atomic POST of the whole
design — all or nothing), `staged` (per-object waves, parents before
children), and `plan` (a dry-run diff against the live fabric).  cobra has
one: `commit()`.  There is no dry run; the closest workflow is committing
and reading back.

## The language of the code

cobra exposes the model as the APIC stores it: wire attribute names
(`arpFlood`, `unicastRoute`), abbreviation-heavy class names, and query
filters written as raw strings — from the
[getting-started guide](https://cobra.readthedocs.io/en/latest/getting-started.html):

```python
moDir.lookupByClass("fvTenant", propFilter='and(eq(fvTenant.name, "Tenant1"))')
```

Because `acimodel` is generated without type annotations, an IDE cannot
autocomplete attributes or catch a misspelled one; the documentation is an
API dump of the model.

niwaki spends its entire codegen budget on this problem: human-readable
field names with the wire name as alias (`arp_flooding` ↔ `arpFlood`),
558 real enums, typed cursors **per position** so autocompletion knows that
`.node_block()` under a leaf selector differs from the one under a spine
selector, and eager Pydantic validation at the call site.  The
{doc}`vocabulary book <reference/vocabulary/index>` is generated from the
same tables the runtime uses — documentation that cannot drift from the
code.

## Observation

Both SDKs read anything.  cobra queries with `DnQuery`/`ClassQuery` and
string filters; niwaki with a typed builder (`where`, `under`, `include`,
`with_faults/health/stats`, streaming pagination) that still accepts any of
the ~15,000 class names as plain strings for operational classes.  niwaki
adds a first-class **async mirror** (`AsyncNiwaki`, TaskGroup `gather`),
proactive token refresh, typed exceptions and retries with backoff — cobra
predates asyncio and leaves session expiry and retries to the caller.

## Where cobra remains the right tool

Honesty matters in a comparison:

- **Guaranteed firmware parity.**  cobra's wheels come from *your* APIC:
  every class and attribute of that exact firmware is writable.  niwaki's
  models are generated from one schema release (currently APIC 6.0) and its
  curated design vocabulary covers the common operational surface — the
  escape hatches (`.mo()`, `bind_dn()`, string-class queries) cover the
  rest, but a brand-new class in tomorrow's firmware reaches cobra first.
- **Official support.**  cobra is Cisco's; a TAC case can reference it.
- **Python 2 estates.**  If you are still running Python 2.7 automation,
  cobra is your only option — and your bigger problem.

If your goal is configuration as code — reviewable, diffable, converging,
typed — that is the use case niwaki was built for.

## Sources

- [cobra installation guide](https://cobra.readthedocs.io/en/latest/install.html)
  (APIC-hosted wheels, firmware matching, "Python 2.7 or Python 3.6")
- [cobra getting started](https://cobra.readthedocs.io/en/latest/getting-started.html)
  (LoginSession/MoDirectory, ConfigRequest, propFilter strings)
- [cobra examples](https://cobra.readthedocs.io/en/latest/api-examples/index.html)
  (tenant/BD/EPG example quoted above)
- PyPI: `acicobra` and `acimodel` are not published (checked 2026-07-11)
