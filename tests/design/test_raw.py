"""``Cursor.raw()`` / ``Cursor.raw_set()`` — the wire-name escape hatches.

2.0 it.2 lot A. ``raw()`` is a *name-based* door, never a validation bypass:
a class with a generated model routes through the typed path (wire names
translated, full validation); the pure catalogue-served node
(:class:`RawDesignNode`) is the substrate the reverse importer and the
future multi-version door build on — on today's catalogue every attachable
class is generated, so that substrate is exercised at node level.
``raw_set()`` adds out-of-model wire attributes to any node; the merge
happens at the wire boundary in every push mode.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.design import controller, tenant
from niwaki.exceptions import DesignError, DuplicateDeclarationError
from niwaki.models.base import ManagedObject
from niwaki.utils import mo_diff


def _find(payload: dict, aci_class: str) -> dict | None:
    for cls, body in payload.items():
        if cls == aci_class:
            return dict(body.get("attributes", {}))
        for child in body.get("children", []):
            found = _find(child, aci_class)
            if found is not None:
                return found
    return None


class TestRawTypedRouting:
    """raw() on generated-but-uncurated classes — the reachable surface."""

    def test_generated_class_routes_typed_and_emits_wire(self) -> None:
        # infraKafkaPol: generated, never curated (no maker) — raw() reaches
        # it by name and the payload carries wire keys.
        cfg = controller()
        cfg.raw("infraKafkaPol", name="kafka", mode="ON")
        attrs = _find(cfg.to_payload(), "infraKafkaPol")
        assert attrs is not None
        assert attrs["name"] == "kafka"
        assert attrs["mode"] == "ON"

    def test_rn_and_dn(self) -> None:
        cur = controller().raw("infraKafkaPol", name="kafka")
        node = cur._node  # pyright: ignore[reportPrivateUsage]
        assert node.rn == "kafkapol"
        assert node.dn() == "uni/controller/kafkapol"

    def test_staged_ops_include_the_node(self) -> None:
        from niwaki.design._compiler import compile_ops
        from niwaki.design._resolver import resolve

        cfg = controller()
        cfg.raw("infraKafkaPol", name="kafka", mode="ON")
        root = cfg._node  # pyright: ignore[reportPrivateUsage]
        while root.parent is not None:
            root = root.parent
        ops = compile_ops(root, resolve(root))
        kafka_ops = [op for op in ops if op.payload is not None and "infraKafkaPol" in op.payload]
        assert len(kafka_ops) == 1
        assert kafka_ops[0].payload is not None
        assert kafka_ops[0].payload["infraKafkaPol"]["attributes"]["mode"] == "ON"

    def test_unknown_class_fails_loud(self) -> None:
        with pytest.raises(DesignError, match="unknown ACI class"):
            tenant("t").raw("fvDoesNotExist", name="x")

    def test_unknown_wire_prop_fails_loud(self) -> None:
        with pytest.raises(DesignError, match="unknown wire propert"):
            controller().raw("infraKafkaPol", name="k", nopeNotAProp="x")

    def test_missing_naming_prop_fails_loud(self) -> None:
        # tagTag's RN is key-{key}: raw() without the naming prop must refuse.
        with pytest.raises(DesignError, match="missing naming propert"):
            controller().raw("infraKafkaPol").raw("tagTag", value="prod")

    def test_full_validation_applies(self) -> None:
        # The typed route keeps it.0's strict surface: a bad value refuses.
        with pytest.raises(ValidationError):
            controller().raw("infraKafkaPol", name="k" * 300)

    def test_containment_fails_loud(self) -> None:
        with pytest.raises(DesignError, match="not a valid APIC child"):
            tenant("t").raw("infraKafkaPol", name="k")

    def test_duplicate_raw_declaration_fails(self) -> None:
        cfg = controller()
        cfg.raw("infraKafkaPol", name="k")
        with pytest.raises(DuplicateDeclarationError):
            cfg.raw("infraKafkaPol", name="k")

    def test_child_under_raw_node(self) -> None:
        cfg = controller()
        kafka = cfg.raw("infraKafkaPol", name="k")
        kafka.raw("tagTag", key="env", value="prod")
        attrs = _find(cfg.to_payload(), "tagTag")
        assert attrs is not None and attrs["value"] == "prod"


class TestRawSet:
    def test_wire_attr_joins_the_typed_envelope(self) -> None:
        t = tenant("demo")
        t.bd("web").raw_set(arpFlood="yes")
        attrs = _find(t.to_payload(), "fvBD")
        assert attrs is not None
        assert attrs["arpFlood"] == "yes"
        assert attrs["name"] == "web"

    def test_unknown_wire_prop_fails_loud(self) -> None:
        with pytest.raises(DesignError, match="unknown wire propert"):
            tenant("demo").bd("web").raw_set(definitelyNotAProp="x")

    def test_chainable(self) -> None:
        t = tenant("demo")
        cur = t.bd("web").raw_set(arpFlood="yes")
        cur.subnet("10.0.0.1/24")
        assert _find(t.to_payload(), "fvSubnet") is not None

    def test_typed_model_never_sees_the_key(self) -> None:
        t = tenant("demo")
        cur = t.bd("web").raw_set(arpFlood="yes")
        instance = cur._node.mo()  # pyright: ignore[reportPrivateUsage]
        assert "arpFlood" not in instance.model_fields_set
        assert instance.to_apic()["fvBD"]["attributes"]["arpFlood"] == "yes"


def _raw_node(wire_class: str, naming: dict, rn: str, attrs: dict):
    """A RawDesignNode detached from cursor plumbing — the substrate under test.

    telemetryServer / commTelnet are two of the twelve exportable classes
    without a generated model, so ``from_apic`` serves them catalogue-backed
    regardless of REGISTRY state (a full-suite ``load_all()`` cannot turn
    them typed — the exact ordering bug this replaces).
    """
    from niwaki.design._node import RawDesignNode

    root = tenant("x")._node  # pyright: ignore[reportPrivateUsage]
    return RawDesignNode(wire_class, naming, rn, attrs, root)


class TestRawSubstrate:
    def test_raw_node_mo_serialises_wire(self) -> None:
        node = _raw_node(
            "telemetryServer", {"ip": "10.0.0.9"}, "server-10.0.0.9", {"dstPort": "5640"}
        )
        mo = node.mo()
        attrs = mo.to_apic()["telemetryServer"]["attributes"]
        assert attrs["ip"] == "10.0.0.9"
        assert attrs["dstPort"] == "5640"
        assert mo.rn == "server-10.0.0.9"

    def test_wire_pair_diff_detects_drift(self) -> None:
        node = _raw_node(
            "telemetryServer", {"ip": "10.0.0.9"}, "server-10.0.0.9", {"dstPort": "5640"}
        )
        desired = node.mo()
        current = ManagedObject.from_apic(
            {"telemetryServer": {"attributes": {"ip": "10.0.0.9", "dstPort": "9999"}}}
        )
        delta = mo_diff(desired, current, respect_fields_set=True)
        assert delta is not None
        emitted = delta.to_apic()["telemetryServer"]["attributes"]
        assert emitted["dstPort"] == "5640"
        assert delta.rn == desired.rn  # pairing identity survives the delta

    def test_wire_pair_diff_converges(self) -> None:
        node = _raw_node(
            "telemetryServer", {"ip": "10.0.0.9"}, "server-10.0.0.9", {"dstPort": "5640"}
        )
        desired = node.mo()
        current = ManagedObject.from_apic(
            {"telemetryServer": {"attributes": {"ip": "10.0.0.9", "dstPort": "5640"}}}
        )
        assert mo_diff(desired, current, respect_fields_set=True) is None

    def test_wire_pair_class_mismatch_raises(self) -> None:
        a = ManagedObject.from_apic({"telemetryServer": {"attributes": {"ip": "1.2.3.4"}}})
        b = ManagedObject.from_apic({"commTelnet": {"attributes": {}}})
        with pytest.raises(TypeError, match="wire class"):
            mo_diff(a, b)


class TestPlanFidelityTyped:
    def test_mo_diff_sees_raw_attr_drift(self) -> None:
        t = tenant("demo")
        cur = t.bd("web").raw_set(arpFlood="yes")
        desired = cur._node.mo()  # pyright: ignore[reportPrivateUsage]
        current = type(desired).from_apic(
            {"fvBD": {"attributes": {"name": "web", "arpFlood": "no"}}}
        )
        delta = mo_diff(desired, current, respect_fields_set=True)
        assert delta is not None
        assert delta.to_apic()["fvBD"]["attributes"]["arpFlood"] == "yes"

    def test_mo_diff_converges_when_raw_attr_matches(self) -> None:
        # Wire spellings have synonyms: the read side coerces "yes" to True,
        # the raw side keeps the string — comparison is in coerced space.
        t = tenant("demo")
        cur = t.bd("web").raw_set(arpFlood="yes")
        desired = cur._node.mo()  # pyright: ignore[reportPrivateUsage]
        current = type(desired).from_apic(
            {"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}}
        )
        assert mo_diff(desired, current, respect_fields_set=True) is None
