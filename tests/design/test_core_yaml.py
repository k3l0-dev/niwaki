"""Curation consistency — vocabulary.yaml validated against generated data.

These tests are what keeps the hand-curated vocabulary honest when the APIC
schema (and therefore CHILD_MAP / REFERENCE_MAP / _contains) is regenerated.
"""

from __future__ import annotations

from typing import Any

import pytest

from niwaki.design._cursor import _load_class, _tables
from niwaki.design._sugar import apply_sugar
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, REFERENCE_MAP, TARGET_SUBCLASSES

# Deliberate divergences from the facade jargon (CHILD_MAP names), documented
# here so the consistency test stays strict for everything else.  The design
# surface prefers short operator vocabulary; the facade jargon keeps longer
# read-navigation names derived from the schema labels.
_MAKER_RENAMES = {
    ("fvCtx", "pim"): "pim_ctx",
    ("infraInfra", "storm_control_policy"): "storm_control_interface_policy",
    ("fvnsVlanInstP", "range"): "ranges",
    ("infraFuncP", "access_group"): "leaf_access_port_policy_group",
    ("infraSpineP", "spine_selector"): "switch_association",
    ("fabricInst", "datetime_policy"): "date_and_time_policy",
    ("fabricInst", "bgp_instance"): "bgp_route_reflector_policy",
    ("fabricInst", "syslog_group"): "syslog_monitoring_destination_group",
    ("fabricInst", "vpc_protection"): "virtual_port_channel_security_policy",
    ("datetimePol", "ntp_provider"): "providers",
    ("dnsProfile", "provider"): "dns_provider",
    ("dnsProfile", "domain"): "dns_domain",
    ("syslogGroup", "remote_destination"): "syslog_remote_destination",
    ("bgpInstPol", "autonomous_system"): "autonomous_system_profile",
    ("bgpInstPol", "route_reflector"): "bgp_route_reflector",
    ("bgpRRP", "node"): "route_reflector_node_policy_ep",
    ("fabricProtPol", "vpc_pair"): "vpc_explicit_protection_group",
    ("fabricExplicitGEp", "node"): "node_policy_end_point",
    ("ctrlrInst", "fabric_membership"): "fabric_membership_policy",
    # On a dhcp_relay_policy cursor, .provider() reads naturally — the dhcp_
    # prefix is redundant at that position (same call as dnsProfile.provider).
    ("dhcpRelayP", "provider"): "dhcp_provider",
}


def _makers() -> list[tuple[str, str, str]]:
    makers = _tables().makers
    return [
        (parent, name, child) for parent, table in makers.items() for name, child in table.items()
    ]


def _binds() -> list[tuple[str, str, str]]:
    binds = _tables().binds
    return [
        (owner, alias, target) for owner, table in binds.items() for alias, target in table.items()
    ]


class TestMakers:
    @pytest.mark.parametrize(("parent", "name", "child"), _makers())
    def test_child_class_exists_and_contained(self, parent: str, name: str, child: str) -> None:
        """Every maker's child class is generated and a valid APIC child."""
        assert child in CLASS_PKG, f"{child} not in CLASS_PKG"
        parent_cls = _load_class(parent)
        assert child in parent_cls._contains, f"{child} not a child of {parent}"

    @pytest.mark.parametrize(("parent", "name", "child"), _makers())
    def test_maker_name_matches_facade_jargon(self, parent: str, name: str, child: str) -> None:
        """Curated maker names agree with CHILD_MAP jargon (or are whitelisted)."""
        jargon = {cls: meth for meth, cls in CHILD_MAP.get(parent, {}).items()}
        if child not in jargon:
            return  # class not exposed by facade jargon — nothing to agree with
        expected = _MAKER_RENAMES.get((parent, name), name)
        assert jargon[child] == expected, (
            f"design maker {parent}.{name} → {child} diverges from facade "
            f"jargon {jargon[child]!r} without a documented rename"
        )

    def test_poluni_is_the_single_root_table(self) -> None:
        """Every maker parent is polUni or reachable from it (one rooted tree)."""
        makers = _tables().makers
        reachable = {"polUni"}
        frontier = ["polUni"]
        while frontier:
            for child in makers.get(frontier.pop(), {}).values():
                if child not in reachable:
                    reachable.add(child)
                    frontier.append(child)
        orphans = set(makers) - reachable
        assert not orphans, f"maker tables unreachable from polUni: {sorted(orphans)}"


class TestBinds:
    @pytest.mark.parametrize(("owner", "alias", "target"), _binds())
    def test_edge_resolvable_and_constructible(self, owner: str, alias: str, target: str) -> None:
        """Every bind edge resolves through REFERENCE_MAP with a usable flavor.

        Abstract targets resolve through their concrete subclasses; the Rs
        class must construct (and produce an RN) with the field its flavor
        dictates — ``name`` for tn* relations, ``target_dn`` for tDn ones.
        """
        candidates = [target, *TARGET_SUBCLASSES.get(target, [])]
        direct = {
            entry
            for cand in candidates
            if (entry := REFERENCE_MAP.get(owner, {}).get(cand)) is not None
        }
        inverse = {
            entry
            for cand in candidates
            if (entry := REFERENCE_MAP.get(cand, {}).get(owner)) is not None
        }
        entries = direct or inverse
        assert len(entries) == 1, (
            f"({owner}, {target}) resolves to {sorted(entries)} — need exactly one"
        )
        rs, flavor = next(iter(entries))
        fields = {"name": "x"} if flavor == "name" else {"target_dn": "uni/x"}
        rs_mo = _load_class(rs).model_validate(fields)
        assert rs_mo.rn, f"{rs} produced an empty RN"


class TestVerbs:
    def test_verb_rs_classes_constructible(self) -> None:
        """provide/consume Rs classes exist and take name=."""
        verbs = _tables().verbs
        assert verbs, "verbs table is empty"
        for table in verbs.values():
            for spec in table.values():
                naming: dict[str, Any] = {"name": "x"}
                rs_mo = _load_class(spec["rs"])(**naming)
                assert rs_mo.rn
                assert spec["target"] in CLASS_PKG


class TestSugar:
    def test_sugar_params_are_consumed_by_the_runtime(self) -> None:
        """Every declared sugar parameter is rewritten by design._sugar.

        A sugar key that survives ``apply_sugar`` untouched would reach the
        Pydantic model as an unknown field — the declaration and the runtime
        must stay in lock-step.
        """
        for aci_class, params in _tables().sugar.items():
            assert aci_class in CLASS_PKG
            for param in params:
                rewritten = apply_sugar(aci_class, {param: 80})
                assert param not in rewritten, (
                    f"sugar {aci_class}.{param} is not handled by apply_sugar"
                )


class TestAtomic:
    def test_atomic_classes_are_curated_makers(self) -> None:
        """Atomic classes exist and appear as a maker child (else unreachable)."""
        makers = _tables().makers
        curated_children = {c for table in makers.values() for c in table.values()}
        for aci_class in _tables().atomic:
            assert aci_class in CLASS_PKG
            assert aci_class in curated_children, f"atomic {aci_class} is not a curated child"
