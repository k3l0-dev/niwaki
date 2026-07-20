# niwaki

**niwaki** is a design-first, fully typed Python SDK for Cisco ACI, built
for network engineers who automate the APIC in Python.  The promise: **you
should not have to memorise the APIC object model** — navigation, object
names and attributes use operator vocabulary with full IDE autocompletion,
and the SDK translates to ACI classes, relation props and wire names for
you.  Structure is literal, vocabulary is translated.

One mental model, everywhere: the **design DSL describes** the desired
configuration — the whole `uni` subtree, from fabric policies to tenants —
**`push()` applies** it (dry-run diff, atomic POST, or staged waves), and
the **facade observes** (navigation, typed reads, queries, deletion).

```python
from niwaki import Niwaki
from niwaki.design import tenant

config = tenant("prod", description="my first tenant")
config.vrf("main")
config.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    config.push(aci, mode="plan")   # dry run — see the diff first
    config.push(aci)                # one atomic POST
```

`bind(vrf="main")` is resolved and checked **before anything touches the
network** — a typo fails in your editor, with a did-you-mean.

## Start here

1. {doc}`guide/installation` — `uv add niwaki`, air-gapped installs included
2. {doc}`guide/quickstart` — describe, apply, observe in ten minutes
3. {doc}`guide/design-dsl` — the full describe surface
4. {doc}`guide/cursors` — the cursor operations, and when to use each
5. {doc}`guide/push-modes` — `strict`, `staged`, `plan`
6. {doc}`guide/observing` — navigation, typed reads, queries
7. {doc}`guide/discovery` — search and describe any of the ~15,300 readable classes

## Find your way

- **Guide** — learn the SDK, one concept per page.
- **Cookbook** — get a real task done, copy-adapt a recipe.
- **Reference** — look it up: every curated position, the API, limits.
- **Explanation** — understand why the SDK has this shape.

Every Python block in this documentation runs as a test in the SDK's own
suite — the examples cannot go stale.

**Coming from cobra?**  Three pages, three jobs: {doc}`why` (the argument),
{doc}`comparison` (the same tasks side by side),
{doc}`cookbook/migrate-from-cobra` (the migration guide).

```{toctree}
:caption: Guide
:maxdepth: 2

guide/installation
guide/quickstart
guide/design-dsl
guide/cursors
guide/push-modes
guide/observing
guide/discovery
guide/connection
guide/errors
guide/testing
guide/async
guide/models
```

```{toctree}
:caption: Cookbook
:maxdepth: 2

cookbook/index
```

```{toctree}
:caption: Reference
:maxdepth: 2

reference/index
```

```{toctree}
:caption: Explanation
:maxdepth: 1

design-first
inside-the-dsl
why
comparison
```

```{toctree}
:caption: Project
:maxdepth: 1

Changelog <project/CHANGELOG>
Contributing <project/CONTRIBUTING>
Security <project/SECURITY>
Code of conduct <project/CODE_OF_CONDUCT>
```
