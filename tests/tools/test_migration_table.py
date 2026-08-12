"""Guards on the committed 1.x → 2.0 migration table.

Corpus-free and git-free: these run against the committed JSON and the
shipped catalogue only, so public CI can verify the artifact users will
actually consume.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TABLE_PATH = Path("tools/niwaki_migrate/migration_2_0.json")


@pytest.fixture(scope="module")
def table() -> dict:
    return json.loads(TABLE_PATH.read_text())


class TestStructure:
    def test_surfaces_and_versions(self, table: dict) -> None:
        assert table["niwaki_from"] == "1.10.0"
        assert table["niwaki_to"] == "2.0.0"
        assert set(table["surfaces"]) == {
            "model_fields",
            "catalog_readable",
            "navigation",
            "makers",
        }

    def test_unrenamed_surfaces_are_explicitly_empty(self, table: dict) -> None:
        # 2.0 renamed fields, never navigation names or makers — stated, not implied.
        assert table["surfaces"]["navigation"] == {}
        assert table["surfaces"]["makers"] == {}

    def test_counts_match_the_content(self, table: dict) -> None:
        for surface in ("model_fields", "catalog_readable"):
            actual = sum(len(v) for v in table["surfaces"][surface].values())
            assert actual == table["counts"][surface]
        assert len(table["attribute_safe"]) == table["counts"]["attribute_safe"]

    def test_model_surface_is_the_validated_wave(self, table: dict) -> None:
        assert table["counts"]["model_fields"] == 568

    def test_every_entry_renames(self, table: dict) -> None:
        for surface in ("model_fields", "catalog_readable"):
            for cls, renames in table["surfaces"][surface].items():
                for wire, entry in renames.items():
                    assert entry["old"] != entry["new"], (surface, cls, wire)
                    assert entry["new"].isidentifier(), (surface, cls, wire)


class TestGoldens:
    def test_l3ext_scopemeta_renames(self, table: dict) -> None:
        mf = table["surfaces"]["model_fields"]
        assert mf["l3extOut"]["enforceRtctrl"] == {
            "old": "enforce_rtctrl",
            "new": "enforce_route_control",
        }
        assert mf["l3extRsNodeL3OutAtt"]["rtrId"] == {"old": "rtr_id", "new": "router_id"}
        assert mf["l3extRsPathL3OutAtt"]["addr"] == {"old": "addr", "new": "ip_address"}

    def test_sentence_fix_renames(self, table: dict) -> None:
        mf = table["surfaces"]["model_fields"]
        assert mf["fvAEPg"]["hasMcastSource"] == {
            "old": "epg_with_multisite_mcast_source",
            "new": "has_mcast_source",
        }
        assert mf["netflowNodePol"]["collectIntvl"] == {
            "old": "collection_interval_in_seconds",
            "new": "collect_intvl",
        }

    def test_curated_irreducibles_did_not_move(self, table: dict) -> None:
        # fvSubnet.preferred and vzEntry.applyToFrag are pinned by curation —
        # a row here would mean the override silently stopped applying.
        mf = table["surfaces"]["model_fields"]
        assert "preferred" not in mf.get("fvSubnet", {})
        assert "applyToFrag" not in mf.get("vzEntry", {})

    def test_keyword_gate_reaches_the_catalogue_surface(self, table: dict) -> None:
        # Label "Class" no longer produces the unreachable name `class`.
        cr = table["surfaces"]["catalog_readable"]
        assert cr["fvEPgSelector"]["matchClass"] == {"old": "class", "new": "match_class"}


class TestAttributeSafety:
    def test_safe_old_names_resolve_uniquely(self, table: dict) -> None:
        old_to_new: dict[str, set[str]] = {}
        for surface in ("model_fields", "catalog_readable"):
            for renames in table["surfaces"][surface].values():
                for entry in renames.values():
                    old_to_new.setdefault(entry["old"], set()).add(entry["new"])
        for old, new in table["attribute_safe"].items():
            assert old_to_new.get(old) == {new}, old

    def test_still_current_names_are_never_safe(self, table: dict) -> None:
        # rtr_id was renamed on l3extRsNodeL3OutAtt but vnsRtrCfg still
        # serves it — a blanket attribute rewrite would corrupt vns users.
        # The exact over-rename our own migration hit, pinned forever.
        assert "rtr_id" not in table["attribute_safe"]
        assert "addr" not in table["attribute_safe"]
        assert "mac" not in table["attribute_safe"]

    def test_safe_names_are_not_served_by_the_shipped_catalogue(self, table: dict) -> None:
        # Sampled hard check against the artifact users install: a "safe"
        # old name must not be a live readable name on high-traffic classes.
        from niwaki import catalog

        safe = set(table["attribute_safe"])
        for cls in ("fvBD", "fvAEPg", "l3extOut", "topSystem", "fabricNode", "vnsRtrCfg"):
            live = set(catalog.class_meta(cls).wire_to_readable.values())
            assert not (safe & live), (cls, sorted(safe & live)[:5])


class TestMakerContext:
    def test_known_positions(self, table: dict) -> None:
        mc = table["maker_context"]
        assert mc["path_attachment"] == ["l3extRsPathL3OutAtt"]
        assert mc["node_attachment"] == ["l3extRsNodeL3OutAtt"]
        assert "l3out" in mc and "l3extOut" in mc["l3out"]

    def test_every_context_class_is_a_string(self, table: dict) -> None:
        for maker, classes in table["maker_context"].items():
            assert classes and all(isinstance(c, str) for c in classes), maker


class TestBuilderParser:
    """Direct unit tests on the builder's model-file parser (review m7)."""

    def test_multiline_field_with_alias(self) -> None:
        from tools.niwaki_migrate.build_table import _model_fields_of

        src = (
            "class fvX(ManagedObject):\n"
            "    arp_flooding: bool = Field(\n"
            "        default=False,\n"
            '        validation_alias="arpFlood",\n'
            '        serialization_alias="arpFlood",\n'
            "    )\n"
            "    unicast_route: bool = True\n"
        )
        assert _model_fields_of(src) == {
            "arpFlood": "arp_flooding",
            "unicast_route": "unicast_route",
        }

    def test_single_line_field_with_alias(self) -> None:
        from tools.niwaki_migrate.build_table import _model_fields_of

        src = '    scope: str = Field(default="private", serialization_alias="scope")\n'
        assert _model_fields_of(src) == {"scope": "scope"}

    def test_classvars_excluded(self) -> None:
        from tools.niwaki_migrate.build_table import _model_fields_of

        src = (
            '    _aci_class: ClassVar[str] = "fvX"\n'
            '    _rn_format: ClassVar[str] = "x-{name}"\n'
            '    name: str = Field(serialization_alias="name")\n'
        )
        assert _model_fields_of(src) == {"name": "name"}

    def test_children_excluded(self) -> None:
        from tools.niwaki_migrate.build_table import _model_fields_of

        src = '    children: list = []\n    name: str = Field(serialization_alias="name")\n'
        assert _model_fields_of(src) == {"name": "name"}


class TestFreshness:
    def test_model_surface_matches_a_rebuild(self, table: dict) -> None:
        """Dev-repo guard: the committed table equals a fresh model-surface build.

        Needs the git history (the 1.x commit) — skipped where absent (public
        CI ships a single squashed commit).
        """
        import subprocess

        from tools.niwaki_migrate.build_table import LAST_1X_COMMIT, _build_model_surface

        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{LAST_1X_COMMIT}^{{commit}}"],
            capture_output=True,
        )
        if probe.returncode != 0:
            pytest.skip("1.x commit not in history (public export)")
        surface, _ = _build_model_surface()
        assert surface == table["surfaces"]["model_fields"]
