"""Tests for the vocabulary-candidate proposer (``_codegen/propose_vocabulary``).

The tool reads the local schema extraction (``data/schemas/``, never in the
repository) — like the pipeline-integrity tests, the whole module skips
where that data is absent.
"""

from __future__ import annotations

import pytest
import yaml

from niwaki._codegen.generate_domain import SCHEMA_DIR

pytestmark = pytest.mark.skipif(
    not SCHEMA_DIR.exists(),
    reason="requires the APIC schema data (data/schemas/, not in the repository)",
)


@pytest.fixture(scope="module")
def l3out_wave():  # type: ignore[no-untyped-def]
    from niwaki._codegen.propose_vocabulary import propose

    return propose(["l3extOut"], max_depth=4)


class TestMakers:
    def test_known_positions_are_proposed_with_childmap_names(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        """Maker names come verbatim from CHILD_MAP — the jargon agreement is
        by construction, not by whitelist."""
        root = l3out_wave.makers["l3extOut"]
        assert root["external_epg"][0] == "l3extInstP"
        assert root["node_profile"][0] == "l3extLNodeP"

    def test_rs_classes_are_never_makers(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        proposed = {
            child for table in l3out_wave.makers.values() for child, _flags in table.values()
        }
        assert not any(
            cls.split("Rs")[0] != cls and "Rs" in cls
            for cls in proposed
            if cls.startswith("l3extRs")
        )
        assert "l3extRsEctx" not in proposed

    def test_noisy_and_excluded_families_are_filtered(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        proposed = {
            child for table in l3out_wave.makers.values() for child, _flags in table.values()
        }
        assert "tagInst" not in proposed
        assert "tagAnnotation" not in proposed
        assert not any(cls.startswith(("cloud", "vns")) for cls in proposed)

    def test_max_depth_bounds_the_walk(self) -> None:
        from niwaki._codegen.propose_vocabulary import propose

        shallow = propose(["l3extOut"], max_depth=1)
        assert set(shallow.makers) == {"l3extOut"}

    def test_already_curated_positions_are_skipped(self) -> None:
        from niwaki._codegen.propose_vocabulary import propose

        wave = propose(["fvTenant"], max_depth=1)
        proposed = {child for child, _flags in wave.makers.get("fvTenant", {}).values()}
        assert "fvBD" not in proposed  # curated maker (tenant.bd)
        assert "fvCtx" not in proposed  # curated maker (tenant.vrf)
        assert wave.skipped_curated >= 6  # app/bd/vrf/l3out/filter/contract

    def test_long_names_carry_the_review_flag(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        flagged = [
            name
            for table in l3out_wave.makers.values()
            for name, (_child, flags) in table.items()
            if "long-name" in flags
        ]
        assert all(len(n) > 40 or n.count("_") >= 5 for n in flagged)


class TestBindsAndVerbs:
    def test_bind_aliases_come_from_reference_map(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        assert l3out_wave.binds["bfdIfP"]["bfd_interface_policy"][0] == "bfdIfPol"

    def test_already_curated_binds_are_skipped(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        targets = {t for t, _flags in l3out_wave.binds.get("l3extOut", {}).values()}
        assert "fvCtx" not in targets  # curated as l3out.bind(vrf=...)

    def test_contract_verbs_are_proposed_as_a_pair(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        verbs = l3out_wave.verbs["l3extInstP"]
        assert verbs["provide"] == {"rs": "fvRsProv", "target": "vzBrCP"}
        assert verbs["consume"] == {"rs": "fvRsCons", "target": "vzBrCP"}

    def test_curated_verb_owners_are_not_reproposed(self) -> None:
        from niwaki._codegen.propose_vocabulary import propose

        wave = propose(["fvTenant"], max_depth=3)
        assert "fvAEPg" not in wave.verbs  # epg verbs are curated already


class TestRender:
    def test_output_is_valid_yaml_with_the_vocabulary_shape(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        data = yaml.safe_load(l3out_wave.render())
        assert set(data) == {"makers", "binds", "verbs"}
        assert data["makers"]["l3extOut"]["external_epg"] == "l3extInstP"
        assert data["verbs"]["l3extInstP"]["provide"] == {"rs": "fvRsProv", "target": "vzBrCP"}

    def test_review_flags_survive_as_comments(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        text = l3out_wave.render()
        assert "# REVIEW:" in text

    def test_report_counts_match_the_tables(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        n_makers = sum(len(t) for t in l3out_wave.makers.values())
        assert f"{n_makers} makers" in l3out_wave.report()
