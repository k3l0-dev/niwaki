"""Read-only live validation — the read catalogue against a real fabric.

Run:
    uv run pytest tests/integration/test_catalog_live.py -m integration -s

Unlike the numbered phases, this file provisions nothing and owns no ``wipe()``
— it only *observes*.  It confronts the shipped read catalogue
(``niwaki.catalog``) and the ``ManagedObject.__getattr__`` bridge with objects
the fabric produces on its own: node inventory, LLDP adjacencies, faults,
hardware sensors — classes the SDK has no generated model for. The unit suite
proves this offline against the raw schemas; this file proves it against real
wire values the APIC actually emits, and against the abstract-class query
fan-out, which only a live controller can exhibit.

It is safe to run at any point in the suite, including on an empty fabric —
each check skips the classes that have no live instances yet instead of
failing, so it never blocks the walkthrough sequence.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki, catalog
from niwaki.models.base import ManagedObject

pytestmark = pytest.mark.integration

# Genuinely non-generated (no niwaki.models._generated.<pkg>.<cls> module):
# operational/learned classes every ACI fabric populates on its own.
NONGENERATED_CLASSES = ("topSystem", "fabricNode", "lldpAdjEp", "eqptSensor", "faultInst")


def _live_objects(aci: Niwaki, class_name: str) -> list[ManagedObject]:
    objs = aci.query(class_name).fetch()
    if not objs:
        pytest.skip(f"{class_name}: no live instances on this fabric")
    return objs


@pytest.mark.parametrize("class_name", NONGENERATED_CLASSES)
def test_nongenerated_class_is_not_a_typed_model(live_aci: Niwaki, class_name: str) -> None:
    """The classes under test are actually the case this file exists to cover."""
    objs = _live_objects(live_aci, class_name)
    assert type(objs[0]) is ManagedObject


@pytest.mark.parametrize("class_name", NONGENERATED_CLASSES)
def test_readable_names_resolve_for_every_live_attribute(live_aci: Niwaki, class_name: str) -> None:
    """Every wire attribute a real object carries has a working readable name.

    Cross-checks the catalogue's own metadata (``describe``) against
    ``__getattr__`` on objects built from real wire payloads — the naming-parity
    and coercion machinery exercised end to end, not against synthetic fixtures.
    """
    objs = _live_objects(live_aci, class_name)
    doc = catalog.describe(class_name)
    readable_by_wire = {p.wire: p.readable for p in doc.props}

    for mo in objs:
        for wire, value in mo.attrs.items():
            readable = readable_by_wire.get(wire)
            if readable is None:
                continue  # residual/internal prop the catalogue does not surface
            got = getattr(mo, readable)
            assert mo[wire] == value  # raw wire access is always the untouched value
            if not value:
                continue  # coercion of an empty string is not interesting here
            assert got is not None


@pytest.mark.parametrize("class_name", NONGENERATED_CLASSES)
def test_bool_kind_properties_coerce_real_yes_no_values(live_aci: Niwaki, class_name: str) -> None:
    """The exact case a review found broken pre-release: real 'yes'/'no' wire
    strings must become actual Python booleans, not stay stringly-typed."""
    objs = _live_objects(live_aci, class_name)
    doc = catalog.describe(class_name)
    bool_props = {p.wire: p.readable for p in doc.props if p.kind == "bool"}
    if not bool_props:
        pytest.skip(f"{class_name}: no bool-kind property in the catalogue")

    checked = 0
    for mo in objs:
        for wire, readable in bool_props.items():
            raw = mo.attrs.get(wire)
            if raw not in ("yes", "no"):
                continue
            value = getattr(mo, readable)
            assert value is (raw == "yes")
            checked += 1
    if checked == 0:
        pytest.skip(f"{class_name}: no live yes/no value observed for a bool property")


def test_polymorphic_abstract_class_query_fans_out_serverside(live_aci: Niwaki) -> None:
    """``aci.query(<abstract class>)`` must return real concrete instances.

    The read foundation never fans an abstract class out client-side — this
    confirms the APIC itself resolves ``fvEPg`` to its concrete descendants
    (``fvAEPg``, ``l3extInstP``, ``mgmtInB``, ...), matching the catalogue's own
    ``concrete_subclasses``.
    """
    known_concrete = set(catalog.concrete_subclasses("fvEPg"))
    results = live_aci.query("fvEPg").fetch()
    if not results:
        pytest.skip("no fvEPg-family objects on this fabric")

    seen = {mo._wire_class for mo in results}
    assert seen, "query returned objects but none carried a wire class"
    assert seen <= known_concrete, f"unexpected class(es) outside the catalogue's set: {seen}"
    assert "fvEPg" not in seen  # the abstract class itself is never a concrete instance


# Rule-name prefixes for fault codes minted at runtime rather than defined
# statically in the class schema — the catalogue cannot know these by
# construction, no matter how fresh. Threshold-crossing alerts (``tca-*``, e.g.
# an interface's drop-rate policy) key off an operator-configured
# ``statsThresholdPolicy``; FSM task-transition faults (``fsm-*``) key off a
# specific internal task run (e.g. ``fsm-switch-chassis-fsm-fail``). Both were
# found live on this fabric — this set may grow as more dynamic subsystems are
# observed; growing it is expected maintenance, not a sign of a bug.
_DYNAMIC_FAULT_PREFIXES = ("tca-", "fsm-")


def test_fault_codes_seen_live_resolve_or_are_known_dynamic_codes(live_aci: Niwaki) -> None:
    """Every *statically defined* fault code this fabric has raised has a name.

    The catalogue's fault table comes from the class schema corpus — faults a
    class can raise, baked into its definition (e.g. ``fltFvBDInvalidConfigOnBD``
    on ``fvBD``). Some fault codes are instead minted at runtime by a dynamic
    subsystem (see :data:`_DYNAMIC_FAULT_PREFIXES`) and exist nowhere in the
    static schema, so the catalogue cannot know them. This is a real scope
    boundary this live run surfaced, not a bug: assert every *unresolved* code is
    one of these, so a genuinely missing schema-defined fault still fails loud.

    Exercises the public :func:`niwaki.catalog.fault_name`.
    """
    faults = _live_objects(live_aci, "faultInst")
    unresolved = {
        str(f["code"]): str(f["rule"]) for f in faults if catalog.fault_name(str(f["code"])) is None
    }
    unexpected = {
        code: rule
        for code, rule in unresolved.items()
        if not rule.startswith(_DYNAMIC_FAULT_PREFIXES)
    }
    assert not unexpected, f"fault code(s) with no catalogue entry: {unexpected}"


def test_search_and_find_prop_agree_with_a_class_actually_on_the_fabric(
    live_aci: Niwaki,
) -> None:
    """Discovery (offline) points at classes/properties the live fabric confirms."""
    _live_objects(live_aci, "lldpAdjEp")
    assert "lldpAdjEp" in catalog.search("lldp")
    assert ("lldpAdjEp", "chassisIdV") in catalog.find_prop("chassisIdV")
