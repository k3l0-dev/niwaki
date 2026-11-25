"""Attributes on a carrier node are refused at declaration.

A *carrier* (``carrier`` in ``domain/vocabulary.yaml`` — today ``vmmProvP``,
reached as ``design().vmm_provider("VMware")``) is a non-creatable, path-only
class: the APIC refuses to POST it on its own, so the push emits **no
operation** for it, its declared children post at their full DNs, and the plan
diffs those children alone.

That leaves a hole for any attribute declared on the carrier node itself — the
three push modes disagree about it:

- ``strict`` (one atomic ``polUni`` POST) writes it, inside the envelope;
- ``staged`` drops it silently (no operation carries it);
- ``plan`` never compares it (the carrier is not diffed).

So the same design pushes different configuration depending on the mode, and
the plan cannot see the difference.  The contract pinned here closes the hole
at its source: **every non-naming attribute on a carrier node is refused at
declaration**, through whichever door it arrives (maker keyword arguments,
``set()``, ``raw_set()``, the ``mo()`` and ``raw()`` escape hatches), with a
:class:`~niwaki.exceptions.DesignError` that names the class and the attribute
and says why — a carrier is path-only, its attributes cannot be pushed.

The importers (``to_design`` / ``from_payload``) are the one place that
cannot simply refuse: a real capture of a fabric carries configurable
non-naming properties on ``vmmProvP``.  They import the carrier bare and
report each attribute they dropped (a ``DesignHintWarning`` naming the class,
the DN and the attribute) — see the section above
:class:`TestImportOfACarrierWithAttributes`.

The tests are parametrized over the carrier set read from the vocabulary, so a
carrier curated tomorrow is covered the day it lands.
"""

from __future__ import annotations

import inspect
import warnings
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Literal

import pytest
from pydantic import ValidationError

import niwaki.design._cursor as cursor_module
from niwaki.design import Cursor, design, from_payload, merge, to_design
from niwaki.design._compiler import compile_ops
from niwaki.design._cursor import _class_may_contain, _load_class, _tables
from niwaki.design._node import RawDesignNode
from niwaki.design._resolver import resolve
from niwaki.domain._child_map import CLASS_PKG
from niwaki.exceptions import DesignError, DesignHintWarning
from niwaki.models.base import ManagedObject
from niwaki.query._catalog import catalog
from niwaki.snapshot import Snapshot, _normalise_attrs
from niwaki.testing import FakeApic
from niwaki.types import JsonObject, JsonValue
from tests.design.test_import import canonical, payload_to_tree

from .._jsonpath import as_object, as_objects, at

# ── The carrier set, read from the vocabulary ─────────────────────────────────


def _carriers() -> list[str]:
    """Every curated carrier class, sorted (the vocabulary is the single source)."""
    return sorted(_tables().carrier)


def _wire(cls: type[ManagedObject], field: str) -> str:
    """Wire spelling of a model field (its serialization alias, or itself)."""
    alias = cls.model_fields[field].serialization_alias
    return alias if isinstance(alias, str) else field


def _naming(cls: type[ManagedObject]) -> dict[str, object]:
    """Valid naming values for *cls*, by readable prop name (defaults first)."""
    values: dict[str, object] = {}
    for prop in cls._naming_props:
        info = cls.model_fields[prop]
        values[prop] = "ab" if info.is_required() else info.default
    return values


def _maker(cursor: Cursor, label: str) -> Callable[..., Cursor]:
    """The maker *label* of *cursor* (runtime dispatch, any cursor class)."""
    maker: Callable[..., Cursor] = getattr(cursor, label)
    return maker


def _untyped(cursor: Cursor) -> Cursor:
    """The same node behind the **base** :class:`Cursor`: no typed signature in the way.

    The generated typed cursors narrow ``set()`` and the makers to the curated
    keywords; on a carrier those keywords are gone (an attribute there is a
    type error, and a ``TypeError`` at runtime).  The runtime refusal
    (:class:`DesignError`) is what a caller reaches through the untyped door —
    the base class, whose ``set(**attrs)`` and ``__getattr__`` makers accept
    anything and leave the refusal to the declaration.  Every test of that
    refusal goes through here, so none depends on the typed surface staying
    wide.
    """
    return Cursor(cursor.design_node)


def _maker_path(target: str) -> list[tuple[str, str]]:
    """Makers leading from the ``polUni`` root to *target*: ``[(label, class), …]``.

    Raises:
        AssertionError: No chain of curated makers reaches *target*.
    """
    makers = _tables().makers
    queue: deque[tuple[str, list[tuple[str, str]]]] = deque([("polUni", [])])
    seen = {"polUni"}
    while queue:
        parent, path = queue.popleft()
        for label, child in makers.get(parent, {}).items():
            if child in seen:
                continue
            step = [*path, (label, child)]
            if child == target:
                return step
            seen.add(child)
            queue.append((child, step))
    raise AssertionError(f"no chain of curated makers reaches the carrier {target!r}")


def _declare(root: Cursor, target: str, *, stop_before: bool = False) -> Cursor:
    """Declare the maker chain to *target* (naming only); the cursor on its end.

    With *stop_before* the chain stops at the carrier's parent, so the caller
    can declare the carrier itself through the door under test.
    """
    path = _maker_path(target)
    if stop_before:
        path = path[:-1]
    cursor = root
    for label, child in path:
        cursor = _maker(cursor, label)(**_naming(_load_class(child)))
    return cursor


def _carrier_label(target: str) -> str:
    """The maker label that declares *target* under its parent."""
    return _maker_path(target)[-1][0]


def _carrier_design(target: str) -> Cursor:
    """A design declaring *target* (naming only) with its first curated child.

    A carrier without a child would compile to nothing in ``staged`` mode, so
    every parity check needs one.  Returns the design **root** cursor.
    """
    root = design()
    carrier = _declare(root, target)
    children = _tables().makers.get(target, {})
    assert children, f"carrier {target!r} curates no child maker — nothing to push beneath it"
    label, child = next(iter(children.items()))
    _maker(carrier, label)(**_naming(_load_class(child)))
    return root


# ── Probes: one (carrier, non-naming attribute, valid value) per attribute ────


@dataclass(frozen=True)
class Probe:
    """A non-naming attribute of a carrier that the model accepts with *value*."""

    aci_class: str
    field: str
    wire: str
    value: str

    def __str__(self) -> str:
        return f"{self.aci_class}.{self.field}"


def _probes() -> list[Probe]:
    """Every non-naming field of every carrier that accepts a plain string.

    Fields that take an enum, a number or a bitmask are skipped (a plain
    string is not valid for them); each carrier is guarded separately to keep
    at least one probe.
    """
    probes: list[Probe] = []
    for aci_class in _carriers():
        cls = _load_class(aci_class)
        naming = _naming(cls)
        for field in cls.model_fields:
            if field == "children" or field in cls._naming_props:
                continue
            for candidate in ("ab", "1"):
                try:
                    cls.model_validate({**naming, field: candidate})
                except ValidationError:
                    continue
                probes.append(Probe(aci_class, field, _wire(cls, field), candidate))
                break
    return probes


_PROBES = _probes()


# ── The six doors through which an attribute reaches a carrier node ───────────

Door = Callable[[Probe], object]


def _door_maker_kwargs(probe: Probe) -> object:
    """Maker keyword arguments: ``vmm_provider("VMware", owner_tag=...)``."""
    cls = _load_class(probe.aci_class)
    parent = _untyped(_declare(design(), probe.aci_class, stop_before=True))
    return _maker(parent, _carrier_label(probe.aci_class))(
        **_naming(cls), **{probe.field: probe.value}
    )


def _door_set(probe: Probe) -> object:
    """``.set(...)`` on a naming-only carrier."""
    return _untyped(_declare(design(), probe.aci_class)).set(**{probe.field: probe.value})


def _door_raw_set(probe: Probe) -> object:
    """``.raw_set(...)`` — the wire-name escape on an existing carrier."""
    return _declare(design(), probe.aci_class).raw_set(**{probe.wire: probe.value})


def _door_mo(probe: Probe) -> object:
    """``.mo(Class, ...)`` — the typed escape hatch, from the carrier's parent."""
    cls = _load_class(probe.aci_class)
    parent = _declare(design(), probe.aci_class, stop_before=True)
    return parent.mo(cls, **_naming(cls), **{probe.field: probe.value})


def _door_raw(probe: Probe) -> object:
    """``.raw("Class", ...)`` — the wire-name escape hatch, from the carrier's parent."""
    cls = _load_class(probe.aci_class)
    parent = _declare(design(), probe.aci_class, stop_before=True)
    wire_naming = {_wire(cls, prop): value for prop, value in _naming(cls).items()}
    return parent.raw(probe.aci_class, **wire_naming, **{probe.wire: probe.value})


_DOORS: dict[str, Door] = {
    "maker-kwargs": _door_maker_kwargs,
    "set": _door_set,
    "raw_set": _door_raw_set,
    "mo": _door_mo,
    "raw": _door_raw,
}


def _assert_refusal(error: DesignError, aci_class: str, spellings: set[str]) -> None:
    """The refusal names the class and the attribute, and says why.

    *spellings* are the accepted renderings of the attribute: the readable
    field name or its wire name (``raw()`` translates one into the other
    before the declaration is checked, so either is a faithful report).
    """
    text = str(error)
    lowered = text.lower()
    assert aci_class in text, f"the refusal does not name the class: {text!r}"
    assert any(spelling in text for spelling in spellings), (
        f"the refusal does not name the attribute (any of {sorted(spellings)}): {text!r}"
    )
    assert "carrier" in lowered, f"the refusal does not say the class is a carrier: {text!r}"
    assert "path-only" in lowered, f"the refusal does not say the class is path-only: {text!r}"
    assert "push" in lowered, f"the refusal does not say attributes cannot be pushed: {text!r}"


# ── Guards: the parametrization cannot silently go empty ──────────────────────


class TestVocabularyGuards:
    """The parametrized suites below stand on these premises."""

    def test_the_vocabulary_curates_at_least_one_carrier(self) -> None:
        assert _carriers(), "the vocabulary curates no carrier: every suite below would be empty"
        assert _PROBES, "no carrier exposes a probeable attribute: the refusal suite is empty"

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_every_carrier_can_be_exercised(self, aci_class: str) -> None:
        """A maker chain reaches it, it has a child maker and a probeable attribute."""
        assert _maker_path(aci_class)
        assert _tables().makers.get(aci_class), f"{aci_class} curates no child maker"
        assert [probe for probe in _PROBES if probe.aci_class == aci_class], (
            f"{aci_class} has no non-naming string attribute to probe — "
            "extend _probes() so its refusal stays covered"
        )

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_no_bind_verb_or_sugar_can_reach_a_carrier(self, aci_class: str) -> None:
        """``ref()`` and sugar are not entry paths today — this keeps that true.

        ``ref()`` configures the *relationship* (the Rs object), never the node
        that owns it, and a carrier curates no bind or verb at all.  Sugar
        expands into attributes of its own class, and no carrier has any.  Should
        curation ever change either, the attribute-refusal contract has a new
        door to cover — fail here so it is decided consciously.
        """
        tables = _tables()
        assert not tables.binds.get(aci_class), f"{aci_class} curates binds: ref() can reach it"
        assert not tables.verbs.get(aci_class), f"{aci_class} curates verbs: ref() can reach it"
        assert not tables.sugar.get(aci_class), f"{aci_class} curates sugar: sugar can reach it"


# ── The refusal ───────────────────────────────────────────────────────────────


class TestEveryDoorRefusesAnAttribute:
    """Every door x every non-naming attribute of every carrier."""

    @pytest.mark.parametrize("probe", _PROBES, ids=str)
    @pytest.mark.parametrize("door", sorted(_DOORS))
    def test_refused_at_declaration(self, door: str, probe: Probe) -> None:
        with pytest.raises(DesignError) as caught:
            _DOORS[door](probe)
        _assert_refusal(caught.value, probe.aci_class, {probe.field, probe.wire})


class TestRefusalOnTheUntypedDoor:
    """The base cursor — what a caller reaches around the typed surface — refuses too.

    The generated typed cursors no longer offer an attribute on a carrier (see
    :class:`TestTypedSignatureOfACarrier`), so the runtime refusal is pinned
    through the base :class:`Cursor`, whose makers and ``set()`` take anything.
    """

    def test_a_maker_keyword_is_refused(self) -> None:
        untyped = _untyped(design())
        with pytest.raises(DesignError) as caught:
            untyped.vmm_provider("VMware", owner_tag="orchestrator")
        _assert_refusal(caught.value, "vmmProvP", {"owner_tag", "ownerTag"})

    def test_a_set_is_refused(self) -> None:
        carrier = design().vmm_provider("VMware")
        with pytest.raises(DesignError) as caught:
            _untyped(carrier).set(owner_tag="orchestrator")
        _assert_refusal(caught.value, "vmmProvP", {"owner_tag", "ownerTag"})

    def test_a_set_after_a_child_is_refused(self) -> None:
        """Reaching the carrier again through a held cursor changes nothing."""
        carrier = design().vmm_provider("VMware")
        carrier.vmm_dom("prod")
        with pytest.raises(DesignError) as caught:
            _untyped(carrier).set(annotation="orchestrator:x")
        _assert_refusal(caught.value, "vmmProvP", {"annotation"})

    def test_every_offending_attribute_is_named(self) -> None:
        """Collect-all, in the house style: one refusal lists them all."""
        untyped = _untyped(design())
        with pytest.raises(DesignError) as caught:
            untyped.vmm_provider("VMware", owner_key="k", owner_tag="t")
        text = str(caught.value)
        assert "owner_key" in text or "ownerKey" in text
        assert "owner_tag" in text or "ownerTag" in text


class TestTypedSignatureOfACarrier:
    """The generated typed surface offers a carrier its naming and nothing else.

    A call that always raises must not type-check: the generated maker of a
    carrier takes the naming props only, and its typed cursor's ``set()`` takes
    no attribute at all.  This is the signature layer (``inspect``) of the
    contract; ``tests/typing/test_carrier_static.py`` is the checker layer (a
    generated probe through mypy and pyright).
    """

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_the_typed_maker_takes_the_naming_props_only(self, aci_class: str) -> None:
        cls = _load_class(aci_class)
        parent = _declare(design(), aci_class, stop_before=True)
        assert type(parent) is not Cursor, "the parent is not a generated typed cursor"
        maker = getattr(type(parent), _carrier_label(aci_class))
        parameters = [
            p for name, p in inspect.signature(maker).parameters.items() if name != "self"
        ]
        assert {p.name for p in parameters} == set(cls._naming_props), (
            f"the typed maker of {aci_class} offers {[p.name for p in parameters]}, "
            f"not just its naming props {cls._naming_props}"
        )
        assert not [
            p
            for p in parameters
            if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        ], "the typed maker takes a catch-all"

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_the_typed_cursor_set_takes_no_attribute_keyword(self, aci_class: str) -> None:
        carrier = _declare(design(), aci_class)
        assert type(carrier) is not Cursor, "the carrier cursor is not a generated typed cursor"
        parameters = [
            p
            for name, p in inspect.signature(type(carrier).set).parameters.items()
            if name != "self"
        ]
        assert parameters == [], (
            f"the typed set() of {aci_class} still takes {[p.name for p in parameters]} — "
            "an attribute there can only be refused"
        )


class TestARefusalLeavesNoTrace:
    """A refused declaration changes nothing — the design stays pushable."""

    @pytest.mark.parametrize("probe", _PROBES, ids=str)
    def test_a_refused_maker_does_not_declare_the_carrier(self, probe: Probe) -> None:
        """The retry without the attribute is not a duplicate declaration."""
        cls = _load_class(probe.aci_class)
        parent = _declare(design(), probe.aci_class, stop_before=True)
        label = _carrier_label(probe.aci_class)
        with pytest.raises(DesignError):
            _maker(_untyped(parent), label)(**_naming(cls), **{probe.field: probe.value})
        carrier = _maker(parent, label)(**_naming(cls))
        node = carrier.design_node
        assert node.attrs == {}
        assert node.raw_attrs == {}

    @pytest.mark.parametrize("probe", _PROBES, ids=str)
    def test_a_refused_set_leaves_the_node_untouched(self, probe: Probe) -> None:
        carrier = _declare(design(), probe.aci_class)
        with pytest.raises(DesignError):
            _untyped(carrier).set(**{probe.field: probe.value})
        assert carrier.design_node.attrs == {}
        assert carrier.design_node.raw_attrs == {}

    @pytest.mark.parametrize("probe", _PROBES, ids=str)
    def test_a_refused_raw_set_leaves_the_node_untouched(self, probe: Probe) -> None:
        carrier = _declare(design(), probe.aci_class)
        with pytest.raises(DesignError):
            carrier.raw_set(**{probe.wire: probe.value})
        assert carrier.design_node.attrs == {}
        assert carrier.design_node.raw_attrs == {}

    @pytest.mark.parametrize("probe", _PROBES, ids=str)
    def test_the_payload_after_a_refusal_carries_the_naming_props_only(self, probe: Probe) -> None:
        carrier = _declare(design(), probe.aci_class)
        with pytest.raises(DesignError):
            _untyped(carrier).set(**{probe.field: probe.value})
        attrs = _attrs_by_dn(carrier.to_payload())[carrier.dn]
        assert set(attrs) == set(catalog().class_meta(probe.aci_class).naming)


# ── The nominal path: a naming-only carrier, three modes in agreement ─────────


def _attrs_by_dn(envelope: JsonObject, parent_dn: str = "") -> dict[str, JsonObject]:
    """Flatten a payload envelope into ``{dn: wire attributes}``."""
    ((aci_class, body),) = envelope.items()
    inner = as_object(body)
    attrs = as_object(inner.get("attributes", {}))
    if aci_class == "polUni":
        dn = "uni"
    else:
        rn = catalog().rn_format(aci_class)
        for prop in catalog().class_meta(aci_class).naming:
            rn = rn.replace(f"{{{prop}}}", str(attrs[prop]))
        dn = f"{parent_dn}/{rn}"
    found: dict[str, JsonObject] = {dn: attrs}
    for child in as_objects(inner.get("children", [])):
        found.update(_attrs_by_dn(child, dn))
    return found


def _staged_attrs_by_dn(cfg: Cursor) -> dict[str, JsonObject]:
    """``{dn: wire attributes}`` of every operation ``staged`` would POST."""
    root = cfg.design_node.root()
    found: dict[str, JsonObject] = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        found[op.dn] = as_object(at(body, "attributes"))
    return found


def _stored(fake: FakeApic) -> dict[str, dict[str, str]]:
    """``{dn: attributes}`` of everything the fake holds."""
    return {obj.dn: dict(obj.attributes) for obj in fake.objects()}


def _carrier_dns(cfg: Cursor) -> set[str]:
    """DNs of the design's carrier nodes."""
    carrier = _tables().carrier
    return {node.dn for node in cfg.view() if node.aci_class in carrier}


class TestNamingOnlyCarrier:
    """What still works — and must keep working — once attributes are refused."""

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_a_naming_only_declaration_is_accepted(self, aci_class: str) -> None:
        root = _carrier_design(aci_class)
        carrier = root.view().by_class(aci_class)[0]
        assert carrier.attrs == {}
        assert carrier.raw_attrs == {}
        attrs = _attrs_by_dn(root.to_payload())[carrier.dn]
        assert set(attrs) == set(catalog().class_meta(aci_class).naming)

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_strict_and_staged_carry_the_same_content(self, aci_class: str) -> None:
        """Nothing strict writes is lost by staged — except the carrier's own op."""
        root = _carrier_design(aci_class)
        strict = _attrs_by_dn(root.to_payload())
        staged = _staged_attrs_by_dn(root)
        carriers = _carrier_dns(root)

        assert carriers
        # The polUni root and the carriers have no operation; everything else does.
        assert set(strict) - set(staged) == carriers | {"uni"}
        assert set(staged) <= set(strict)
        for dn, attrs in staged.items():
            assert attrs == strict[dn], dn

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_strict_and_staged_leave_the_same_objects(self, aci_class: str) -> None:
        root = _carrier_design(aci_class)
        carriers = _carrier_dns(root)
        strict_fake, staged_fake = FakeApic(), FakeApic()
        with strict_fake.client() as aci:
            root.push(aci, mode="strict")
        with staged_fake.client() as aci:
            root.push(aci, mode="staged")

        strict, staged = _stored(strict_fake), _stored(staged_fake)
        for dn in carriers:
            # What the strict envelope wrote on the carrier is its naming and nothing else.
            assert set(strict[dn]) == set(catalog().class_meta(aci_class).naming)
        assert {dn: a for dn, a in strict.items() if dn not in carriers} == {
            dn: a for dn, a in staged.items() if dn not in carriers
        }

    @pytest.mark.parametrize("mode", ["strict", "staged"])
    @pytest.mark.parametrize("aci_class", _carriers())
    def test_the_plan_of_an_applied_design_has_no_change(
        self, aci_class: str, mode: Literal["strict", "staged"]
    ) -> None:
        root = _carrier_design(aci_class)
        fake = FakeApic()
        with fake.client() as aci:
            root.push(aci, mode=mode)
            plan = root.push(aci, mode="plan")
        assert not plan.has_changes
        assert plan.creates == []
        assert plan.updates == {}

    @pytest.mark.parametrize("aci_class", _carriers())
    def test_the_plan_of_a_fresh_fabric_creates_the_children_not_the_carrier(
        self, aci_class: str
    ) -> None:
        root = _carrier_design(aci_class)
        with FakeApic().client() as aci:
            plan = root.push(aci, mode="plan")
        assert plan.creates
        assert not set(plan.creates) & _carrier_dns(root)

    def test_the_plan_still_diffs_a_child_of_the_carrier(self) -> None:
        """The plan compares the children — the one thing it was ever meant to."""

        def build(display_name: str) -> Cursor:
            cfg = design()
            cfg.vmm_provider("VMware").vmm_dom("prod", display_name=display_name)
            return cfg

        fake = FakeApic()
        with fake.client() as aci:
            build("Production").push(aci, mode="staged")
            same = build("Production").push(aci, mode="plan")
            drift = build("Staging").push(aci, mode="plan")
        assert not same.has_changes
        assert drift.updates == {
            "uni/vmmp-VMware/dom-prod": {"display_name": ("Production", "Staging")}
        }


class TestCompositionKeepsCarriersAttributeFree:
    """``merge`` / ``slice`` / ``from_payload`` on naming-only carriers."""

    @staticmethod
    def _provider(*domains: str) -> Cursor:
        cfg = design()
        carrier = cfg.vmm_provider("VMware")
        for domain in domains:
            carrier.vmm_dom(domain, display_name=f"{domain}-domain")
        return cfg

    def test_merge_unions_the_children_under_one_naming_only_carrier(self) -> None:
        merged = merge(self._provider("a"), self._provider("b"))
        assert [n.dn for n in merged.view().by_class("vmmDomP")] == [
            "uni/vmmp-VMware/dom-a",
            "uni/vmmp-VMware/dom-b",
        ]
        (carrier,) = merged.view().by_class("vmmProvP")
        assert carrier.attrs == {}
        assert carrier.raw_attrs == {}
        assert set(_attrs_by_dn(merged.to_payload())["uni/vmmp-VMware"]) == {"vendor"}

    def test_a_merged_design_pushes_the_same_in_strict_and_staged(self) -> None:
        merged = merge(self._provider("a"), self._provider("b"))
        strict = _attrs_by_dn(merged.to_payload())
        staged = _staged_attrs_by_dn(merged)
        assert set(strict) - set(staged) == {"uni", "uni/vmmp-VMware"}
        assert all(staged[dn] == strict[dn] for dn in staged)

    def test_slicing_the_carrier_keeps_it_attribute_free(self) -> None:
        sliced = self._provider("a", "b").slice("uni/vmmp-VMware")
        (carrier,) = sliced.view().by_class("vmmProvP")
        assert carrier.attrs == {}
        assert carrier.raw_attrs == {}
        assert len(sliced.view().by_class("vmmDomP")) == 2

    def test_slicing_below_the_carrier_rebuilds_it_bare(self) -> None:
        sliced = self._provider("a", "b").slice("uni/vmmp-VMware/dom-a")
        (carrier,) = sliced.view().by_class("vmmProvP")
        assert carrier.attrs == {}
        assert carrier.raw_attrs == {}
        assert [n.dn for n in sliced.view().by_class("vmmDomP")] == ["uni/vmmp-VMware/dom-a"]

    def test_from_payload_round_trips_a_naming_only_carrier(self) -> None:
        original = self._provider("a")
        clone = from_payload(original.to_payload())
        assert canonical(clone.to_payload()) == canonical(original.to_payload())


# ── The importers: drop and report ────────────────────────────────────────────
#
# DECIDED (owner): DROP + REPORT.
#
# A real capture of a fabric carries CONFIGURABLE NON-NAMING PROPERTIES on a
# carrier.  Measured on the shipped catalogue, not assumed: ``vmmProvP`` is
# ``isExportable`` (so ``snapshot.take`` keeps it — it is the node
# ``uni/vmmp-VMware`` every fabric has) and five of its properties are
# ``isConfigurable`` (``annotation``, ``nameAlias``, ``ownerKey``, ``ownerTag``,
# ``userdom``).  ``take()`` therefore stores all five on every real fabric;
# the importer drops the empty strings (the wire spelling of "not
# configured"), but ``userdom`` is served as ``":all:"`` (standard APIC
# behaviour, not verifiable offline — the sim is unreachable), so it reaches the
# carrier node with a value.
#
# So the importers cannot refuse a carrier's attributes the way a declaration
# does — ``to_design(take(uni))`` would break on every fabric — and they cannot
# keep them either (``staged`` never wrote them, ``plan`` never compared them).
# The contract: ``to_design()`` / ``from_payload()`` import the carrier BARE and
# REPORT each attribute they dropped through a visible, non-blocking channel,
# and never raise for it.  The report is what makes the drop acceptable: the
# fact is not lost silently, the caller can read it, log it, or escalate it.
#
# The channel is ``warn_at_caller(..., DesignHintWarning)`` — the SDK's one
# existing runtime channel for "legal, but you should know" (``niwaki._warn``;
# the resolver's F3744 hint is its only other user).  ``redacted="skip"`` has
# no channel of its own: it drops silently, on explicit opt-in, so there is
# nothing there to reuse.  ``Snapshot.warnings`` is a capture-time record on a
# frozen object the importer is only handed, and ``SnapshotImportError`` is
# blocking by construction.  A warning is non-blocking by default and a CI
# pipeline can escalate it with ``simplefilter("error", DesignHintWarning)``.
#
# What a report entry holds: the class, the DN and the attribute NAME — never
# the value.  The redaction rules say a secure property's value must never be
# echoed; no carrier property is secure today (so the rule cannot be exercised
# without patching tables that several modules bind at import), and a report
# without any value satisfies it by construction, now and for any carrier
# curated later.
#
# Layer 1 below (green on current code, must stay green): a real capture still
# imports and plans to "0 change".  Layer 2 (red on current code): the carrier
# is imported bare and each dropped attribute is reported.

#: What the APIC serves for ``uni/vmmp-VMware`` (every property, halo included).
_RAW_VMM_PROV_P: dict[str, JsonValue] = {
    "annotation": "",
    "childAction": "",
    "dn": "uni/vmmp-VMware",
    "extMngdBy": "",
    "lcOwn": "local",
    "modTs": "2026-01-01T00:00:00.000+00:00",
    "monPolDn": "uni/fabric/monfab-default",
    "name": "",
    "nameAlias": "",
    "ownerKey": "",
    "ownerTag": "",
    "rn": "",
    "status": "",
    "type": "",
    "uid": "0",
    "userdom": ":all:",
    "vendor": "VMware",
}

#: A value no message of the SDK could contain by accident.
_SECRET_LOOKING_VALUE = "s3cr3t-carrier-value"


def _real_capture(*, userdom: str = ":all:") -> Snapshot:
    """A ``uni`` capture holding what ``snapshot.take`` stores for a VMM provider.

    The carrier's attributes go through the very function ``take()`` normalises
    with (configurable props kept, operational halo dropped).  *userdom* stands
    in for the one property a real fabric serves with a value; ``""`` gives the
    all-empty variant.
    """
    attributes = _normalise_attrs(catalog(), "vmmProvP", {**_RAW_VMM_PROV_P, "userdom": userdom})
    tree: JsonObject = {
        "class": "polUni",
        "rn": "uni",
        "attributes": {},
        "children": [
            {
                "class": "vmmProvP",
                "rn": "vmmp-VMware",
                "attributes": attributes,
                "children": [
                    {
                        "class": "vmmDomP",
                        "rn": "dom-prod",
                        "attributes": {"name": "prod"},
                        "children": [],
                    }
                ],
            }
        ],
    }
    return Snapshot(scope="uni", tree=tree)


def _inject(envelope: JsonObject, aci_class: str, extra: Mapping[str, str]) -> bool:
    """Add wire attributes to the first *aci_class* envelope found; ``True`` if found."""
    ((cls, body),) = envelope.items()
    inner = as_object(body)
    if cls == aci_class:
        as_object(inner.setdefault("attributes", {})).update(extra)
        return True
    return any(_inject(child, aci_class, extra) for child in as_objects(inner.get("children", [])))


def _find_envelope(envelope: JsonObject, aci_class: str) -> JsonObject | None:
    """The first envelope of *aci_class* in the payload (depth-first), or ``None``."""
    ((cls, body),) = envelope.items()
    if cls == aci_class:
        return envelope
    for child in as_objects(as_object(body).get("children", [])):
        if (found := _find_envelope(child, aci_class)) is not None:
            return found
    return None


def _carrier_payload(aci_class: str, extra: Mapping[str, str]) -> JsonObject:
    """The payload of a carrier design, with *extra* wire attributes injected on the carrier."""
    payload = _carrier_design(aci_class).to_payload()
    assert _inject(payload, aci_class, extra)
    return payload


def _carrier_dn(aci_class: str) -> str:
    """The DN the carrier occupies in the standard carrier design."""
    return _carrier_design(aci_class).view().by_class(aci_class)[0].dn


def _assert_imported_without_attributes(cfg: Cursor, aci_class: str) -> None:
    """The carrier is in the design, bare, with its children — and pushes as a bare one."""
    (carrier,) = cfg.view().by_class(aci_class)
    assert carrier.attrs == {}
    assert carrier.raw_attrs == {}
    assert carrier.children, "the carrier's children must survive the import"
    assert set(_attrs_by_dn(cfg.to_payload())[carrier.dn]) == set(
        catalog().class_meta(aci_class).naming
    )


@dataclass(frozen=True)
class Imported:
    """An import, with what it reported on the way.

    Attributes:
        design: The root cursor the importer returned.
        reports: Every :class:`~niwaki.exceptions.DesignHintWarning` the import
            raised, as recorded by :func:`warnings.catch_warnings` — the
            channel the importers report dropped carrier attributes through.
    """

    design: Cursor
    reports: list[warnings.WarningMessage]

    @property
    def messages(self) -> list[str]:
        """The text of every report."""
        return [str(report.message) for report in self.reports]


def _import(door: Callable[[], Cursor]) -> Imported:
    """Run an import with every warning recorded; it must neither raise nor warn loudly."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = door()
    return Imported(cfg, [w for w in caught if issubclass(w.category, DesignHintWarning)])


ImportDoor = Callable[[str, Mapping[str, str]], Cursor]


def _import_via_snapshot(aci_class: str, extra: Mapping[str, str]) -> Cursor:
    """``to_design()`` of a full ``uni`` capture whose carrier holds *extra*."""
    payload = _carrier_payload(aci_class, extra)
    return to_design(Snapshot(scope="uni", tree=payload_to_tree(payload)))


def _import_via_scoped_snapshot(aci_class: str, extra: Mapping[str, str]) -> Cursor:
    """``to_design()`` of a capture scoped on the carrier itself."""
    envelope = _find_envelope(_carrier_payload(aci_class, extra), aci_class)
    assert envelope is not None
    return to_design(Snapshot(scope=_carrier_dn(aci_class), tree=payload_to_tree(envelope)))


def _import_via_payload(aci_class: str, extra: Mapping[str, str]) -> Cursor:
    """``from_payload()`` of an envelope whose carrier holds *extra*."""
    return from_payload(_carrier_payload(aci_class, extra))


_IMPORT_DOORS: dict[str, ImportDoor] = {
    "to_design": _import_via_snapshot,
    "to_design-scoped": _import_via_scoped_snapshot,
    "from_payload": _import_via_payload,
}


def _assert_reported(
    imported: Imported, aci_class: str, dn: str, attributes: list[tuple[str, str]]
) -> None:
    """Each ``(field, wire)`` attribute is reported with the class, the DN and its name.

    The importers speak wire names, the model speaks readable ones: either
    spelling is a faithful name for the attribute.
    """
    for field, wire in attributes:
        assert any(
            aci_class in message and dn in message and (field in message or wire in message)
            for message in imported.messages
        ), (
            f"no report names {aci_class} at {dn} with attribute {wire!r} — "
            f"reports: {imported.messages!r}"
        )


class TestImportOfACarrierWithAttributes:
    """A real capture still imports; the carrier comes out bare."""

    def test_premise_a_real_capture_carries_configurable_props_on_the_carrier(self) -> None:
        """The finding itself, pinned: ``take()``'s own normaliser keeps them.

        If this ever fails, the importer question goes away (the schema stopped
        flagging those properties configurable) and this section can go.
        """
        snapshot = _real_capture()
        carrier = as_object(at(snapshot.tree, "children", 0, "attributes"))
        extras = set(carrier) - set(catalog().class_meta("vmmProvP").naming)
        assert extras, "a real capture carries no configurable non-naming prop on vmmProvP"
        assert carrier["userdom"] == ":all:"

    def test_a_real_capture_still_imports(self) -> None:
        """Layer 1 — the invariant itself (green today, must stay green).

        Whatever the importer does with a carrier's attributes, it must not
        refuse the capture: ``to_design(take(uni))`` is the brownfield door.
        """
        imported = _import(lambda: to_design(_real_capture()))
        (carrier,) = imported.design.view().by_class("vmmProvP")
        assert [child.dn for child in carrier.children] == ["uni/vmmp-VMware/dom-prod"]

    def test_the_imported_design_of_a_real_capture_plans_to_zero_change(self) -> None:
        """Layer 1 — the brownfield invariant in miniature (green today, must stay green)."""
        snapshot = _real_capture()
        cfg = _import(lambda: to_design(snapshot)).design
        with FakeApic.from_snapshot(snapshot).client() as aci:
            plan = cfg.push(aci, mode="plan")
        assert not plan.has_changes

    def test_the_carrier_of_a_real_capture_is_imported_bare(self) -> None:
        """Layer 2 — ``userdom=":all:"`` is dropped; the node carries naming only."""
        cfg = _import(lambda: to_design(_real_capture())).design
        _assert_imported_without_attributes(cfg, "vmmProvP")

    @pytest.mark.parametrize("aci_class", _carriers())
    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_every_door_imports_every_carrier_bare(self, door: str, aci_class: str) -> None:
        """Generic form: a configurable value on any carrier, through any door."""
        extra = {p.wire: p.value for p in _PROBES if p.aci_class == aci_class}
        assert extra
        cfg = _import(lambda: _IMPORT_DOORS[door](aci_class, extra)).design
        _assert_imported_without_attributes(cfg, aci_class)


class TestDroppedCarrierAttributesAreReported:
    """Every attribute dropped from a carrier is named — class, DN, attribute."""

    def test_a_real_capture_reports_the_one_attribute_it_dropped(self) -> None:
        imported = _import(lambda: to_design(_real_capture()))
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [("userdom", "userdom")])

    def test_empty_values_are_not_attributes_and_are_not_reported(self) -> None:
        """``annotation=""`` and friends are "not configured": nothing was dropped.

        A real capture carries them on every fabric; reporting them would bury
        the one entry that matters under four that say nothing.
        """
        imported = _import(lambda: to_design(_real_capture()))
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [("userdom", "userdom")])
        not_configured = {
            "annotation",
            "nameAlias",
            "display_name",
            "ownerKey",
            "owner_key",
            "ownerTag",
            "owner_tag",
        }
        for message in imported.messages:
            assert not any(name in message for name in not_configured), message

    @pytest.mark.parametrize("aci_class", _carriers())
    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_every_dropped_attribute_is_reported_through_every_door(
        self, door: str, aci_class: str
    ) -> None:
        probes = [p for p in _PROBES if p.aci_class == aci_class]
        extra = {p.wire: p.value for p in probes}
        imported = _import(lambda: _IMPORT_DOORS[door](aci_class, extra))
        _assert_reported(
            imported, aci_class, _carrier_dn(aci_class), [(p.field, p.wire) for p in probes]
        )

    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_a_meaningful_value_is_dropped_and_reported_like_the_rest(self, door: str) -> None:
        """``ownerTag`` is not the system's halo — it is dropped and reported all the same."""
        imported = _import(lambda: _IMPORT_DOORS[door]("vmmProvP", {"ownerTag": "orchestrator"}))
        _assert_imported_without_attributes(imported.design, "vmmProvP")
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [("owner_tag", "ownerTag")])

    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_a_report_names_the_attribute_but_never_carries_its_value(self, door: str) -> None:
        """The report holds class, DN and name — never a value.

        This is how the redaction rules are honoured by construction: the value
        of a secure property can never appear in a report that holds no value.
        """
        imported = _import(
            lambda: _IMPORT_DOORS[door]("vmmProvP", {"ownerTag": _SECRET_LOOKING_VALUE})
        )
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [("owner_tag", "ownerTag")])
        assert not any(_SECRET_LOOKING_VALUE in message for message in imported.messages)

    @pytest.mark.parametrize("aci_class", _carriers())
    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_a_naming_only_carrier_reports_nothing(self, door: str, aci_class: str) -> None:
        imported = _import(lambda: _IMPORT_DOORS[door](aci_class, {}))
        assert imported.reports == []

    def test_a_capture_with_only_empty_values_reports_nothing(self) -> None:
        imported = _import(lambda: to_design(_real_capture(userdom="")))
        assert imported.reports == []
        _assert_imported_without_attributes(imported.design, "vmmProvP")

    def test_a_report_is_attributed_to_the_caller(self) -> None:
        """Through ``warn_at_caller``: the warning points at the line that imported."""
        imported = _import(lambda: to_design(_real_capture()))
        assert imported.reports
        assert {report.filename for report in imported.reports} == {__file__}

    def test_the_report_can_be_escalated_by_the_caller(self) -> None:
        """Non-blocking by default, strict on demand — the channel's whole point."""
        payload = _carrier_payload("vmmProvP", {"userdom": ":all:"})
        with warnings.catch_warnings():
            warnings.simplefilter("error", DesignHintWarning)
            with pytest.raises(DesignHintWarning, match="vmmProvP"):
                from_payload(payload)


class TestUnknownPropertiesAreMarked:
    """A property the catalogue does not know is dropped *and flagged as such*.

    The importer drops every attribute of a carrier; most are real properties
    the SDK knows (``userdom``), but a capture from a newer firmware can carry
    one the shipped catalogue has never heard of.  The report must not make the
    two look alike: the unknown one reads "unknown to the catalogue" beside its
    name, the known ones are never marked.
    """

    #: Not a property of any class — the catalogue has never heard of it.
    UNKNOWN = "notAProp"
    MARK = "unknown to the catalogue"

    @staticmethod
    def _gap(text: str, marker: str, name: str) -> int:
        """Characters between *marker* and the nearest occurrence of *name* in *text*."""
        m_start = text.index(marker)
        m_end = m_start + len(marker)
        gaps = [
            max(start - m_end, m_start - (start + len(name)), 0)
            for start in (i for i in range(len(text)) if text.startswith(name, i))
        ]
        return min(gaps)

    def test_premise_the_unknown_name_is_unknown(self) -> None:
        for aci_class in _carriers():
            assert self.UNKNOWN not in catalog().class_meta(aci_class).wire_to_readable

    @pytest.mark.parametrize("aci_class", _carriers())
    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_an_unknown_property_is_marked_next_to_its_name(
        self, door: str, aci_class: str
    ) -> None:
        known = next(p for p in _PROBES if p.aci_class == aci_class)
        extra = {known.wire: known.value, self.UNKNOWN: "x"}
        imported = _import(lambda: _IMPORT_DOORS[door](aci_class, extra))

        # Both are dropped and reported — the marking is what tells them apart.
        _assert_reported(
            imported,
            aci_class,
            _carrier_dn(aci_class),
            [(known.field, known.wire), (self.UNKNOWN, self.UNKNOWN)],
        )
        message = next(m for m in imported.messages if self.UNKNOWN in m)
        assert message.count(self.MARK) == 1, (
            f"the unknown property is not marked {self.MARK!r}: {message!r}"
        )
        # The mark sits beside the unknown name, closer to it than to the known one.
        beside_unknown = self._gap(message, self.MARK, self.UNKNOWN)
        assert beside_unknown <= 20, message
        if known.wire in message:
            assert beside_unknown < self._gap(message, self.MARK, known.wire), message

    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_an_unknown_property_alone_is_reported_and_marked(self, door: str) -> None:
        imported = _import(lambda: _IMPORT_DOORS[door]("vmmProvP", {self.UNKNOWN: "x"}))
        _assert_imported_without_attributes(imported.design, "vmmProvP")
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [(self.UNKNOWN, self.UNKNOWN)])
        assert any(self.MARK in message for message in imported.messages)

    @pytest.mark.parametrize("aci_class", _carriers())
    @pytest.mark.parametrize("door", sorted(_IMPORT_DOORS))
    def test_known_properties_are_never_marked(self, door: str, aci_class: str) -> None:
        extra = {p.wire: p.value for p in _PROBES if p.aci_class == aci_class}
        imported = _import(lambda: _IMPORT_DOORS[door](aci_class, extra))
        assert imported.reports or not extra
        assert not any(self.MARK in message for message in imported.messages)

    def test_a_real_capture_marks_nothing(self) -> None:
        """``userdom`` is a real property: reported, never marked."""
        imported = _import(lambda: to_design(_real_capture()))
        _assert_reported(imported, "vmmProvP", "uni/vmmp-VMware", [("userdom", "userdom")])
        assert not any(self.MARK in message for message in imported.messages)


# ── The non-generated branch of the refusal ───────────────────────────────────
#
# Every curated carrier today (``vmmProvP``) has a generated model, so
# ``raw()``'s second route — the one for a class that exists in the catalogue
# only — would never meet a carrier.  It is still a door (the vocabulary may
# curate a catalogue-only carrier tomorrow), and untested code is code that
# rots: the tests below curate a carrier *synthetically*.
#
# The seam is ``niwaki.design._cursor._tables``: the one function the cursor
# reads the vocabulary through (``_refuse_carrier_attrs`` included, and
# ``compile_ops``, which imports it at call time).  The fixture swaps it for a
# vocabulary whose ``carrier`` set also holds the synthetic class, and
# ``monkeypatch`` puts the real one back — no module state survives the test.


@cache
def _non_generated_class() -> tuple[str, str, str]:
    """A catalogue-only class that can sit directly under ``uni`` and has an attribute.

    Returns:
        ``(aci_class, naming wire prop, attribute wire prop)`` of the first such
        class in name order — one simple naming prop (no bracketed value), one
        configurable non-naming property, a ``uni/<rn>`` DN, containment under
        ``polUni`` provable (so ``raw()`` reaches the catalogue route).

    Raises:
        AssertionError: The catalogue has no such class.
    """
    reader = catalog()
    for aci_class in sorted(reader._concrete_classes()):  # pyright: ignore[reportPrivateUsage]
        if aci_class in CLASS_PKG:
            continue
        try:
            formats = reader.dn_formats(aci_class)
        except KeyError:
            continue
        if not any(f.count("/") == 1 and f.startswith("uni/") for f in formats):
            continue
        naming = sorted(reader.class_meta(aci_class).naming)
        if len(naming) != 1 or "[" in reader.rn_format(aci_class):
            continue
        attributes = sorted(
            wire
            for wire, flags in reader.prop_flags(aci_class).items()
            if flags.is_configurable and wire not in naming
        )
        if attributes and _class_may_contain("polUni", aci_class):
            return aci_class, naming[0], attributes[0]
    raise AssertionError("the catalogue holds no non-generated class fit for the synthetic carrier")


@pytest.fixture
def synthetic_carrier(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """Curate :func:`_non_generated_class` as a carrier — for this test only."""
    aci_class, naming, attribute = _non_generated_class()
    real = _tables()
    synthetic = real._replace(carrier=real.carrier | {aci_class})
    monkeypatch.setattr("niwaki.design._cursor._tables", lambda: synthetic)
    return aci_class, naming, attribute


class TestANonGeneratedCarrier:
    """``raw()`` / ``raw_set()`` on a carrier that has no generated model."""

    def test_the_synthetic_class_is_catalogue_only(self) -> None:
        aci_class, _naming_prop, _attribute = _non_generated_class()
        assert aci_class not in CLASS_PKG
        assert aci_class not in _tables().carrier

    def test_the_seam_swaps_the_vocabulary_and_restores_it(
        self, synthetic_carrier: tuple[str, str, str]
    ) -> None:
        aci_class, _naming_prop, _attribute = synthetic_carrier
        assert aci_class in cursor_module._tables().carrier  # pyright: ignore[reportPrivateUsage]

    def test_the_seam_is_restored_after_the_test(self) -> None:
        aci_class, _naming_prop, _attribute = _non_generated_class()
        assert aci_class not in cursor_module._tables().carrier  # pyright: ignore[reportPrivateUsage]

    def test_without_the_seam_the_same_class_takes_attributes(self) -> None:
        """Control: the refusal below comes from the carrier set, nothing else."""
        aci_class, naming, attribute = _non_generated_class()
        cursor = design().raw(aci_class, **{naming: "x", attribute: "x"})
        assert cursor.design_node.raw_attrs == {attribute: "x"}

    def test_an_attribute_is_refused_at_declaration(
        self, synthetic_carrier: tuple[str, str, str]
    ) -> None:
        aci_class, naming, attribute = synthetic_carrier
        with pytest.raises(DesignError) as caught:
            design().raw(aci_class, **{naming: "x", attribute: "x"})
        _assert_refusal(caught.value, aci_class, {attribute})

    def test_the_refusal_leaves_no_trace(self, synthetic_carrier: tuple[str, str, str]) -> None:
        """The retry without the attribute is not a duplicate declaration."""
        aci_class, naming, attribute = synthetic_carrier
        root = design()
        with pytest.raises(DesignError):
            root.raw(aci_class, **{naming: "x", attribute: "x"})
        assert root.view().by_class(aci_class) == ()
        cursor = root.raw(aci_class, **{naming: "x"})
        assert cursor.design_node.raw_attrs == {}

    def test_a_naming_only_declaration_is_accepted(
        self, synthetic_carrier: tuple[str, str, str]
    ) -> None:
        aci_class, naming, _attribute = synthetic_carrier
        root = design()
        cursor = root.raw(aci_class, **{naming: "x"})
        node = cursor.design_node
        assert isinstance(node, RawDesignNode), "the catalogue route builds a raw node"
        assert node.raw_attrs == {}
        carrier = _attrs_by_dn(root.to_payload())[node.dn()]
        assert carrier == {naming: "x"}

    def test_the_synthetic_carrier_emits_no_staged_operation(
        self, synthetic_carrier: tuple[str, str, str]
    ) -> None:
        """The seam is real: the engine treats the class as a carrier too."""
        aci_class, naming, _attribute = synthetic_carrier
        root = design()
        root.raw(aci_class, **{naming: "x"})
        assert compile_ops(root.design_node.root(), resolve(root.design_node.root())) == []

    def test_raw_set_is_refused_too(self, synthetic_carrier: tuple[str, str, str]) -> None:
        aci_class, naming, attribute = synthetic_carrier
        cursor = design().raw(aci_class, **{naming: "x"})
        with pytest.raises(DesignError) as caught:
            cursor.raw_set(**{attribute: "x"})
        _assert_refusal(caught.value, aci_class, {attribute})
        assert cursor.design_node.raw_attrs == {}
