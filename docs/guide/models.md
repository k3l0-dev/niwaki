# Working with the models

niwaki ships 2,222 typed Pydantic models generated from the APIC schemas,
plus 558 enums.  Models carry **data and validation only** — writing is the
design DSL's job ({doc}`design-dsl`), reading returns them fully typed
({doc}`observing`).  This page is about handling them directly.

## Importing a class

Every model lives in a package named after its ACI class prefix, and the
import path spells the ACI class name — if you know the class, you know the
import:

```python
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.fv.fvTenant import fvTenant
from niwaki.models.vz.vzBrCP import vzBrCP
```

Most of the time you do not import at all:

- **navigation** returns typed instances without imports —
  `aci.tenant("prod").bd("web").read()` is an `fvBD`;
- **queries by name** accept any of the ~15,000 class strings —
  `aci.query("topSystem")` — including operational classes outside the
  generated set.

Import a class when you want the type itself: typed queries
(`aci.query(fvBD)`), `isinstance` checks, or the `.mo()` escape hatch.

## Readable fields, wire aliases

Every attribute has a human-readable field name; the APIC wire name rides
along as its alias.  You write the readable one, the wire format is produced
at serialisation:

```python
bd = fvBD(name="web", arp_flooding=True)

assert bd.arp_flooding is True
assert bd.to_apic() == {"fvBD": {"attributes": {"name": "web", "arpFlood": "true"}}}
```

Two places still speak wire names, because the APIC does: query filters
(`where(arpFlood=True)` — the filter string goes to the APIC verbatim) and
raw payloads you inspect with `to_payload()`.

## Enums

Constrained attributes are real `StrEnum`s — one module per enum, same
import convention as the models:

```python
from niwaki.models.enums.FvBDType import FvBDType

bd = fvBD(name="storage", type="fc")       # plain strings coerce…
assert bd.type is FvBDType.FC              # …into the enum member
assert list(FvBDType) == [FvBDType.FC, FvBDType.REGULAR]
```

## Validation at the call site

Values are validated the moment you construct — a bad value never reaches
the wire:

```python
import pydantic

try:
    fvBD(name="web", type="banana")
except pydantic.ValidationError as exc:
    print(exc.error_count(), "error —", exc.errors()[0]["loc"])
```

Instances returned by reads keep every APIC attribute (also the ones outside
the generated schema version), so a round-trip never loses data.

## Cisco's definitions, built in

The APIC schemas carry a human-written definition for most configurable
properties — and the generated models keep them.  Every described field
carries it as its Pydantic `description`, so your IDE shows Cisco's own
words when you hover an attribute, and the API reference renders them:

```python
from niwaki.models.fv.fvBD import fvBD

info = fvBD.model_fields["arp_flooding"]
assert "ARP flooding" in info.description
```

Enum values are documented too — each generated `StrEnum` member carries
Cisco's per-value description as an attribute docstring (`OspfNwT.BCAST` →
*"Broadcast interface"*), which IDEs surface in autocompletion.

## The `.mo()` escape hatch

The design DSL's curated vocabulary covers {{ positions }} positions; the other 2,000+
classes remain one call away.  `.mo(Class, **attrs)` declares a child of any
generated class at the current position — containment is still validated
against the schema:

```python
from niwaki.design import tenant
from niwaki.models.mon.monEPGPol import monEPGPol

config = tenant("prod")
config.mo(monEPGPol, name="strict-monitoring")

payload = config.to_payload()
children = payload["polUni"]["children"][0]["fvTenant"]["children"]
assert children == [{"monEPGPol": {"attributes": {"name": "strict-monitoring"}}}]
```

If you reach for `.mo()` often for the same class, that is a vocabulary gap
worth reporting — the *vocabulary request* issue template exists exactly for
that ({doc}`coverage matrix <../reference/vocabulary/coverage>`).

## Next steps

- {doc}`../reference/vocabulary/index` — where each class is curated
- {doc}`design-dsl` — the write path built on these models
