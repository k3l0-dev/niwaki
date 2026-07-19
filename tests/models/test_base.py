"""Tests for ManagedObject base class.

Covers: REGISTRY, rn/dn, children serialisation, to_apic, from_apic.
Uses minimal helper classes (SimpleMO / SimpleChild) to keep tests
independent of generated code, then verifies end-to-end with fvBD.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError

from niwaki.models.base import REGISTRY, ManagedObject, _coerce_apic_value

# ── Minimal helper classes ────────────────────────────────────────────────────
# Defined at module level so they are registered in REGISTRY exactly once.
# ClassVar is required: Pydantic v2 strips underscore-prefixed annotations
# that are not declared as ClassVar, causing the base class defaults to win.


class SimpleMO(ManagedObject):
    """Minimal MO with a naming prop and two optional fields."""

    _aci_class: ClassVar[str] = "simpleMO"
    _rn_format: ClassVar[str] = "mo-{name}"
    _naming_props: ClassVar[list[str]] = ["name"]
    _contains: ClassVar[frozenset[str]] = frozenset({"simpleChild"})

    name: str
    descr: str = ""
    active: bool = True


class SimpleChild(ManagedObject):
    """Minimal child MO with a numeric-style naming prop."""

    _aci_class: ClassVar[str] = "simpleChild"
    _rn_format: ClassVar[str] = "ch-{id}"
    _naming_props: ClassVar[list[str]] = ["id"]
    _contains: ClassVar[frozenset[str]] = frozenset()

    id: str


class NoNamingMO(ManagedObject):
    """MO with no naming props (e.g. singleton relation objects)."""

    _aci_class: ClassVar[str] = "noNamingMO"
    _rn_format: ClassVar[str] = "fixed-rn"
    _naming_props: ClassVar[list[str]] = []
    _contains: ClassVar[frozenset[str]] = frozenset()

    target: str = ""


# ── REGISTRY ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_classes_are_registered_on_definition(self) -> None:
        assert "simpleMO" in REGISTRY
        assert "simpleChild" in REGISTRY
        assert "noNamingMO" in REGISTRY

    def test_registry_maps_to_correct_class(self) -> None:
        assert REGISTRY["simpleMO"] is SimpleMO
        assert REGISTRY["simpleChild"] is SimpleChild

    def test_generated_fvbd_is_registered(self) -> None:
        # Importing the generated module triggers registration
        from niwaki.models._generated.fv.fvBD import fvBD

        assert "fvBD" in REGISTRY
        assert REGISTRY["fvBD"] is fvBD

    def test_base_managedobj_is_not_registered(self) -> None:
        # Empty _aci_class → not registered
        assert "" not in REGISTRY


# ── RN / DN ───────────────────────────────────────────────────────────────────


class TestRnDn:
    def test_rn_with_naming_prop(self) -> None:
        mo = SimpleMO(name="prod")
        assert mo.rn == "mo-prod"

    def test_rn_no_naming_props(self) -> None:
        mo = NoNamingMO()
        assert mo.rn == "fixed-rn"

    def test_rn_updates_when_name_changes(self) -> None:
        mo = SimpleMO(name="initial")
        mo.name = "updated"
        assert mo.rn == "mo-updated"


# ── to_apic ───────────────────────────────────────────────────────────────────


class TestToApic:
    def test_naming_prop_always_included(self) -> None:
        mo = SimpleMO(name="prod")
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert attrs["name"] == "prod"

    def test_only_explicit_fields_sent(self) -> None:
        mo = SimpleMO(name="prod")  # descr and active not set explicitly
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert "descr" not in attrs
        assert "active" not in attrs

    def test_explicit_optional_field_included(self) -> None:
        mo = SimpleMO(name="prod", descr="hello")
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert attrs["descr"] == "hello"

    def test_bool_true_serialized_as_string(self) -> None:
        mo = SimpleMO(name="x", active=True)
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert attrs["active"] == "true"

    def test_bool_false_serialized_as_string(self) -> None:
        mo = SimpleMO(name="x", active=False)
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert attrs["active"] == "false"

    def test_children_not_in_attributes(self) -> None:
        mo = SimpleMO(name="x")
        mo.children.append(SimpleChild(id="c1"))
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert "children" not in attrs

    def test_children_serialized_under_children_key(self) -> None:
        mo = SimpleMO(name="x")
        mo.children.append(SimpleChild(id="c1"))
        payload = mo.to_apic()["simpleMO"]
        assert "children" in payload
        assert payload["children"][0]["simpleChild"]["attributes"]["id"] == "c1"

    def test_no_children_key_when_empty(self) -> None:
        mo = SimpleMO(name="x")
        payload = mo.to_apic()["simpleMO"]
        assert "children" not in payload

    def test_apic_extra_fields_excluded(self) -> None:
        # Simulate reading from APIC: contains extra read-only fields
        mo = SimpleMO.model_validate(
            {
                "name": "prod",
                "descr": "hello",
                "modTs": "2024-01-01T00:00:00.000+00:00",  # APIC read-only
                "uid": "42",
            }
        )
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert "modTs" not in attrs
        assert "uid" not in attrs

    def test_envelope_key_matches_aci_class(self) -> None:
        mo = SimpleMO(name="x")
        assert "simpleMO" in mo.to_apic()


# ── from_apic ─────────────────────────────────────────────────────────────────


class TestFromApic:
    def test_basic_deserialization(self) -> None:
        raw = {"simpleMO": {"attributes": {"name": "prod", "descr": "test"}}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, SimpleMO)
        assert obj.name == "prod"
        assert obj.descr == "test"

    def test_unknown_class_falls_back_to_managedobj(self) -> None:
        raw = {"unknownClass": {"attributes": {"name": "x"}}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, ManagedObject)

    def test_children_are_recursively_deserialized(self) -> None:
        raw = {
            "simpleMO": {
                "attributes": {"name": "parent"},
                "children": [{"simpleChild": {"attributes": {"id": "c1"}}}],
            }
        }
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, SimpleMO)
        assert len(obj.children) == 1
        assert isinstance(obj.children[0], SimpleChild)
        assert obj.children[0].id == "c1"

    def test_no_children_key_gives_empty_list(self) -> None:
        raw = {"simpleMO": {"attributes": {"name": "x"}}}
        obj = ManagedObject.from_apic(raw)
        assert obj.children == []

    def test_missing_attributes_returns_object_without_raising(self) -> None:
        # from_apic uses model_construct (no validation) — missing required fields
        # don't raise; the object is created but the field is simply unset.
        raw = {"simpleMO": {}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, SimpleMO)

    def test_missing_attributes_ok_for_optional_only_class(self) -> None:
        # NoNamingMO has no required fields → empty attributes dict is valid
        raw = {"noNamingMO": {}}
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, NoNamingMO)


# ── Generated class smoke tests ───────────────────────────────────────────────


class TestGeneratedFvBD:
    """Smoke tests: verify the generated fvBD class works end-to-end."""

    def test_fvbd_requires_name(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValidationError):
            fvBD()  # type: ignore[reportCallIssue]  # name is required

    def test_fvbd_name_min_length(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValidationError):
            fvBD(name="")

    def test_fvbd_name_max_length(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValidationError):
            fvBD(name="x" * 65)

    def test_fvbd_invalid_enum(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        with pytest.raises(ValidationError):
            fvBD(name="bd1", multiDstPktAct="not-a-valid-value")  # type: ignore[reportArgumentType]

    def test_fvbd_defaults_are_correct(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD(name="web")
        assert bd.unicast_routing is True
        assert bd.arp_flooding is False
        assert bd.multi_destination_packet_action == "bd-flood"
        assert bd.unknown_mac_unicast_action == "proxy"

    def test_fvbd_rn(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD(name="my-bd")
        assert bd.rn == "BD-my-bd"

    def test_fvbd_to_apic_surgical(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD(name="web", unicast_routing=True)
        attrs = bd.to_apic()["fvBD"]["attributes"]
        # Only explicitly-set fields sent
        assert attrs["name"] == "web"
        assert attrs["unicastRoute"] == "true"
        # Defaults NOT sent (not in model_fields_set)
        assert "arpFlood" not in attrs
        assert "multiDstPktAct" not in attrs

    def test_fvbd_from_apic_roundtrip(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        raw = {
            "fvBD": {
                "attributes": {
                    "name": "web",
                    "unicastRoute": "yes",
                    "arpFlood": "no",
                    "multiDstPktAct": "bd-flood",
                }
            }
        }
        obj = ManagedObject.from_apic(raw)
        assert isinstance(obj, fvBD)
        assert obj.name == "web"


# ── to_apic() value filtering ─────────────────────────────────────────────────


class TestToApicValueFiltering:
    def test_none_values_are_skipped(self) -> None:
        mo = SimpleMO.surgical({"name": "x"}, descr=None)
        assert "descr" not in mo.to_apic()["simpleMO"]["attributes"]

    def test_empty_string_non_naming_is_skipped(self) -> None:
        """Sending "" would silently erase the APIC value — never emitted."""
        mo = SimpleMO.surgical({"name": "x"}, descr="")
        assert "descr" not in mo.to_apic()["simpleMO"]["attributes"]

    def test_empty_naming_prop_is_kept(self) -> None:
        """Naming props are always sent, even empty (the APIC requires them)."""
        mo = SimpleMO.surgical({"name": ""})
        assert mo.to_apic()["simpleMO"]["attributes"] == {"name": ""}


# ── _coerce_apic_value edge branches ──────────────────────────────────────────


class TestCoerceApicValue:
    def test_none_passthrough(self) -> None:
        assert _coerce_apic_value(bool, None) is None

    def test_bool_passthrough(self) -> None:
        assert _coerce_apic_value(bool, True) is True

    def test_falsy_string_to_false(self) -> None:
        assert _coerce_apic_value(bool, "no") is False

    def test_int_string_coerced(self) -> None:
        assert _coerce_apic_value(int, "42") == 42

    def test_non_numeric_int_string_left_alone(self) -> None:
        assert _coerce_apic_value(int, "not-a-number") == "not-a-number"


# ── Surgical fields-set semantics ─────────────────────────────────────────────


class TestFieldsSetSemantics:
    def test_direct_assignment_marks_field_as_set(self) -> None:
        """Pydantic v2 adds assigned fields to model_fields_set — no helper needed."""
        raw = {"simpleMO": {"attributes": {"name": "x", "descr": "hello", "active": "yes"}}}
        mo = ManagedObject.from_apic(raw)
        assert isinstance(mo, SimpleMO)
        assert "descr" not in mo.to_apic()["simpleMO"]["attributes"]
        mo.descr = "world"
        assert mo.to_apic()["simpleMO"]["attributes"]["descr"] == "world"

    def test_from_apic_excludes_non_naming_from_to_apic(self) -> None:
        raw = {"simpleMO": {"attributes": {"name": "x", "descr": "hello"}}}
        mo = ManagedObject.from_apic(raw)
        attrs = mo.to_apic()["simpleMO"]["attributes"]
        assert "name" in attrs
        assert "descr" not in attrs  # not in model_fields_set — surgical POST


# ── ManagedObject.surgical() (R1) ────────────────────────────────────────────


class TestSurgical:
    def test_naming_prop_always_present(self) -> None:
        delta = SimpleMO.surgical({"name": "x"})
        attrs = delta.to_apic()["simpleMO"]["attributes"]
        assert attrs["name"] == "x"

    def test_change_included_in_payload(self) -> None:
        delta = SimpleMO.surgical({"name": "x"}, descr="hello")
        attrs = delta.to_apic()["simpleMO"]["attributes"]
        assert attrs["descr"] == "hello"

    def test_unchanged_field_not_in_payload(self) -> None:
        delta = SimpleMO.surgical({"name": "x"}, descr="hello")
        attrs = delta.to_apic()["simpleMO"]["attributes"]
        assert "active" not in attrs

    def test_multiple_changes_all_included(self) -> None:
        delta = SimpleMO.surgical({"name": "x"}, descr="a", active=False)
        attrs = delta.to_apic()["simpleMO"]["attributes"]
        assert attrs["descr"] == "a"
        assert attrs["active"] == "false"
        assert attrs["name"] == "x"

    def test_no_changes_only_naming_in_payload(self) -> None:
        delta = SimpleMO.surgical({"name": "y"})
        attrs = delta.to_apic()["simpleMO"]["attributes"]
        assert list(attrs.keys()) == ["name"]

    def test_model_fields_set_is_naming_plus_changes(self) -> None:
        delta = SimpleMO.surgical({"name": "z"}, descr="hi")
        assert delta.model_fields_set == {"name", "descr"}

    def test_equivalent_to_manual_model_construct(self) -> None:
        manual = SimpleMO.model_construct(
            _fields_set={"name", "descr"},
            name="x",
            descr="hello",
        )
        auto = SimpleMO.surgical({"name": "x"}, descr="hello")
        assert manual.to_apic() == auto.to_apic()

    def test_works_with_generated_fvbd(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        delta = fvBD.surgical({"name": "web"}, arp_flooding=True)
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert attrs["name"] == "web"
        assert attrs["arpFlood"] == "true"
        assert "unicastRoute" not in attrs


# ── Uniform read access (.dn / __getitem__ / .attrs) ──────────────────────────


def _read_simple() -> SimpleMO:
    """A SimpleMO as it comes back from a read: config props + APIC-only attrs."""
    return SimpleMO.from_apic(
        {"simpleMO": {"attributes": {"name": "x", "descr": "hi", "dn": "mo-x", "modTs": "2026"}}}
    )  # type: ignore[return-value]


class TestDnAccessor:
    def test_dn_from_read_result(self) -> None:
        assert _read_simple().dn == "mo-x"

    def test_dn_from_generated_read_result(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "dn": "uni/tn-p/BD-web"}}})
        assert bd.dn == "uni/tn-p/BD-web"

    def test_dn_from_unregistered_read_result(self) -> None:
        top = ManagedObject.from_apic(
            {"topSystem": {"attributes": {"dn": "topology/pod-1/node-101", "role": "leaf"}}}
        )
        assert top.dn == "topology/pod-1/node-101"

    def test_dn_absent_on_local_object_raises(self) -> None:
        with pytest.raises(AttributeError, match="constructed locally"):
            _ = SimpleMO(name="x").dn


class TestGetItem:
    def test_extra_attribute(self) -> None:
        mo = _read_simple()
        assert mo["dn"] == "mo-x"
        assert mo["modTs"] == "2026"

    def test_non_renamed_field(self) -> None:
        assert _read_simple()["name"] == "x"

    def test_renamed_field_by_wire_name_returns_typed_value(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}})
        # Addressed by the wire name, but the value is the typed (coerced) one.
        assert bd["arpFlood"] is True

    def test_unknown_key_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            _ = _read_simple()["nope"]

    def test_non_string_key_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="wire attribute name"):
            _ = _read_simple()[0]  # type: ignore[index]


class TestAttrsView:
    def test_wire_view_merges_fields_and_extras(self) -> None:
        attrs = _read_simple().attrs
        assert attrs["name"] == "x"
        assert attrs["descr"] == "hi"
        assert attrs["dn"] == "mo-x"
        assert attrs["modTs"] == "2026"

    def test_reflects_full_object_state(self) -> None:
        # A generated object read from the APIC carries every field, so the wire
        # view is the object's whole state (config fields at their current
        # values), not only the ones that differ from a default.
        attrs = _read_simple().attrs
        assert attrs["active"] == "true"  # a config field at its default value

    def test_renamed_field_uses_wire_name_and_wire_value(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        bd = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}})
        attrs = bd.attrs
        assert attrs["arpFlood"] == "true"  # bool wire form, under the wire name
        assert attrs["unicastRoute"] == "true"  # config fields render under wire names

    def test_unregistered_class_view_is_all_extras(self) -> None:
        top = ManagedObject.from_apic(
            {"topSystem": {"attributes": {"dn": "topology/pod-1/node-101", "role": "leaf"}}}
        )
        assert top.attrs == {"dn": "topology/pod-1/node-101", "role": "leaf"}

    def test_children_excluded(self) -> None:
        raw = {
            "simpleMO": {
                "attributes": {"name": "x"},
                "children": [{"simpleChild": {"attributes": {"id": "1"}}}],
            }
        }
        mo = ManagedObject.from_apic(raw)
        assert mo.children  # the child was parsed
        assert "children" not in mo.attrs


class TestIterationSentinel:
    """Adding ``__getitem__`` must not turn the model into an old-style iterable
    (Pydantic already defines ``__iter__``, which yields ``(name, value)``)."""

    def test_iter_still_yields_field_tuples(self) -> None:
        items = dict(iter(SimpleMO(name="x", descr="hi")))
        assert items["name"] == "x"
        assert items["descr"] == "hi"
