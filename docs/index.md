# niwaki

Modern, typed Python SDK for Cisco ACI (APIC) — an idiomatic replacement for
`cobra`.

The promise: **you should not have to memorise the APIC object model.**
Navigation, object names, and attributes use operator vocabulary with full IDE
autocompletion; the SDK translates to ACI classes, `tn*Name` relation props,
and wire attribute names for you.

One mental model, everywhere (ADR-001): the **design DSL describes** the
desired configuration — the whole `uni` subtree, from fabric policies to
tenants — **`push()` applies** it, and the **facade observes** (navigation,
reads, queries, deletion).

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = (
    tenant("prod")
    .app("shop")
        .epg("frontend").bind(bd="frontend").consume("web-api")
        .epg("backend").bind(bd="backend").provide("web-api")
    .bd("frontend")
        .set(unicast_routing=True)
        .subnet("10.0.1.1/24")
        .bind(vrf="prod")
    .bd("backend")
        .set(unicast_routing=True)
        .bind(vrf="prod")
    .vrf("prod")
    .filter("web")
        .entry("api", tcp=8080)
    .contract("web-api")
        .set(scope="vrf")
        .subject("api").bind(filter="web")
)  # fmt: skip

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    config.push(aci)          # one atomic POST — all or nothing
```

```{toctree}
:caption: Guide
:maxdepth: 2

guide/quickstart
guide/design-dsl
guide/push-modes
guide/observing
```

```{toctree}
:caption: Reference
:maxdepth: 2

reference/vocabulary/index
reference/api/index
```

```{toctree}
:caption: Explanation
:maxdepth: 1

why
```
