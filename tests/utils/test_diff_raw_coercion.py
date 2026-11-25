"""``mo_diff`` — the wire-name channel compares in the catalogue's coerced space.

The design escape hatches (``raw()`` on a catalogue-served class, ``raw_set()``
on any node) carry attributes as wire strings.  The APIC answers with its own
canonical spelling, and wire spellings have synonyms: a bool declared
``"true"`` reads back ``"yes"``, a bitmask declared ``"syn,ack"`` reads back
``"ack,syn"``, an integer declared ``"0x10"`` reads back ``"16"``.  Compared as
raw strings, every one of those is a phantom change that ``mode="plan"``
reports forever.

Contract pinned here: when the catalogue knows the (wire class, wire property)
pair, both sides are compared in the space the catalogue-backed read path
already coerces to (``ManagedObject.__getattr__`` — bool / int / float / flags;
enums, addresses and free strings keep the wire string).  Two spellings the
kind declares equal are not a change; genuinely different values still are;
an unknown class, an unknown property, or a value the coercion refuses falls
back to the plain string comparison — never an exception.

Named numbers are deliberately absent: the catalogue serves them as ``int``
and the read path keeps a name (``"http"``) as the wire string, so the read
path itself does not equate ``80`` with ``"http"`` — nothing to mirror.
"""

from __future__ import annotations

import pytest

from niwaki.design import design, tenant
from niwaki.design._node import RawDesignNode
from niwaki.models.aaa.aaaLdapProvider import aaaLdapProvider
from niwaki.models.aaa.aaaRadiusProvider import aaaRadiusProvider
from niwaki.models.base import ManagedObject
from niwaki.models.bgp.bgpPeerP import bgpPeerP
from niwaki.models.fv.fvBD import fvBD
from niwaki.utils import mo_diff
from tests._jsonpath import as_object, at

SVC_BD_DN = "uni/tn-prod/svcBD-svc"
FLT_ENTRY_DN = "uni/tn-prod/fp-7/fe-e1"


# ── Builders ──────────────────────────────────────────────────────────────────
#
# fvSvcBD (bools + a flags prop) and vzRFltE (a multi-member bitmask + ints)
# have no generated model: both sides of the diff are catalogue-served
# ManagedObject wire pairs.  The desired side is built through the public
# raw() door, exactly as a hand-written or reverse-imported design builds it.


def _desired_svc_bd(**wire: str) -> ManagedObject:
    cursor = tenant("prod").raw("fvSvcBD", name="svc", **wire)
    return cursor.design_node.mo()


def _current_svc_bd(**wire: str) -> ManagedObject:
    return ManagedObject.from_apic(
        {"fvSvcBD": {"attributes": {"name": "svc", "dn": SVC_BD_DN, **wire}}}
    )


def _desired_flt_entry(**wire: str) -> ManagedObject:
    cursor = tenant("prod").raw("vzRFltP", id="7").raw("vzRFltE", name="e1", **wire)
    return cursor.design_node.mo()


def _current_flt_entry(**wire: str) -> ManagedObject:
    return ManagedObject.from_apic(
        {"vzRFltE": {"attributes": {"name": "e1", "dn": FLT_ENTRY_DN, **wire}}}
    )


def _emitted(delta: ManagedObject, aci_class: str) -> dict[str, object]:
    """The wire attributes a delta would POST."""
    return dict(as_object(at(delta.to_apic(), aci_class, "attributes")))


def _raw_desired(
    wire_class: str, naming: dict[str, str], rn: str, attrs: dict[str, str]
) -> ManagedObject:
    """A catalogue-served desired MO the public door would refuse to build.

    ``raw()`` rejects unknown classes and properties at declaration; the
    reverse importer's bypass channel (``on_unknown="raw"``) does not, so the
    diff must cope with them.  Built on the node substrate directly.
    """
    root = tenant("x").design_node
    return RawDesignNode(wire_class, naming, rn, attrs, root).mo()


# ── Bool synonyms ─────────────────────────────────────────────────────────────


class TestBoolSynonyms:
    @pytest.mark.parametrize(
        ("declared", "read_back"),
        [("true", "yes"), ("false", "no"), ("yes", "yes"), ("no", "false"), ("on", "yes")],
    )
    def test_synonym_spellings_are_not_a_change(self, declared: str, read_back: str) -> None:
        desired = _desired_svc_bd(arpFlood=declared)
        current = _current_svc_bd(arpFlood=read_back)

        assert mo_diff(desired, current, respect_fields_set=True) is None

    @pytest.mark.parametrize(("declared", "read_back"), [("true", "no"), ("false", "yes")])
    def test_a_different_bool_is_still_a_change(self, declared: str, read_back: str) -> None:
        desired = _desired_svc_bd(arpFlood=declared)
        current = _current_svc_bd(arpFlood=read_back)

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        # The delta carries the desired value in its declared wire spelling.
        assert _emitted(delta, "fvSvcBD")["arpFlood"] == declared

    def test_only_the_genuine_change_survives_in_the_delta(self) -> None:
        desired = _desired_svc_bd(arpFlood="true", unicastRoute="false", ipLearning="yes")
        current = _current_svc_bd(arpFlood="yes", unicastRoute="yes", ipLearning="yes")

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        emitted = _emitted(delta, "fvSvcBD")
        assert emitted["unicastRoute"] == "false"
        assert "arpFlood" not in emitted
        assert "ipLearning" not in emitted
        assert delta.rn == "svcBD-svc"  # pairing identity survives the delta


# ── Bitmask order ─────────────────────────────────────────────────────────────


class TestFlagsOrder:
    @pytest.mark.parametrize(
        ("declared", "read_back"),
        [("syn,ack", "ack,syn"), ("est,fin,rst", "rst,est,fin"), ("syn", "syn")],
    )
    def test_member_order_is_not_a_change(self, declared: str, read_back: str) -> None:
        desired = _desired_flt_entry(tcpRules=declared)
        current = _current_flt_entry(tcpRules=read_back)

        assert mo_diff(desired, current, respect_fields_set=True) is None

    @pytest.mark.parametrize(
        ("declared", "read_back"),
        [("syn", "ack,syn"), ("syn,ack", "syn"), ("syn,ack", "est,fin")],
    )
    def test_a_different_member_set_is_still_a_change(self, declared: str, read_back: str) -> None:
        desired = _desired_flt_entry(tcpRules=declared)
        current = _current_flt_entry(tcpRules=read_back)

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "vzRFltE")["tcpRules"] == declared


# ── Integer spellings ─────────────────────────────────────────────────────────


class TestIntSpellings:
    def test_hex_and_decimal_spellings_are_one_value(self) -> None:
        # The read path coerces an int-kind wire value through the same
        # coercer: "0x10" reads as 16.
        desired = _desired_flt_entry(matchDscp="0x10")
        current = _current_flt_entry(matchDscp="16")

        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_a_different_integer_is_still_a_change(self) -> None:
        desired = _desired_flt_entry(matchDscp="16")
        current = _current_flt_entry(matchDscp="17")

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "vzRFltE")["matchDscp"] == "16"

    def test_mixed_kinds_in_one_object(self) -> None:
        desired = _desired_flt_entry(tcpRules="syn,ack", stateful="true", matchDscp="0x10")
        current = _current_flt_entry(tcpRules="ack,syn", stateful="yes", matchDscp="16")

        assert mo_diff(desired, current, respect_fields_set=True) is None


# ── Fallbacks: string comparison, never an exception ──────────────────────────


class TestFallbackToStringComparison:
    def test_a_value_the_kind_cannot_hold_compares_as_a_string(self) -> None:
        # A sentinel outside the bool space: the coercion leaves it untouched
        # (as the read path does), so the comparison is plain string equality.
        desired = _desired_svc_bd(arpFlood="not-applicable")

        assert (
            mo_diff(desired, _current_svc_bd(arpFlood="not-applicable"), respect_fields_set=True)
            is None
        )
        delta = mo_diff(desired, _current_svc_bd(arpFlood="yes"), respect_fields_set=True)
        assert delta is not None
        assert _emitted(delta, "fvSvcBD")["arpFlood"] == "not-applicable"

    def test_unknown_property_of_a_known_class(self) -> None:
        desired = _raw_desired("fvSvcBD", {"name": "svc"}, "svcBD-svc", {"zzFutureProp": "true"})

        same = _current_svc_bd(zzFutureProp="true")
        assert mo_diff(desired, same, respect_fields_set=True) is None

        # No kind is known for the property: a synonym spelling cannot be
        # proven equal, so it stays a change.
        synonym = _current_svc_bd(zzFutureProp="yes")
        delta = mo_diff(desired, synonym, respect_fields_set=True)
        assert delta is not None
        assert _emitted(delta, "fvSvcBD")["zzFutureProp"] == "true"

    def test_unknown_class(self) -> None:
        desired = _raw_desired("zzFutureThing", {"name": "x"}, "future-x", {"enabled": "true"})

        def current(enabled: str) -> ManagedObject:
            return ManagedObject.from_apic(
                {"zzFutureThing": {"attributes": {"name": "x", "enabled": enabled}}}
            )

        assert mo_diff(desired, current("true"), respect_fields_set=True) is None
        delta = mo_diff(desired, current("yes"), respect_fields_set=True)
        assert delta is not None
        assert _emitted(delta, "zzFutureThing")["enabled"] == "true"

    def test_property_absent_from_the_read_is_a_change(self) -> None:
        desired = _desired_svc_bd(arpFlood="true")

        delta = mo_diff(desired, _current_svc_bd(), respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "fvSvcBD")["arpFlood"] == "true"


# ── The typed-field branch (generated class, renamed field) ───────────────────


class TestTypedFieldBranchUnchanged:
    """raw_set() of a prop the generated model knows under a renamed field.

    Already compared in coerced space through the field's own annotation;
    the catalogue channel must not change that.
    """

    @pytest.mark.parametrize(("declared", "read_back"), [("true", "yes"), ("yes", "yes")])
    def test_synonyms_converge(self, declared: str, read_back: str) -> None:
        desired = tenant("demo").bd("web").raw_set(arpFlood=declared).design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "arpFlood": read_back}}})

        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_a_real_change_is_reported(self) -> None:
        desired = tenant("demo").bd("web").raw_set(arpFlood="yes").design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "arpFlood": "no"}}})

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "fvBD")["arpFlood"] == "yes"


# ── raw_set() of a typed field whose wire name IS its Python name ─────────────


class TestTypedFieldSpelledLikeItsWireName:
    """raw_set() of a generated field that carries no wire alias (``type``).

    The model's alias map lists renamed fields only, so such a prop never
    reaches the typed branch; and ``from_apic`` stores its value in the typed
    field, not in the extras — the string fallback then finds nothing on the
    current side and reports a change even for an identical value.  The
    reverse importer emits exactly this shape for a model-refused value on
    an identity-named field, so it is a phantom drift on every plan.
    """

    def test_identical_value_is_not_a_change(self) -> None:
        desired = tenant("demo").bd("web").raw_set(type="regular").design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "type": "regular"}}})

        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_a_different_value_is_still_a_change(self) -> None:
        desired = tenant("demo").bd("web").raw_set(type="fc").design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", "type": "regular"}}})

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "fvBD")["type"] == "fc"


# ── A prop absent from the read is always a change ────────────────────────────


def _radius_provider_desired(**wire: str) -> ManagedObject:
    cursor = design().aaa().raw("aaaRadiusEp").raw("aaaRadiusProvider", name="r1")
    return cursor.raw_set(**wire).design_node.mo()


def _radius_provider_read(**wire: str) -> ManagedObject:
    dn = "uni/userext/radiusext/radiusprovider-r1"
    return aaaRadiusProvider.from_apic(
        {"aaaRadiusProvider": {"attributes": {"name": "r1", "dn": dn, **wire}}}
    )


def _ldap_provider_desired(**wire: str) -> ManagedObject:
    cursor = design().aaa().raw("aaaLdapEp").raw("aaaLdapProvider", name="l1")
    return cursor.raw_set(**wire).design_node.mo()


def _ldap_provider_read(**wire: str) -> ManagedObject:
    dn = "uni/userext/ldapext/ldapprovider-l1"
    return aaaLdapProvider.from_apic(
        {"aaaLdapProvider": {"attributes": {"name": "l1", "dn": dn, **wire}}}
    )


def _bgp_peer_desired(**wire: str) -> ManagedObject:
    cursor = (
        tenant("prod")
        .raw("l3extOut", name="out")
        .raw("l3extLNodeP", name="np")
        .raw("bgpPeerP", addr="10.0.0.1")
    )
    return cursor.raw_set(**wire).design_node.mo()


def _bgp_peer_read(**wire: str) -> ManagedObject:
    dn = "uni/tn-prod/out-out/lnodep-np/peerP-[10.0.0.1]"
    return bgpPeerP.from_apic({"bgpPeerP": {"attributes": {"addr": "10.0.0.1", "dn": dn, **wire}}})


class TestAbsentFromTheReadIsAlwaysAChange:
    """A raw wire prop the read did not return is a change — never the model default.

    The APIC never echoes a secret, so a typed field left at its model
    default on the current side says nothing about the fabric.  ``push``
    sends every raw wire attr verbatim (an empty secret clears it), so
    ``plan`` must report it whatever the default happens to be.
    """

    def test_empty_secret_spelled_like_its_wire_name(self) -> None:
        # bgpPeerP.password: secure, no wire alias, model default "".
        desired = _bgp_peer_desired(password="")

        delta = mo_diff(desired, _bgp_peer_read(), respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "bgpPeerP")["password"] == ""

    def test_empty_secret_key_of_a_radius_provider(self) -> None:
        desired = _radius_provider_desired(key="")

        delta = mo_diff(desired, _radius_provider_read(), respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "aaaRadiusProvider")["key"] == ""

    def test_empty_secret_behind_a_renamed_field(self) -> None:
        # aaaLdapProvider: wire "key" is the Python field "password" (secure).
        assert aaaLdapProvider.model_fields["password"].validation_alias == "key"
        desired = _ldap_provider_desired(key="")

        delta = mo_diff(desired, _ldap_provider_read(), respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "aaaLdapProvider")["key"] == ""

    def test_non_empty_secret_absent_from_the_read(self) -> None:
        desired = _bgp_peer_desired(password="s3cret")

        delta = mo_diff(desired, _bgp_peer_read(), respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "bgpPeerP")["password"] == "s3cret"

    @pytest.mark.parametrize(
        ("wire", "value"),
        # Each value equals the model default of its field.
        [("arpFlood", "no"), ("type", "regular")],
    )
    def test_non_secure_prop_at_its_default_but_absent(self, wire: str, value: str) -> None:
        desired = tenant("demo").bd("web").raw_set(**{wire: value}).design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web"}}})

        delta = mo_diff(desired, current, respect_fields_set=True)

        assert delta is not None
        assert _emitted(delta, "fvBD")[wire] == value

    @pytest.mark.parametrize(("wire", "value"), [("arpFlood", "no"), ("type", "regular")])
    def test_present_and_equal_is_still_unchanged(self, wire: str, value: str) -> None:
        desired = tenant("demo").bd("web").raw_set(**{wire: value}).design_node.mo()
        current = fvBD.from_apic({"fvBD": {"attributes": {"name": "web", wire: value}}})

        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_secret_present_and_equal_is_unchanged(self) -> None:
        desired = _radius_provider_desired(key="")

        assert mo_diff(desired, _radius_provider_read(key=""), respect_fields_set=True) is None


# ── Classes the catalogue cannot describe ─────────────────────────────────────

# The catalogue refuses ``class_meta`` for these (ValueError: two wire props
# collide on one readable name) — the raw compare falls back to strings.
NAME_COLLISION_CLASSES: dict[str, tuple[dict[str, str], str]] = {
    "compHpNic": ({"name": "n"}, "hpnic-n"),
    "compMgmtNic": ({"name": "n"}, "mgmtnic-n"),
    "compSystem": ({"nodeDn": "topology/pod-1/node-101"}, "system-[topology/pod-1/node-101]"),
    "smarteventRuleInst": ({"genericRuleName": "g", "eventID": "1"}, "ruleinst-g-1"),
    "smartfaultRuleInst": ({"key": "k"}, "ruleinst-[k]"),
}


class TestCatalogueRefusedClassFallsBackToStrings:
    @pytest.mark.parametrize("wire_class", sorted(NAME_COLLISION_CLASSES))
    def test_equal_strings_are_unchanged(self, wire_class: str) -> None:
        naming, rn = NAME_COLLISION_CLASSES[wire_class]
        desired = _raw_desired(wire_class, naming, rn, {"descr": "a"})
        current = ManagedObject.from_apic({wire_class: {"attributes": {**naming, "descr": "a"}}})

        assert mo_diff(desired, current, respect_fields_set=True) is None

    @pytest.mark.parametrize("wire_class", sorted(NAME_COLLISION_CLASSES))
    def test_different_strings_are_a_change(self, wire_class: str) -> None:
        naming, rn = NAME_COLLISION_CLASSES[wire_class]
        desired = _raw_desired(wire_class, naming, rn, {"descr": "a"})
        current = ManagedObject.from_apic({wire_class: {"attributes": {**naming, "descr": "b"}}})

        delta = mo_diff(desired, current, respect_fields_set=True)

        # Only the verdict is pinned: serialising these classes (``to_apic``)
        # consults the same refused catalogue entry, outside this contract.
        assert delta is not None
        assert delta.aci_class == wire_class
