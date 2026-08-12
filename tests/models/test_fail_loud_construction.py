"""The construction-time fail-loud guard, and the public ``aci_class`` door.

Before 2.0's it.0, ``fvBD(name="web", brandNewProp="x")`` absorbed the foreign
key into ``model_extra`` (the config is ``extra="allow"`` for tolerant reads)
and ``to_apic()`` silently dropped it — the caller believed a property was
configured and nothing was sent.  The one asymmetry that matters is pinned
here from both sides: **writes fail loud, reads stay tolerant.**
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.models.base import ManagedObject
from niwaki.models.fv.fvBD import fvBD
from niwaki.models.fv.fvTenant import fvTenant


class TestConstructionFailsLoud:
    def test_a_foreign_property_is_refused_not_dropped(self) -> None:
        with pytest.raises(ValidationError, match="brandNewProp"):
            fvBD(name="web", brandNewProp="futureValue")

    def test_the_error_names_every_foreign_key(self) -> None:
        with pytest.raises(ValidationError, match=r"'alsoWrong', 'brandNewProp'"):
            fvBD(name="web", brandNewProp="x", alsoWrong="y")

    def test_a_non_string_key_still_raises_a_validation_error(self) -> None:
        """A raw TypeError from the guard's own message-building would escape
        an ``except ValidationError`` — the join is repr-based for that."""
        with pytest.raises(ValidationError):
            fvBD.model_validate({1: "x", "name": "w"})

    def test_a_typo_on_a_real_property_is_refused(self) -> None:
        """The everyday case: one letter off from an existing field."""
        with pytest.raises(ValidationError, match="arp_floodign"):
            fvBD(name="web", arp_floodign=True)

    def test_python_names_still_construct(self) -> None:
        assert fvBD(name="web", arp_flooding=True).arp_flooding is True

    def test_wire_aliases_still_construct(self) -> None:
        """``populate_by_name`` accepts the wire spelling; the guard must not
        reject what the model itself accepts."""
        assert fvBD(name="web", arpFlood="yes").arp_flooding is True

    def test_children_is_a_legitimate_key(self) -> None:
        bd = fvBD(name="web", children=[])
        assert bd.children == []

    def test_the_base_class_stays_open(self) -> None:
        """A bare ManagedObject has an open property universe (catalogue-served
        classes) — there is nothing to validate against, so no guard."""
        ManagedObject(children=[])  # must not raise


class TestAssignmentFailsLoud:
    def test_a_typo_d_assignment_is_refused(self) -> None:
        """The documented read-modify-write flow is guarded too: before this,
        ``bd.arp_floodign = True`` was absorbed into extras and the payload
        silently omitted the intended change."""
        bd = fvBD(name="web")
        with pytest.raises(ValueError, match="arp_floodign"):
            bd.arp_floodign = True

    def test_a_wire_alias_assignment_is_refused_with_a_pointer(self) -> None:
        """Assignment routes by field name only — assigning the wire alias
        would land in extras and vanish.  The refusal names the python field."""
        bd = fvBD(name="web")
        with pytest.raises(ValueError, match="arp_flooding"):
            bd.arpFlood = "yes"

    def test_real_fields_still_assign(self) -> None:
        bd = fvBD(name="web")
        bd.arp_flooding = True
        assert bd.arp_flooding is True
        assert "arp_flooding" in bd.model_fields_set  # surgical to_apic contract

    def test_read_modify_write_flow_still_works(self) -> None:
        mo = ManagedObject.from_apic({"fvBD": {"attributes": {"name": "w"}}})
        mo.arp_flooding = True  # the documented flow, on the typed dispatch
        assert "arpFlood" in str(mo.to_apic())

    def test_the_base_class_assignment_stays_open(self) -> None:
        raw = ManagedObject.from_apic({"topSystem": {"attributes": {"id": "1"}}})
        raw.anything = "x"  # open universe — catalogue-served classes

    def test_private_attributes_still_assign(self) -> None:
        bd = fvBD(name="web")
        bd._wire_class = "fvBD"  # pydantic private machinery must pass


class TestSurgicalFailsLoud:
    def test_a_foreign_change_key_is_refused(self) -> None:
        """surgical() builds through model_construct (no validators) — its
        caller-typed kwargs get the same contract as construction."""
        with pytest.raises(ValueError, match="arp_floodign"):
            fvBD.surgical({"name": "web"}, arp_floodign=True)

    def test_a_foreign_naming_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nmae"):
            fvBD.surgical({"nmae": "web"}, arp_flooding=True)

    def test_legitimate_surgical_payloads_still_build(self) -> None:
        delta = fvBD.surgical({"name": "web"}, arp_flooding=True)
        assert "arpFlood" in str(delta.to_apic())

    def test_a_wire_alias_change_key_is_refused_with_a_hint(self) -> None:
        """The silent-drop trap the guard exists for, in its worst form.

        A wire alias passes the constructor-keys gate and model_construct even
        resolves it into the right field — the instance LOOKS correct — but
        fields_set stamps the raw key, and to_apic() silently omitted the one
        change the caller asked for. Refused with the python-name hint, the
        same contract as __setattr__.
        """
        with pytest.raises(ValueError, match=r"arpFlood.*arp_flooding"):
            fvBD.surgical({"name": "web"}, arpFlood=True)

    def test_a_wire_alias_naming_key_is_refused_too(self) -> None:
        """The naming dict walks the same stamping path as the changes."""
        from niwaki.models._generated.fvns.fvnsEncapBlk import fvnsEncapBlk

        with pytest.raises(ValueError, match=r"'from'.*'from_'"):
            fvnsEncapBlk.surgical({"from": "vlan-100", "to": "vlan-200"})


class TestModelCopyResidual:
    def test_model_copy_update_is_the_documented_escape(self) -> None:
        """pydantic's validators-bypassed API stays open by design — pinned so
        the residual is a decision, not an accident."""
        copied = fvBD(name="web").model_copy(update={"brandNewProp": "x"})
        assert "brandNewProp" not in str(copied.to_apic())


class TestToApicExcludesExtras:
    def test_internally_crafted_extras_never_reach_the_payload(self) -> None:
        """The ``& model_field_names`` exclusion branch in to_apic, exercised
        via the one remaining door to that state (model_construct — trusted
        internal path).  Mutation-guards the branch now that construction and
        assignment can no longer create extras on typed models."""
        mo = fvBD.model_construct(
            _fields_set={"name", "modTs"},
            name="web",
            modTs="2024-01-01T00:00:00.000+00:00",
        )
        attrs = mo.to_apic()["fvBD"]["attributes"]
        assert attrs == {"name": "web"}


class TestReadsStayTolerant:
    def test_from_apic_absorbs_unknown_attributes(self) -> None:
        """An APIC newer than the SDK keeps deserialising without a scratch."""
        mo = ManagedObject.from_apic(
            {"fvBD": {"attributes": {"name": "w", "unknownFutureAttr": "v"}}}
        )
        assert mo["unknownFutureAttr"] == "v"

    def test_from_event_absorbs_unknown_attributes(self) -> None:
        mo = ManagedObject.from_event(
            {"fvBD": {"attributes": {"dn": "uni/tn-t/BD-w", "unknownFutureAttr": "v"}}}
        )
        assert mo["unknownFutureAttr"] == "v"

    def test_a_full_read_write_cycle_survives_unknown_read_attrs(self) -> None:
        """Read tolerant, re-serialise surgical: the unknown prop read back is
        neither rejected nor re-sent."""
        mo = ManagedObject.from_apic(
            {"fvBD": {"attributes": {"name": "w", "unknownFutureAttr": "v"}}}
        )
        payload = mo.to_apic()
        assert "unknownFutureAttr" not in str(payload)


class TestConstructionCost:
    def test_the_guard_is_cheap(self) -> None:
        """The kill criterion was >15% push overhead; the guard is a frozenset
        lookup per key.  Budget generously (CI machines vary): 10k typed
        constructions in under two seconds."""
        import time

        start = time.perf_counter()
        for i in range(10_000):
            fvBD(name=f"bd{i}", arp_flooding=True, description="x")
        assert time.perf_counter() - start < 2.0


class TestAciClass:
    def test_a_generated_model_answers_from_its_baked_class(self) -> None:
        assert fvTenant(name="t").aci_class == "fvTenant"

    def test_a_catalogue_served_read_answers_from_the_wire_envelope(self) -> None:
        mo = ManagedObject.from_apic({"topSystem": {"attributes": {"id": "1"}}})
        assert type(mo) is ManagedObject  # no generated model for topSystem
        assert mo.aci_class == "topSystem"

    def test_a_bare_local_object_answers_empty(self) -> None:
        assert ManagedObject().aci_class == ""
