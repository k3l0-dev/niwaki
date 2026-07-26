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
    def test_maker_name_is_the_navigation_name(self, parent: str, name: str, child: str) -> None:
        """The curated maker name IS the facade navigation name — one vocabulary.

        The generator overlays the makers section onto CHILD_MAP keyed by
        ``(parent, child)``, so the read and write surfaces cannot diverge at
        a curated position.  The ``polUni`` makers table maps to the special
        ``_root`` navigation row.
        """
        key = "_root" if parent == "polUni" else parent
        assert CHILD_MAP.get(key, {}).get(name) == child, (
            f"design maker {parent}.{name} → {child} is not the navigation name "
            f"in CHILD_MAP[{key!r}] — regenerate _child_map.py "
            "(uv run python -m niwaki._codegen.generate_domain)"
        )

    def test_never_creatable_makers_under_multi_instance_parents_are_reviewed(self) -> None:
        """Guard against the qosDscpTransPol bug class (dropped in 0.14.2).

        A never-creatable, name-keyed class curated as a maker under a *creatable*
        (multi-instance) parent only works if its default MO exists at the
        maker's own DN.  When that default lives only under one specific parent
        instance (``tn-infra``, ``tn-mgmt``, …), the POST hits a non-existent,
        non-creatable DN and the APIC returns HTTP 400 everywhere else.  Every
        such maker must be reviewed against a live fabric and recorded here.
        """
        reviewed = {
            "vzAny": "auto-exists per VRF at uni/tn-*/ctx-*/any — POST-modifies it",
            "mgmtMgmtP": "only under tn-mgmt; the management-EPG subtree hangs off it",
            "mgmtExtMgmtEntity": "only under tn-mgmt; external management network profile",
            # Mandatory service singletons auto-created under every commPol — the
            # HTTP/HTTPS/SSH access config lives here (the vzAny pattern).
            "commHttp": "auto-exists under every commPol",
            "commHttps": "auto-exists under every commPol",
            "commSsh": "auto-exists under every commPol",
            "commShellinabox": "auto-exists under every commPol",
        }
        offenders: dict[str, str] = {}
        for parent, name, child in _makers():
            if getattr(_load_class(parent), "_is_creatable", True) is False:
                continue  # singleton parent — the default always sits at the maker DN
            child_cls = _load_class(child)
            if getattr(child_cls, "_is_creatable", True) is not False:
                continue  # creatable child — the maker can create it outright
            if "name" not in child_cls.model_fields:
                continue  # fixed-RN singleton (e.g. vnsSvcCont), not name-keyed
            if child not in reviewed:
                offenders[f"{parent}.{name}"] = child
        assert not offenders, (
            f"un-reviewed never-creatable makers under multi-instance parents: "
            f"{offenders}. Verify against a fabric and add to `reviewed`, or drop the "
            f"maker — this is the qosDscpTransPol pattern (HTTP 400 under user tenants)."
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
