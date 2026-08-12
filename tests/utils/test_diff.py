"""Tests for niwaki.utils.diff.

Covers: mo_diff — nominal delta, no change, type mismatch, naming prop mismatch,
partial change, bool serialisation in result, extra fields ignored.
"""

from __future__ import annotations

import pytest

from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvCtx import fvCtx
from niwaki.models._generated.fv.fvSubnet import fvSubnet
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.utils.diff import mo_diff


def _current_bd(**attrs: str) -> fvBD:
    """Build an fvBD instance simulating a deserialized APIC GET response."""
    return fvBD.model_validate(attrs)


# ── Nominal delta ─────────────────────────────────────────────────────────────


class TestMoDiffNominal:
    def test_single_bool_changed(self) -> None:
        desired = fvBD(name="web", unicast_routing=False)
        current = _current_bd(name="web", unicastRoute="yes")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert attrs["unicastRoute"] == "false"
        assert attrs["name"] == "web"

    def test_single_enum_changed(self) -> None:
        desired = fvBD(name="web", multi_destination_packet_action="drop")  # type: ignore[reportArgumentType]
        current = _current_bd(name="web", multiDstPktAct="bd-flood")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert attrs["multiDstPktAct"] == "drop"

    def test_multiple_fields_changed(self) -> None:
        desired = fvBD(name="web", unicast_routing=False, arp_flooding=True)
        current = _current_bd(name="web", unicastRoute="yes", arpFlood="no")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert attrs["unicastRoute"] == "false"
        assert attrs["arpFlood"] == "true"

    def test_unchanged_fields_excluded_from_delta(self) -> None:
        desired = fvBD(name="web", unicast_routing=True)  # same as default
        current = _current_bd(name="web", unicastRoute="yes")
        delta = mo_diff(desired, current)
        # unicastRoute is the same → no diff
        assert delta is None

    def test_naming_prop_always_in_delta(self) -> None:
        desired = fvBD(name="web", arp_flooding=True)
        current = _current_bd(name="web", arpFlood="no")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert "name" in attrs

    def test_delta_to_apic_only_sends_naming_plus_changed(self) -> None:
        desired = fvBD(name="web", unicast_routing=False)
        current = _current_bd(name="web", unicastRoute="yes", arpFlood="no")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        # Only name (naming) + unicastRoute (changed) should be present
        assert set(attrs.keys()) == {"name", "unicastRoute"}


# ── No change ─────────────────────────────────────────────────────────────────


class TestMoDiffNoChange:
    def test_identical_objects_return_none(self) -> None:
        desired = fvBD(name="web", unicast_routing=True)
        current = _current_bd(name="web", unicastRoute="yes")
        assert mo_diff(desired, current) is None

    def test_same_instance_returns_none(self) -> None:
        bd = fvBD(name="web")
        assert mo_diff(bd, bd) is None

    def test_only_naming_props_identical(self) -> None:
        # No optional fields set explicitly on desired — compare against defaults
        desired = fvBD(name="web")
        current = _current_bd(name="web", unicastRoute="yes", arpFlood="no")
        # desired has unicastRoute=True (default), current "yes" → True: no diff
        assert mo_diff(desired, current) is None


# ── Error cases ───────────────────────────────────────────────────────────────


class TestMoDiffErrors:
    def test_different_types_raises_type_error(self) -> None:
        bd = fvBD(name="web")
        tenant = fvTenant(name="prod")
        with pytest.raises(TypeError, match="classes must match"):
            mo_diff(bd, tenant)  # type: ignore[arg-type]

    def test_naming_prop_mismatch_raises_value_error(self) -> None:
        desired = fvBD(name="web")
        current = _current_bd(name="db")  # different object
        with pytest.raises(ValueError, match="naming prop"):
            mo_diff(desired, current)

    def test_different_subclasses_raises_type_error(self) -> None:
        bd = fvBD(name="web")
        ctx = fvCtx(name="main")
        with pytest.raises(TypeError):
            mo_diff(bd, ctx)  # type: ignore[arg-type]


# ── APIC extra fields are ignored ─────────────────────────────────────────────


class TestMoDiffIgnoresExtra:
    def test_apic_readonly_fields_not_compared(self) -> None:
        desired = fvBD(name="web", unicast_routing=True)
        # current has read-only APIC fields — a READ absorbs them tolerantly
        # (a direct constructor with foreign keys now fails loud by design)
        current = fvBD.from_apic(
            {
                "fvBD": {
                    "attributes": {
                        "name": "web",
                        "unicastRoute": "yes",
                        "modTs": "2024-01-01T00:00:00.000+00:00",
                        "uid": "15374",
                    }
                }
            }
        )
        # Diff should be None because unicastRoute is the same
        assert mo_diff(desired, current) is None

    def test_recurse_children_false_ignores_children(self) -> None:
        from niwaki.models._generated.fv.fvSubnet import fvSubnet

        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web"})
        # recurse_children=False → children ignored, no scalar change → None
        assert mo_diff(desired, current, recurse_children=False) is None

    def test_extra_fields_on_desired_do_not_appear_in_delta(self) -> None:
        # Even if desired has extra fields from a prior GET, they are not
        # compared (the GET path is from_apic — tolerant by design)
        desired = fvBD.from_apic(
            {"fvBD": {"attributes": {"name": "web", "unicastRoute": "no", "modTs": "old"}}}
        )
        current = _current_bd(name="web", unicastRoute="yes")
        delta = mo_diff(desired, current)
        assert delta is not None
        attrs = delta.to_apic()["fvBD"]["attributes"]
        assert "modTs" not in attrs


# ── Recursive children diff (P1.7) ────────────────────────────────────────────


class TestMoDiffChildren:
    """mo_diff with recurse_children=True (default)."""

    def test_changed_child_appears_in_delta(self) -> None:
        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web"})
        current.children.append(fvSubnet.model_validate({"ip": "10.0.0.1/24", "scope": "private"}))

        delta = mo_diff(desired, current)
        assert delta is not None
        assert len(delta.children) == 1
        child_attrs = delta.children[0].to_apic()["fvSubnet"]["attributes"]
        assert child_attrs["scope"] == "public"

    def test_new_child_in_desired_is_included_in_full(self) -> None:
        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web"})
        # current has no children

        delta = mo_diff(desired, current)
        assert delta is not None
        assert len(delta.children) == 1

    def test_unchanged_child_not_in_delta(self) -> None:
        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web"})
        current.children.append(fvSubnet.model_validate({"ip": "10.0.0.1/24", "scope": "public"}))

        delta = mo_diff(desired, current)
        # No scalar diff, no child diff → None
        assert delta is None

    def test_child_in_current_only_is_ignored(self) -> None:
        desired = fvBD(name="web")
        # desired has no children
        current = fvBD.model_validate({"name": "web"})
        current.children.append(fvSubnet.model_validate({"ip": "10.0.0.1/24", "scope": "public"}))

        # mo_diff does not produce DELETEs — current-only children are ignored
        delta = mo_diff(desired, current)
        assert delta is None

    def test_no_child_diff_returns_none_when_scalar_also_unchanged(self) -> None:
        desired = fvBD(name="web")
        current = fvBD.model_validate({"name": "web"})
        assert mo_diff(desired, current) is None

    def test_scalar_diff_with_unchanged_children(self) -> None:
        desired = fvBD(name="web", arp_flooding=True)
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web", "arpFlood": "no"})
        current.children.append(fvSubnet.model_validate({"ip": "10.0.0.1/24", "scope": "public"}))

        delta = mo_diff(desired, current)
        assert delta is not None
        assert delta.to_apic()["fvBD"]["attributes"]["arpFlood"] == "true"
        # Unchanged child must NOT appear in the delta
        assert len(delta.children) == 0

    def test_catalogue_served_children_keep_distinct_identities(self) -> None:
        """Children with no generated model must not collapse onto one key.

        The old key read the ClassVar ``_aci_class`` (empty on the base class)
        and the empty ``_naming_props``, so every operational child keyed as
        ``("",)``: two identical trees whose children were two *different*
        unregistered classes crashed with a cross-class TypeError, and
        same-class siblings diffed against each other's attributes.
        """
        from niwaki.models.base import ManagedObject

        def _tree() -> ManagedObject:
            return ManagedObject.from_apic(
                {
                    "fvTenant": {
                        "attributes": {"name": "prod", "dn": "uni/tn-prod"},
                        "children": [
                            {"opClsA": {"attributes": {"dn": "uni/tn-prod/opA-x", "p": "1"}}},
                            {"opClsB": {"attributes": {"dn": "uni/tn-prod/opB-y", "p": "2"}}},
                        ],
                    }
                }
            )

        assert mo_diff(_tree(), _tree()) is None  # used to raise TypeError

    def test_a_new_same_class_operational_sibling_is_not_swallowed(self) -> None:
        """The collapsed key hid new children behind their siblings.

        Under the old ``("",)`` key the current-children index held a single
        entry for ALL operational children, so a desired child that exists on
        no current object still found a "match" and vanished from the delta —
        a wrong verdict, not just a crash. Keyed on ``(class, rn)`` it now
        surfaces as the new child it is.
        """
        from niwaki.models.base import ManagedObject

        def _tree(names: list[str]) -> ManagedObject:
            return ManagedObject.from_apic(
                {
                    "fvTenant": {
                        "attributes": {"name": "prod", "dn": "uni/tn-prod"},
                        "children": [
                            {"opCls": {"attributes": {"dn": f"uni/tn-prod/op-{n}"}}} for n in names
                        ],
                    }
                }
            )

        assert mo_diff(_tree(["a", "b"]), _tree(["a", "b"])) is None
        delta = mo_diff(_tree(["a", "b", "c"]), _tree(["a", "b"]))
        assert delta is not None
        assert [c.attrs.get("dn") for c in delta.children] == ["uni/tn-prod/op-c"]

    def test_recurse_children_false_ignores_all_child_changes(self) -> None:
        desired = fvBD(name="web")
        desired.children.append(fvSubnet(subnet="10.0.0.1/24", scope="public"))
        current = fvBD.model_validate({"name": "web"})
        current.children.append(fvSubnet.model_validate({"ip": "10.0.0.1/24", "scope": "private"}))

        delta = mo_diff(desired, current, recurse_children=False)
        assert delta is None  # scalar unchanged, children ignored


class TestSecureProps:
    """Write-only props never count as drift — the APIC never echoes them."""

    def test_secure_prop_is_skipped(self) -> None:
        from niwaki.models._generated.fv.fvKeyPol import fvKeyPol

        desired = fvKeyPol(key_id=1, name="rollover", pre_shared_key="s3cr3t")
        current = fvKeyPol.model_validate({"id": "1", "name": "rollover"})
        assert mo_diff(desired, current) is None

    def test_secure_prop_is_skipped_in_fields_set_mode(self) -> None:
        from niwaki.models._generated.fv.fvKeyPol import fvKeyPol

        desired = fvKeyPol(key_id=1, pre_shared_key="s3cr3t")
        current = fvKeyPol.model_validate({"id": "1"})
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_non_secure_changes_still_diff(self) -> None:
        from niwaki.models._generated.fv.fvKeyPol import fvKeyPol

        desired = fvKeyPol(key_id=1, name="rotated", pre_shared_key="s3cr3t")
        current = fvKeyPol.model_validate({"id": "1", "name": "rollover"})
        delta = mo_diff(desired, current)
        assert delta is not None and delta.name == "rotated"


class TestMoDiffEmptyStringMirrorsToApic:
    """A desired empty string on a non-naming field is dropped by ``to_apic``,
    so ``mo_diff`` must not report it as a change — else ``plan`` promises an
    update ``push`` never makes and the drift never converges (audit P1)."""

    # aaaConsoleAuth.provider_group is a non-naming field whose pattern accepts
    # "" (most string fields require ≥1 char and reject it).

    def test_desired_empty_string_is_not_a_change(self) -> None:
        from niwaki.models._generated.aaa.aaaConsoleAuth import aaaConsoleAuth

        desired = aaaConsoleAuth.model_validate({"providerGroup": ""})
        current = aaaConsoleAuth.model_validate({"providerGroup": "radius-grp"})
        # to_apic drops the empty field → push sends nothing → no real change
        assert "providerGroup" not in desired.to_apic()["aaaConsoleAuth"]["attributes"]
        assert mo_diff(desired, current) is None
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_a_real_value_over_empty_current_still_diffs(self) -> None:
        from niwaki.models._generated.aaa.aaaConsoleAuth import aaaConsoleAuth

        desired = aaaConsoleAuth.model_validate({"providerGroup": "radius-grp"})
        current = aaaConsoleAuth.model_validate({"providerGroup": ""})
        delta = mo_diff(desired, current)
        assert delta is not None and delta.provider_group == "radius-grp"


class TestMoDiffNumericNamedNumbers:
    """Named numbers whose names are numeric strings — one value, two coercions.

    ``poeIfPol.maximum_power`` is ``int | Literal['30000', …]``: construction
    keeps the Literal string (exact match beats the int branch) while a read
    coerces the same wire value to ``int``.  ``'30000'`` vs ``30000`` is one
    value spelled by two paths, never drift (measured live: a phantom update
    on every plan of an imported fabric).
    """

    def test_literal_string_and_int_spellings_are_one_value(self) -> None:
        from niwaki.models._generated.poe.poeIfPol import poeIfPol

        desired = poeIfPol(name="default", maximum_power="30000")
        assert desired.maximum_power == "30000"  # the Literal branch won
        # A read through the typed model keeps the name form too — but the
        # catalogue read side (a base ManagedObject, ``_coerce_read`` kind
        # ``int``) serves the same wire value as ``30000``; both spellings
        # must compare equal wherever they meet.
        current = poeIfPol(name="default", maximum_power=30000)
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_a_genuinely_different_number_still_diffs(self) -> None:
        from niwaki.models._generated.poe.poeIfPol import poeIfPol

        desired = poeIfPol(name="default", maximum_power="15400")
        current = poeIfPol.from_apic(
            {"poeIfPol": {"attributes": {"name": "default", "maximumPower": "30000"}}}
        )
        delta = mo_diff(desired, current, respect_fields_set=True)
        assert delta is not None and delta.maximum_power == "15400"

    def test_bool_never_matches_a_numeric_string(self) -> None:
        from niwaki.utils.diff import _values_equal

        assert not _values_equal("1", True)  # bool is not a named number
        assert _values_equal("30000", 30000)
        assert _values_equal(30000, "30000")
        assert not _values_equal("abc", 30000)
