"""Unit tests for the generate_domain naming machinery (no corpus needed).

The collision cascade, the fail-loud collision paths, the pkg-classname tie-break
and the NAV_DEPRECATED baseline diff are exercised on synthetic class dicts;
a few pins against the committed CHILD_MAP document the recovered edges and
the Cisco-typo fixes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from niwaki._codegen import generate_domain as gd
from niwaki.domain._child_map import CHILD_MAP, NAV_DEPRECATED


def _cls(
    label: str = "",
    class_name: str = "",
    pkg: str = "p",
    contained_by: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "className": class_name,
        "classPkg": pkg,
        "containedBy": contained_by or ["parentP"],
    }


class TestDeriveName:
    def test_label_correction_fixes_cisco_typo(self) -> None:
        # "Maitenance" is corrected in the common funnel (LABEL_CORRECTIONS);
        # the old NAV_NAME_OVERRIDES entry that pinned this name is gone.
        assert (
            gd._derive_name("maintCatMaintP", "Catalog Maitenance Policy", "CatMaintP", "maint")
            == "catalog_maintenance_policy"
        )

    def test_usable_label_wins(self) -> None:
        assert gd._derive_name("x", "ARP Flooding", "ArpFlood", "fv") == "arp_flooding"

    def test_sentence_label_falls_to_pkg_classname(self) -> None:
        label = "Profiles For DWDM To Be Applied At The Interface Level"
        assert gd._derive_name("dwdmIfPol", label, "IfPol", "dwdm") == "dwdm_if_pol"

    def test_pkg_prefix_not_duplicated(self) -> None:
        assert gd._pkg_classname("fvnsVlanInstP", "VlanInstP", "fvns") == "fvns_vlan_inst_p"
        assert gd._pkg_classname("x", "FvnsThing", "fvns") == "fvns_thing"

    def test_empty_pkg_keeps_bare_classname(self) -> None:
        assert gd._pkg_classname("x", "DevFolder", "") == "dev_folder"


class TestBuildChildMapFailLoud:
    def test_two_jargon_entries_on_one_slot_raise(self) -> None:
        classes = {
            "aOne": _cls(label="One"),
            "aTwo": _cls(label="Two"),
        }
        jargon = {"aOne": "same", "aTwo": "same"}
        with pytest.raises(ValueError, match="jargon collision"):
            gd._build_child_map(classes, jargon)

    def test_unresolvable_collision_raises(self) -> None:
        # Same pkg, same className, same label: the cascade and the final
        # pkg-classname tie-break both emit the same name — must break the
        # build, never drop an edge silently.
        classes = {
            "pSame1": _cls(label="Same", class_name="Same"),
            "pSame2": _cls(label="Same", class_name="Same"),
        }
        with pytest.raises(ValueError, match="unresolvable navigation collision"):
            gd._build_child_map(classes, {})

    def test_final_tie_break_resolves_via_pkg_classname(self) -> None:
        # Identical labels, no direction suffix, no common-suffix prefix to
        # extract: the cascade emits one shared name, the final tie-break
        # falls back to the (unique) pkg-prefixed className for the loser.
        classes = {
            "pX": _cls(label="Widget", class_name="X"),
            "pX2": _cls(label="Widget", class_name="X2"),
        }
        child_map, _ = gd._build_child_map(classes, {})
        row = child_map["parentP"]
        assert set(row.values()) == {"pX", "pX2"}, row
        assert len(row) == 2

    def test_jargon_keeps_slot_and_implicit_united(self) -> None:
        classes = {
            "aJarg": _cls(label="Widget"),
            "aAuto": _cls(label="Widget", class_name="AutoW"),
        }
        child_map, _ = gd._build_child_map(classes, {"aJarg": "widget"})
        row = child_map["parentP"]
        assert row["widget"] == "aJarg"
        assert "aAuto" in row.values()


# ── Navigation names vs the node API ──────────────────────────────────────────
#
# Attribute lookup only reaches ``__getattr__`` for names the object does NOT
# already carry: a navigation name equal to a node attribute (``query``,
# ``read``, …) is shadowed by it and the edge is unreachable at runtime.  The
# reserved set is introspected from the node classes themselves — a new
# public method on a node is reserved the moment it exists, no list to update.
#
# Generator seam contract (``niwaki._codegen.generate_domain``):
#
# - ``_node_api_names() -> frozenset[str]`` — every public (no leading
#   underscore) attribute name of ``NiwakiNode``, ``AsyncNiwakiNode`` and
#   ``_JargonNavMixin``, introspected *when called* (never frozen at import).
# - ``_root_api_names() -> frozenset[str]`` — ``_node_api_names()`` plus every
#   public attribute name of ``Niwaki`` and ``AsyncNiwaki``: the ``_root`` row
#   is also reached as ``aci.<name>`` through the client's ``__getattr__``.
# - ``_check_node_api_shadowing(child_map: Mapping[str, Mapping[str, str]])
#   -> None`` — raises :class:`ValueError` listing EVERY offender at once
#   (collect-all); each offender's line names the parent key, the child class
#   and the clashing navigation name.  ``_root`` is checked against
#   ``_root_api_names()``, every other parent against ``_node_api_names()``.
#   Curated maker names get no exemption.
# - ``main()`` runs the check on the final map (after the curated maker
#   overlay) and fails BEFORE writing ``OUT_FILE``.
# - Auto-derivation (``_build_child_map``) never emits a reserved name; it
#   consults the introspected set when the map is built, so a node method
#   added later is escaped too (today: a trailing underscore, ``query_``).
#
# The seams are reached through ``_seam`` below so this module type-checks
# before they exist; a missing seam fails with the contract above.


def _public(cls: type) -> frozenset[str]:
    return frozenset(a for a in dir(cls) if not a.startswith("_"))


def _node_api() -> frozenset[str]:
    from niwaki.facade import AsyncNiwakiNode, NiwakiNode, _JargonNavMixin

    return _public(NiwakiNode) | _public(AsyncNiwakiNode) | _public(_JargonNavMixin)


def _root_api() -> frozenset[str]:
    from niwaki.facade import AsyncNiwaki, Niwaki

    return _node_api() | _public(Niwaki) | _public(AsyncNiwaki)


# The node API as of 2.0.1 — a floor, not the definition: proves the
# introspection is not vacuous.
_KNOWN_NODE_API = frozenset({"dn", "cls", "mo", "read", "delete", "query"})


def _seam(name: str) -> Callable[..., Any]:
    fn = getattr(gd, name, None)
    if not callable(fn):
        pytest.fail(
            f"generate_domain.{name} is missing — see the seam contract in "
            "tests/domain/test_generate_domain.py (navigation names vs the node API)"
        )
    return cast(Callable[..., Any], fn)


def _gen_node_api() -> frozenset[str]:
    return cast(frozenset[str], _seam("_node_api_names")())


def _gen_root_api() -> frozenset[str]:
    return cast(frozenset[str], _seam("_root_api_names")())


def _check(child_map: Mapping[str, Mapping[str, str]]) -> None:
    _seam("_check_node_api_shadowing")(child_map)


class TestShippedNavigationAvoidsNodeApi:
    """The shipped tables carry no navigation name the node API shadows."""

    def test_introspected_api_is_not_vacuous(self) -> None:
        assert _node_api() >= _KNOWN_NODE_API
        assert {"connect", "close", "root", "node", "query"} <= _root_api()

    def test_no_child_map_name_is_shadowed_by_the_node_api(self) -> None:
        """Every CHILD_MAP edge is reachable through attribute access.

        ``_root`` is held to the client surface too (``aci.<name>`` proxies
        to the root node only for names the client does not carry itself).
        """
        node_api, root_api = _node_api(), _root_api()
        offenders = sorted(
            (parent, name, child)
            for parent, row in CHILD_MAP.items()
            for name, child in row.items()
            if name in (root_api if parent == "_root" else node_api)
        )
        assert not offenders, (
            f"navigation names shadowed by the node API (unreachable): {offenders}"
        )

    def test_no_deprecated_alias_is_shadowed_by_the_node_api(self) -> None:
        """A shadowed alias would be as dead as a shadowed edge."""
        node_api, root_api = _node_api(), _root_api()
        offenders = sorted(
            (parent, name)
            for parent, row in NAV_DEPRECATED.items()
            for name in row
            if name in (root_api if parent == "_root" else node_api)
        )
        assert not offenders, offenders


class TestNodeApiIntrospection:
    """The generator derives the reserved set from the classes, at call time."""

    def test_node_api_matches_the_node_classes(self) -> None:
        assert _gen_node_api() == _node_api()

    def test_root_api_matches_node_plus_client_classes(self) -> None:
        assert _gen_root_api() == _root_api()

    def test_root_api_is_a_superset_of_node_api(self) -> None:
        assert _gen_node_api() <= _gen_root_api()

    def test_private_names_are_not_reserved(self) -> None:
        # __getattr__ refuses underscore names before any lookup — they are
        # never navigation names, so reserving them would be noise.
        assert not {n for n in _gen_root_api() if n.startswith("_")}

    @pytest.mark.parametrize("owner", ["NiwakiNode", "AsyncNiwakiNode", "_JargonNavMixin"])
    def test_new_node_method_is_reserved_without_a_list_update(
        self, owner: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import niwaki.facade as facade

        monkeypatch.setattr(getattr(facade, owner), "frobnicate", lambda self: None, raising=False)
        assert "frobnicate" in _gen_node_api()
        assert "frobnicate" in _gen_root_api()

    @pytest.mark.parametrize("owner", ["Niwaki", "AsyncNiwaki"])
    def test_new_client_method_is_reserved_at_root_only(
        self, owner: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import niwaki.facade as facade

        monkeypatch.setattr(getattr(facade, owner), "frobnicate", lambda self: None, raising=False)
        assert "frobnicate" in _gen_root_api()
        assert "frobnicate" not in _gen_node_api()


class TestCheckNodeApiShadowing:
    """``_check_node_api_shadowing`` — the generator's fail-loud refusal."""

    def test_clean_map_passes(self) -> None:
        _check({"_root": {"tenant": "fvTenant"}, "parentP": {"widget": "pWidget"}})

    def test_empty_map_passes(self) -> None:
        _check({})
        _check({"parentP": {}})

    @pytest.mark.parametrize(
        "name",
        [
            "query_group",  # contains a reserved word, is not one
            "smart_query",
            "callhome_query",
            "read_only",
            "dn_block",
            "model",  # 'mo' prefix
            "query_",  # the auto-derivation escape
            "Query",  # attribute lookup is case-sensitive
        ],
    )
    def test_names_merely_containing_a_reserved_word_pass(self, name: str) -> None:
        _check({"parentP": {name: "pChild"}})

    @pytest.mark.parametrize("name", sorted(_KNOWN_NODE_API))
    def test_reserved_name_under_a_parent_raises(self, name: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            _check({"parentP": {"widget": "pWidget", name: "pClash"}})
        message = str(excinfo.value)
        assert "parentP" in message
        assert "pClash" in message
        assert repr(name) in message or f"'{name}'" in message or f"`{name}`" in message, (
            f"the clashing name {name!r} must be quoted in the error: {message}"
        )
        assert "pWidget" not in message, "a clean sibling is not an offender"

    def test_every_introspected_node_name_is_refused(self) -> None:
        for name in sorted(_node_api()):
            with pytest.raises(ValueError, match=name):
                _check({"parentP": {name: "pClash"}})

    def test_the_shipped_defect_shape_is_refused(self) -> None:
        """The exact pre-fix edge: curated maker ``query`` under callhomeQueryGroup."""
        with pytest.raises(ValueError) as excinfo:
            _check({"callhomeQueryGroup": {"query": "callhomeQuery", "tag": "tagTag"}})
        message = str(excinfo.value)
        assert "callhomeQueryGroup" in message
        assert "callhomeQuery" in message
        assert "query" in message

    @pytest.mark.parametrize("name", ["connect", "close", "root", "node", "with_client", "gather"])
    def test_client_name_under_root_raises(self, name: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            _check({"_root": {"tenant": "fvTenant", name: "pTop"}})
        message = str(excinfo.value)
        assert "pTop" in message
        assert name in message
        assert "fvTenant" not in message

    @pytest.mark.parametrize("name", ["connect", "close", "root", "node", "with_client", "gather"])
    def test_client_name_below_root_passes(self, name: str) -> None:
        # Only the root row is proxied through the client's __getattr__.
        _check({"parentP": {name: "pChild"}})

    def test_node_name_under_root_raises(self) -> None:
        with pytest.raises(ValueError, match="read"):
            _check({"_root": {"read": "pTop"}})

    def test_offenders_are_collected_not_first_only(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _check(
                {
                    "aParent": {"query": "aChild"},
                    "bParent": {"read": "bChild"},
                    "_root": {"connect": "cTop"},
                }
            )
        message = str(excinfo.value)
        for token in ("aParent", "aChild", "bParent", "bChild", "_root", "cTop", "connect"):
            assert token in message, (token, message)

    def test_tracks_a_node_method_added_later(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from niwaki.facade import NiwakiNode

        _check({"parentP": {"frobnicate": "pChild"}})
        monkeypatch.setattr(NiwakiNode, "frobnicate", lambda self: None, raising=False)
        with pytest.raises(ValueError, match="frobnicate"):
            _check({"parentP": {"frobnicate": "pChild"}})


class TestNamingStepNeverEmitsReservedNames:
    """Auto-derivation and the curated overlay, end to end on synthetic input."""

    @pytest.mark.parametrize("label", ["Query", "Read", "Delete", "Mo", "Dn", "Cls"])
    def test_label_equal_to_a_reserved_name_is_escaped(self, label: str) -> None:
        classes = {"pChild": _cls(label=label, class_name="Child")}
        child_map, _ = gd._build_child_map(classes, {})
        row = child_map["parentP"]
        assert list(row.values()) == ["pChild"], "the edge must survive"
        assert not set(row) & _node_api(), row
        _check(child_map)

    def test_label_equal_to_a_later_node_method_is_escaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niwaki.facade import NiwakiNode

        monkeypatch.setattr(NiwakiNode, "frobnicate", lambda self: None, raising=False)
        classes = {"pChild": _cls(label="Frobnicate", class_name="Child")}
        child_map, _ = gd._build_child_map(classes, {})
        row = child_map["parentP"]
        assert list(row.values()) == ["pChild"]
        assert "frobnicate" not in row, row
        _check(child_map)

    def test_jargon_equal_to_a_reserved_name_is_escaped(self) -> None:
        classes = {"pChild": _cls(label="Child", class_name="Child")}
        child_map, _ = gd._build_child_map(classes, {"pChild": "query"})
        assert "query" not in child_map["parentP"]
        _check(child_map)

    def test_curated_maker_on_a_reserved_name_is_refused(self) -> None:
        """Curated names get no exemption: the overlay output is checked too."""
        classes = {"pChild": _cls(label="Child", class_name="Child")}
        child_map, _ = gd._build_child_map(classes, {})
        gd._apply_maker_overlay(child_map, {"parentP": {"query": "pChild"}})
        with pytest.raises(ValueError, match="pChild"):
            _check(child_map)

    def test_curated_maker_on_a_safe_name_passes(self) -> None:
        classes = {"pChild": _cls(label="Child", class_name="Child")}
        child_map, _ = gd._build_child_map(classes, {})
        gd._apply_maker_overlay(child_map, {"parentP": {"child_query": "pChild"}})
        _check(child_map)
        assert child_map["parentP"] == {"child_query": "pChild"}


def _full_cls(label: str, class_name: str, contained_by: str) -> dict[str, Any]:
    """A synthetic class carrying every key ``main()``'s builders read."""
    return {
        **_cls(label=label, class_name=class_name, contained_by=[contained_by]),
        "identifiedBy": ["name"],
        "rs_target_prop": "",
        "to_mo": "",
        "flavor": "",
        "super_classes": [],
    }


class TestMainRefusesShadowing:
    """``main()`` wires the refusal in, before anything is written."""

    @pytest.fixture()
    def run_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Callable[[dict[str, dict[str, str]]], Path]:
        classes = {
            "pGroup": _full_cls("Group", "Group", "polUni"),
            "pChild": _full_cls("Child", "Child", "pGroup"),
        }
        out = tmp_path / "_child_map.py"
        monkeypatch.setattr(gd, "_load_schemas", lambda: classes)
        monkeypatch.setattr(gd, "NAV_BASELINE_FILE", tmp_path / "no-baseline.json")
        monkeypatch.setattr(gd, "OUT_FILE", out)

        def _run(makers: dict[str, dict[str, str]]) -> Path:
            vocabulary = tmp_path / "vocabulary.yaml"
            vocabulary.write_text(yaml.safe_dump({"makers": makers}))
            monkeypatch.setattr(gd, "VOCABULARY_FILE", vocabulary)
            gd.main()
            return out

        return _run

    def test_clean_vocabulary_generates(
        self, run_main: Callable[[dict[str, dict[str, str]]], Path]
    ) -> None:
        out = run_main({"pGroup": {"child_entry": "pChild"}})
        assert out.exists()
        assert "child_entry" in out.read_text()

    def test_curated_reserved_name_breaks_the_build_before_writing(
        self, tmp_path: Path, run_main: Callable[[dict[str, dict[str, str]]], Path]
    ) -> None:
        with pytest.raises(ValueError) as excinfo:
            run_main({"pGroup": {"query": "pChild"}})
        message = str(excinfo.value)
        assert "pGroup" in message
        assert "pChild" in message
        assert "query" in message
        assert not (tmp_path / "_child_map.py").exists(), "nothing may be written"

    def test_curated_client_name_at_root_breaks_the_build(
        self, tmp_path: Path, run_main: Callable[[dict[str, dict[str, str]]], Path]
    ) -> None:
        with pytest.raises(ValueError, match="connect"):
            run_main({"polUni": {"connect": "pGroup"}})
        assert not (tmp_path / "_child_map.py").exists()


class TestNavDeprecatedBuild:
    def test_diff_rename_unchanged_gone_and_shadowed(self, tmp_path: Path) -> None:
        baseline = {
            "parentP": {
                "old_name": "clsRenamed",  # renamed → alias
                "same_name": "clsSame",  # unchanged → nothing
                "gone_name": "clsGone",  # edge no longer emitted → nothing
                "stolen": "clsMoved",  # name now held by another class → shadowed
            }
        }
        new_map = {
            "parentP": {
                "new_name": "clsRenamed",
                "same_name": "clsSame",
                "stolen": "clsOther",
                "moved_name": "clsMoved",
            }
        }
        baseline_file = tmp_path / "baseline.json"
        baseline_file.write_text(json.dumps(baseline))
        original = gd.NAV_BASELINE_FILE
        gd.NAV_BASELINE_FILE = baseline_file
        try:
            deprecated, shadowed = gd._build_nav_deprecated(new_map)
        finally:
            gd.NAV_BASELINE_FILE = original
        assert deprecated == {"parentP": {"old_name": "clsRenamed"}}
        assert shadowed == [("parentP", "stolen", "clsMoved", "clsOther")]


class TestCommittedMapPins:
    """Document the Lot B outcomes against the committed CHILD_MAP."""

    def test_recovered_edge_lldp_inst_pol(self) -> None:
        # One of the 20 edges silently dropped by the pre-1.5.0 generator.
        assert CHILD_MAP["fabricInst"]["lldp_inst_pol"] == "lldpInstPol"
        assert CHILD_MAP["fabricInst"]["lldp_policy"] == "lldpIfPol"

    def test_typo_fix_stays_current(self) -> None:
        assert CHILD_MAP["fabricInst"]["catalog_maintenance_policy"] == "maintCatMaintP"
        assert CHILD_MAP["fvCtx"]["pim"] == "pimCtxP"

    def test_aliases_are_retired(self) -> None:
        # The 1.5.0 aliases were removed in 1.7.0; the mechanism stays for
        # future renames (delete the baseline file → the table empties).
        assert NAV_DEPRECATED == {}

    def test_every_alias_targets_a_live_edge_under_a_new_name(self) -> None:
        for parent, aliases in NAV_DEPRECATED.items():
            row = CHILD_MAP.get(parent, {})
            inv = {c: n for n, c in row.items()}
            for old_name, cls in aliases.items():
                assert old_name not in row, (parent, old_name)
                assert cls in inv and inv[cls] != old_name, (parent, old_name, cls)
