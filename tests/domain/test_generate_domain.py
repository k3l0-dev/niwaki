"""Unit tests for the generate_domain naming machinery (no corpus needed).

The collision cascade, the fail-loud collision paths, the pkg-classname tie-break
and the NAV_DEPRECATED baseline diff are exercised on synthetic class dicts;
a few pins against the committed CHILD_MAP document the recovered edges and
the Cisco-typo fixes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
    def test_override_wins_over_label(self) -> None:
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


class TestReservedSurface:
    def test_facade_surface_matches_niwaki_node(self) -> None:
        """_FACADE_SURFACE must track NiwakiNode's real public surface."""
        from niwaki.facade import NiwakiNode

        actual = frozenset(a for a in dir(NiwakiNode) if not a.startswith("_"))
        assert actual == gd._FACADE_SURFACE, (
            "NiwakiNode's public surface changed — update _FACADE_SURFACE in "
            "generate_domain.py and regenerate _child_map.py"
        )

    def test_no_auto_name_shadows_the_facade(self) -> None:
        """Auto-derived names never collide with NiwakiNode methods.

        The single curated exception (callhomeQueryGroup.query, reachable via
        .mo()) is allowed — curated maker names are authoritative.
        """
        curated_exceptions = {("callhomeQueryGroup", "query")}
        offenders = {
            (parent, name)
            for parent, row in CHILD_MAP.items()
            for name in row
            if name in gd._FACADE_SURFACE and (parent, name) not in curated_exceptions
        }
        assert not offenders, offenders


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

    def test_typo_fix_with_deprecated_alias(self) -> None:
        assert CHILD_MAP["fabricInst"]["catalog_maintenance_policy"] == "maintCatMaintP"
        assert NAV_DEPRECATED["fabricInst"]["catalog_maitenance_policy"] == "maintCatMaintP"

    def test_lot_c_rename_has_alias(self) -> None:
        # The curated-overlay renames (Lot C) are covered by the same shim.
        assert NAV_DEPRECATED["fvCtx"]["pim_ctx"] == "pimCtxP"
        assert CHILD_MAP["fvCtx"]["pim"] == "pimCtxP"

    def test_every_alias_targets_a_live_edge_under_a_new_name(self) -> None:
        for parent, aliases in NAV_DEPRECATED.items():
            row = CHILD_MAP.get(parent, {})
            inv = {c: n for n, c in row.items()}
            for old_name, cls in aliases.items():
                assert old_name not in row, (parent, old_name)
                assert cls in inv and inv[cls] != old_name, (parent, old_name, cls)
