# Discovering classes

The SDK generates typed models for the ~2,200 *configurable* ACI classes.  The
**read catalogue** covers the rest: read metadata for all ~15,450 ACI
classes — learned endpoints, statistics, hardware, routing runtime — so you can
search for a class, describe it, or read one with human field names.

Discovery is **offline**: it needs no APIC connection.  The catalogue ships with
the package and opens lazily on first use, so `import niwaki` stays cheap.

```python
from niwaki import catalog
```

## Search for a class

Match a class by its wire name or its GUI label:

```python
classes = catalog.search("bridge")
assert "fvBD" in classes
```

## Describe a class

`describe` returns the class's label, its properties (each with a readable name
and a coercion *kind*), its faults, and — for an abstract class — its concrete
subclasses:

```python
doc = catalog.describe("fvBD")
assert doc.label == "Bridge Domain"

arp = next(prop for prop in doc.props if prop.wire == "arpFlood")
assert arp.readable == "arp_flooding"  # the human field name
assert arp.kind == "bool"  # how a wire value reads back
```

It works for a class the SDK does not model, too — a learned endpoint:

```python
endpoint = catalog.describe("fvCEp")
assert endpoint.label == "Client End Point"
assert endpoint.props  # every readable property, described
```

## Find which class carries a property

The complement to `search` — a scan across every class's properties:

```python
hits = catalog.find_prop("arpFlood")
assert ("fvBD", "arpFlood") in hits  # (class, wire property)
```

## The concrete classes behind an abstract one

Querying an abstract class returns concrete instances; `concrete_subclasses`
lists them:

```python
assert "fvAEPg" in catalog.concrete_subclasses("fvEPg")
```

## Where a class lives in the tree

A class is rarely reachable at one place. `dn_formats` gives every DN shape the
APIC uses for it, with the identifying values left as placeholders:

```python
assert catalog.dn_formats("fvBD") == ("uni/tn-{name}/BD-{name}",)

# A subnet is the same class under a bridge domain, an EPG, a tenant, an L2Out
# external EPG, and several service-graph nodes — twelve shapes, one class.
assert len(catalog.dn_formats("fvSubnet")) == 12
```

Quote these; do not rebuild them. Chaining parent RNs does not reproduce them —
the containment graph is both wider than the DNs the APIC actually mints and,
in places, missing parents that it does mint. And a repeated placeholder is
normal: `uni/tn-{name}/BD-{name}` names a tenant and a bridge domain, each
identified by `name`.

An abstract class usually returns an empty tuple — its places belong to the
concrete classes behind it, which `concrete_subclasses` lists.

An empty *string*, on the other hand, is a real template: a container that
prefixes nothing. It can be the whole answer or sit among real templates, so
skip the empties rather than testing the first element.

## Naming a fault code

A `faultInst` object carries a `code` (e.g. `"F0467"`) but not the class that
raised it — `fault_name` looks it up directly, without needing to know:

```python
assert catalog.fault_name("F0467") == "fltFvNwIssuesConfig-failed"
assert catalog.fault_name("F-nonexistent") is None
```

A code that resolves to `None` is not necessarily missing data — a
threshold-crossing alert (a `tca-*` rule, e.g. an interface's drop-rate policy)
mints its fault code at runtime from an operator-configured
`statsThresholdPolicy`, so it exists nowhere in the static class schema the
catalogue is built from.

## Which classes have a generated model

`generated_classes()` enumerates the set behind "generated" everywhere on this
page: every class the SDK ships a typed Pydantic model for, sorted — derived
from the code generator's own index, so it cannot drift from the shipped
models. Offline, like everything else here:

```python
classes = catalog.generated_classes()

assert "fvBD" in classes  # configurable → typed model
assert "topSystem" not in classes  # readable only → catalogue-served
assert list(classes) == sorted(classes)  # deterministic, sorted

catalog.describe(classes[0])  # every name resolves, no KeyError
```

The per-class form is `ClassMeta.has_model` — handy when you already hold a
class name and want to know which world serves it:

```python
assert catalog.class_meta("fvBD").has_model
assert not catalog.class_meta("topSystem").has_model
```

Feeding it a systematic sweep is the intended use — for example, auditing what
a fabric actually uses, one `count()` per configurable class, with this as the
candidate list.

## Readable names on any result

An object read from the APIC exposes **readable field names** even when the SDK
has no model for its class — the catalogue supplies them.  The wire name is
always available through item access:

```python
from niwaki.models.base import ManagedObject

# what a query over an operational class yields, one object
top = ManagedObject.from_apic({"topSystem": {"attributes": {"address": "10.0.0.1"}}})

assert top.infrastructure_ip == "10.0.0.1"  # readable name, from the catalogue
assert top["address"] == "10.0.0.1"  # the raw wire attribute, always there
```

A generated class keeps answering from its typed model; the catalogue only steps
in for classes without one, so reading is uniform across all ~15,450 of them.
