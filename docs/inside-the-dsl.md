# Inside the DSL

The design DSL is niwaki's core value: the layer that lets an operator write
**their own vocabulary** — `.bd("web").bind(vrf="prod")`, `.bgp_peer(...)`,
`.external_epg(...)` — and get exact, validated APIC payloads out.  The
{doc}`guide/design-dsl` teaches how to *use* it; this page explains what it
is made of, how it is implemented in this repository, and how to help it
grow — **the vocabulary is where contribution is most welcome**.

A design is a detached, typed description that compiles to the exact wire
payload:

```python
from niwaki.design import tenant

config = tenant("shop")
config.bd("web", arp_flooding=True).bind(vrf="prod")
config.vrf("prod")

payload = config.to_payload()

bd = payload["polUni"]["children"][0]["fvTenant"]["children"][0]["fvBD"]
assert bd["attributes"] == {"name": "web", "arpFlood": "true"}
assert bd["children"] == [{"fvRsCtx": {"attributes": {"tnFvCtxName": "prod"}}}]
```

Three translations happened, none of them magic: the maker `.bd()` became
the real APIC child class `fvBD`, the readable `arp_flooding` became the
wire attribute `arpFlood`, and the lazy `bind(vrf=...)` was resolved
closed-world into the `fvRsCtx` relation with its `tnFvCtxName` target
property.  Everything the DSL knows comes from two places: **one curated
YAML file** and **the APIC schemas**.

## One source of truth: `vocabulary.yaml`

Every word of the DSL is a line in
[`src/niwaki/domain/vocabulary.yaml`](https://github.com/k3l0-dev/niwaki/blob/main/src/niwaki/domain/vocabulary.yaml)
— 175+ curated positions and counting.  Six sections, each with one job:

```yaml
jargon:                        # ACI class → operator short name
  fvAEPg: epg

makers:                        # parent class → {maker name → child class}
  fvTenant:
    bd: fvBD
    l3out: l3extOut

binds:                         # cursor class → {bind alias → target class}
  fvBD:
    vrf: fvCtx                 # the Rs class and direction come from the schemas

verbs:                         # contract shorthand
  fvAEPg:
    provide: {rs: fvRsProv, target: vzBrCP}

sugar:                         # typed convenience parameters
  vzEntry:
    tcp: "int | str | tuple[int, int] | None"

atomic:                        # subtrees the APIC validates as a unit
  - fabricExplicitGEp
```

The rule behind every line: **structure is literal, verbatim is
translated**.  A maker never invents an object — it names a real APIC child
class in the words an operator uses.  Curation is deliberate
({doc}`design-first` explains why 175 hand-reviewed positions beat 2,222
auto-generated ones); the escape hatches `.mo(AnyClass, ...)` and
`bind_dn(alias=dn)` keep the rest of the schema one call away.

## Generated, never hand-written

Three generators in `src/niwaki/_codegen/` turn that YAML plus the APIC
schemas into everything the DSL ships.  Their outputs are committed,
regenerable, and **drift-guarded**: a test fails if any generated artifact
does not match a fresh regeneration.

| Generator | Output | What it derives |
| --- | --- | --- |
| `generate_domain` | `domain/_child_map.py` | `CHILD_MAP` (12,500+ navigation names from schema labels, sibling collisions resolved), `REFERENCE_MAP` (1,700 relation edges with their name/DN flavor), abstract-target expansion |
| `generate_design` | `design/_generated_cursors/` | one typed cursor class per **position** (a maker path, not a class — `infraNodeBlk` gets distinct cursors under leaf and spine selectors), one module per domain, loaded lazily |
| `generate_docs` | `docs/reference/vocabulary/` | the vocabulary book and the coverage matrix — documentation that cannot drift from the code |

The cursor package is built to scale: each position's makers live in one
mixin, and a cursor *inherits* its ancestor chain — the method resolution
order reproduces the runtime's nearest-wins name resolution exactly, and the
generated code stays around 75 lines per position.  Signatures are
introspected from the generated Pydantic models, so field names and enums
can never disagree with validation.

The documentation is generated from the same source: Cisco's schema
comments flow into every model field's `description`, into per-value enum
docstrings, and into each maker's Args section — hovering
`ospf_interface_policy(` in an IDE shows Cisco's definition of every
parameter, its allowed values, and its default.

The generated classes add **types only**.  The runtime is a thin, stable
core: `design/_cursor.py` dispatches makers dynamically, `design/_resolver.py`
resolves references closed-world (name flavor for `tn*` properties, DN
flavor for `tDn`, abstract targets matched against their concrete
subclasses), and the push engine compiles the tree to `strict`/`staged`/
`plan` executions ({doc}`guide/push-modes`).

## How the vocabulary grows

Coverage advances in curated waves, with a tool doing the heavy lifting and
a human owning every name that becomes public API:

```text
uv run python -m niwaki._codegen.propose_vocabulary l3extOut --wave my-wave
```

`propose_vocabulary` walks a schema subtree and emits a candidate YAML block
shaped exactly like `vocabulary.yaml`: maker names taken from the navigation
jargon (so the DSL and the facade always agree), bind aliases derived from
`REFERENCE_MAP`, contract verbs detected from the `Rs*Prov`/`Rs*Cons` pairs
— and a `# REVIEW:` comment on every line that deserves a human eye
(collision-resolved names, over-long labels, abstract targets).  The
reviewed block is merged **by hand** into `vocabulary.yaml`; candidates are
never program input.

The safety net then takes over, automatically: every merged entry is
validated by parametrized tests — real schema containment, resolvable
references, jargon agreement — the cursors and the book regenerate under
drift guards, and new positions are exercised live against a lab APIC
before they ship.

## Contribute here

The vocabulary is the part of niwaki that grows best with many hands —
every network team has corners of ACI it knows intimately:

- **Missing verbatim?**  Open a
  [vocabulary request](https://github.com/k3l0-dev/niwaki/issues/new/choose)
  — name the ACI classes and the words your team uses for them.  The
  {doc}`coverage matrix <reference/vocabulary/coverage>` shows what is
  curated today.
- **Want to propose the entries yourself?**  A vocabulary PR is a few YAML
  lines plus the regenerated artifacts
  (`uv run python -m niwaki._codegen.generate_domain && uv run python -m
  niwaki._codegen.generate_design && uv run python -m
  niwaki._codegen.generate_docs`).  The parametrized guards in
  `tests/design/test_core_yaml.py` tell you immediately whether an entry is
  valid — you do not need a fabric to contribute.
- The workflow and review model are described in
  [CONTRIBUTING](project/CONTRIBUTING.md).

## Reading further

- {doc}`guide/design-dsl` — using the DSL
- {doc}`design-first` — why one write path, why curation
- {doc}`reference/vocabulary/index` — every position, maker and alias
- {doc}`comparison` — the same tasks in cobra and niwaki
