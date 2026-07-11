<div align="center">

<img src="https://raw.githubusercontent.com/k3l0-dev/niwaki/main/assets/niwaki-banner.svg" width="360" alt="niwaki — a cloud-pruned garden tree">

# niwaki 庭木

**Cisco ACI for humans — describe, push, observe.**

The modern, typed, design-first Python SDK for Cisco ACI.

[![ci](https://github.com/k3l0-dev/niwaki/actions/workflows/ci.yml/badge.svg)](https://github.com/k3l0-dev/niwaki/actions/workflows/ci.yml)
[![docs](https://img.shields.io/badge/docs-k3l0--dev.github.io%2Fniwaki-2e6f45)](https://k3l0-dev.github.io/niwaki/)
[![python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://github.com/k3l0-dev/niwaki/blob/main/pyproject.toml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/k3l0-dev/niwaki/blob/main/LICENSE)
[![pypi](https://img.shields.io/pypi/v/niwaki?color=2e6f45)](https://pypi.org/project/niwaki/)
[![ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-strict-blue)](https://github.com/k3l0-dev/niwaki/blob/main/pyproject.toml)
[![typed](https://img.shields.io/badge/types-Pydantic%20v2-e92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)

</div>

---

*Niwaki* (庭木) is the Japanese art of sculpting full-size, living garden
trees — exactly what this SDK does to the APIC Management Information Tree:
not a miniature in a pot, a production tree, pruned with intent.

The promise: **you should not have to memorise the APIC object model.**
Navigation, object names, and attributes use operator vocabulary with full IDE
autocompletion; the SDK translates to ACI classes, `tn*Name` relation props,
and wire attribute names for you.

## Why another SDK for Cisco ACI?

Cisco ships an official Python SDK — [cobra](https://cobra.readthedocs.io/).
It is authoritative and complete, but it was designed a decade ago and it
shows: you install it as **two wheels downloaded from your own APIC**,
version-matched to the firmware, targeting "Python 2.7 or 3.6"; you write
**wire names** and relation classes by hand (`RsCtx(bd,
tnFvCtxName='prod')`); and every mistake is discovered **by the APIC, after
the POST**.

| | cobra (official) | niwaki |
| --- | --- | --- |
| Distribution | two `.whl` downloaded from a running APIC, firmware-matched | one wheel, standard packaging, installable from an index |
| Python | "2.7 or 3.6", untyped | 3.12+, `Typing :: Typed`, full IDE autocompletion |
| Writing model | imperative: build MOs, `ConfigRequest`, `commit()` | design-first: **describe → plan → push** (atomic or staged waves) |
| Vocabulary | ACI classes and wire names (`fv.BD`, `arpFlood`, `tnFvCtxName`) | operator verbatim (`.bd("web").set(arp_flooding=True).bind(vrf="prod")`) |
| References | relation classes + target-name strings, unchecked | `bind()` resolved closed-world at push time — a typo fails **before any request**, with a did-you-mean |
| Validation | server-side, after the POST | at the call site (Pydantic), plus a `plan` dry-run diff |
| Async / retry / pagination | — | first-class async mirror, proactive token refresh, retries, transparent pagination |

cobra remains the reference when you need guaranteed write parity with your
exact firmware and Cisco support behind it.  For everything else — reading
fabrics, building and converging configuration as code — niwaki is built to
be the SDK you *want* to write.  The deep comparison lives in
[the documentation](https://k3l0-dev.github.io/niwaki/why.html).

## Installation

```bash
uv add niwaki          # or: pip install niwaki
```

Requires Python 3.12+.  To work on the SDK itself, clone and `uv sync --extra dev`.

Full documentation — guides, cookbook, vocabulary book, API reference —
lives at **<https://k3l0-dev.github.io/niwaki/>**.

## Quickstart — declarative provisioning (design DSL)

**One mental model**: describe the desired configuration with the
design DSL, apply it with `push()`, observe with the facade.  The DSL covers
the whole `uni` subtree — tenants, access policies (`infra`), fabric policies
(`fabric`), controller policies — with the same vocabulary everywhere.

Build a detached design tree (no session, no I/O), then validate and push it
in one call:

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = (
    tenant("prod")
    .app("shop")
        .epg("frontend").bind(bd="frontend").consume("fe-to-be")
        .epg("backend").bind(bd="backend").provide("fe-to-be")
    .bd("frontend")
        .set(unicast_routing=True)
        .subnet("10.0.1.1/24")
        .bind(vrf="prod")
    .bd("backend")
        .set(unicast_routing=True)
        .subnet("10.0.2.1/24")
        .bind(vrf="prod")
    .vrf("prod")
    .filter("api")
        .entry("rest", tcp=8080)
    .contract("fe-to-be")
        .set(scope="vrf")
        .subject("api").bind(filter="api")
)

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    config.push(aci, mode="strict")
```

Fabric and access policies use the same verbs — and multi-domain designs are
one `design()` away:

```python
from niwaki.design import design

cfg = design()
cfg.fabric().datetime_policy("prod-ntp").ntp_provider("10.0.0.1")
inf = cfg.infra()
inf.vlan_pool("prod", "static").range("vlan-100", "vlan-199")
cfg.phys_dom("prod-phys").bind(vlan_pool="prod")
inf.aaep("prod-aaep").bind(domain="prod-phys")
cfg.tenant("prod").app("shop").epg("web").bind_dn(domain="uni/phys-prod-phys")

cfg.push(aci)          # everything above in ONE atomic POST
```

Day-2 changes are just smaller designs — declare the field you want, the
parent chain rides along as attribute-less upserts:

```python
from niwaki.design import infra

flip = infra().cdp_policy("cdp-on", admin_state="disabled")
flip.push(aci, mode="plan")   # shows exactly one field change
flip.push(aci)
```

What the DSL gives you:

- **Structure is literal, vocabulary is translated** — every maker maps 1:1 to
  a real APIC child class (`.subject()` is a `vzSubj`, `.pim()` is a
  `pimCtxP`), but names and parameters are the ones operators actually use
  (`entry("rest", tcp=8080)` compiles to `etherT/prot/dFromPort/dToPort`).
- **Lazy, closed-world references** — `bind()`, `provide()`, `consume()`
  resolve at push time; forward references are fine; a typo fails **before
  any request**, with a did-you-mean. Direction is handled for you:
  `.vrf("prod").bind(l3out="ext")` creates the `l3extRsEctx` on the L3Out
  side, where ACI expects it.
- **Typed cursors per position** — makers, `set()` fields, and `bind()`
  aliases are generated with full signatures: autocompletion and mypy cover
  the entire curated vocabulary. `.mo(AnyClass, ...)` remains as the escape
  hatch, and `bind_dn(alias=dn)` references objects outside the design by
  raw DN.
- **Eager validation** — every name and attribute is checked by the Pydantic
  models at the call site, not on the wire.

### Push modes

| Mode | Behaviour |
| --- | --- |
| `strict` (default) | Closed-world validation, then **one atomic POST** of the whole design to `/api/mo/uni.json` — all or nothing. |
| `staged` | One operation per object, executed in DN-depth waves (parents before children); atomic classes (vPC pairs) ship whole; a partial failure raises `StagedPushError` with plain DNs. |
| `plan` | Dry run: reads the current APIC state (one read per declared domain) and reports creates/updates — nothing is pushed. |

`config.to_payload()` returns the exact strict-mode payload without executing
anything (same philosophy as `Query.build()`).

## Reading — typed queries

```python
from niwaki import Niwaki
from niwaki.models.fv.fvBD import fvBD

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    # Jargon navigation, no class imports needed
    bd = aci.tenant("prod").bd("frontend").read()

    # Query builder: filters, scoping, enrichment, pagination
    # (filters address the APIC attribute names — the wire side)
    bds = aci.query(fvBD).where(arpFlood=True).under("uni/tn-prod").fetch()
    n = aci.tenant("prod").query(fvBD).count()

    # Any of the ~15k APIC classes by name (read-only/operational included)
    nodes = aci.query("topSystem").naming_only().fetch()
```

Async is a first-class mirror of the sync API:

```python
from niwaki import AsyncNiwaki
from niwaki.models.fv.fvTenant import fvTenant

async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
    tenants, bd = await aci.gather(
        aci.query(fvTenant).fetch(),
        aci.tenant("prod").bd("frontend").read(),
    )
    await config.push(aci, mode="strict")   # the design DSL is async-ready too
```

## What's inside

- **Design DSL** (`niwaki.design`): THE write path — see above.  Curated
  vocabulary in `domain/vocabulary.yaml`, typed cursors generated per
  position, unified reference resolver (`REFERENCE_MAP`, name + DN flavors,
  abstract targets).
- **2,222 generated Pydantic models** (APIC v6.0 schemas) with human-readable
  field names, constraints, and 558 enums — models carry data and validation,
  never write logic.
- **Facade** (observation): jargon navigation (`aci.tenant("x").bd("y")`),
  typed reads, queries, delete.
- **Sync + async transport**: cookie/token auth, proactive refresh, retry
  with backoff, transparent pagination, typed exception hierarchy.
- Cold-start import: ~90 ms; heavy tables load lazily on first use.

## Development

```bash
uv sync --extra dev
```

Documentation (the hosted site is <https://k3l0-dev.github.io/niwaki/>;
to build it locally — static HTML, no server needed):

```bash
uv sync --extra docs
bash scripts/docs.sh open     # build + open docs/_build/html/index.html
```

Development, the full test suite (13,700+ unit tests plus a three-act live
walkthrough against a lab APIC) and release engineering run in the
maintainers' private infrastructure.  This repository is the public home of
the SDK: source, documentation, releases and issues.

## Status

Active development. 13,700+ tests, mypy strict, ruff. The design DSL covers a
curated vocabulary across tenant, access (`infra`), fabric, and controller
policies (~50 positions); everything else is reachable via `.mo()` and
`bind_dn()`. Why this SDK exists: [the comparison with cobra](https://k3l0-dev.github.io/niwaki/why.html).

## License

Apache License 2.0 — see [LICENSE](https://github.com/k3l0-dev/niwaki/blob/main/LICENSE) and [NOTICE](https://github.com/k3l0-dev/niwaki/blob/main/NOTICE).
Copyright 2026 Monark AIOPS SRL.  Developed by Khalid El-Ouiali.

Cisco, Cisco ACI and APIC are trademarks of Cisco Systems, Inc.  niwaki is an
independent project, not affiliated with or endorsed by Cisco Systems, Inc.
