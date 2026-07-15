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


@pytest.fixture(scope="module")
def tenant_wave():  # type: ignore[no-untyped-def]
    from niwaki._codegen.propose_vocabulary import propose

    return propose(["fvTenant"], max_depth=4)


class TestMakers:
    def test_maker_names_agree_with_childmap(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        """Maker names come verbatim from CHILD_MAP — the jargon agreement is
        by construction, not by whitelist.  Invariant across waves."""
        from niwaki.domain._child_map import CHILD_MAP

        assert tenant_wave.makers, "the tenant subtree should still have uncurated classes"
        for parent, table in tenant_wave.makers.items():
            for name, (child, _flags) in table.items():
                assert CHILD_MAP[parent][name] == child

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

        # vmmDomP has a deep, uncurated subtree (VMM is a deferred wave), so it
        # stays a stable probe as the tenant fills in: a shallow walk reaches
        # strictly fewer parents than a deep one, and every parent it reaches a
        # deep walk reaches too.
        shallow = propose(["vmmDomP"], max_depth=1)
        deep = propose(["vmmDomP"], max_depth=6)
        assert set(shallow.makers) <= set(deep.makers)
        assert len(deep.makers) > len(shallow.makers)

    def test_already_curated_positions_are_skipped(self) -> None:
        from niwaki._codegen.propose_vocabulary import propose

        wave = propose(["fvTenant"], max_depth=1)
        proposed = {child for child, _flags in wave.makers.get("fvTenant", {}).values()}
        assert "fvBD" not in proposed  # curated maker (tenant.bd)
        assert "fvCtx" not in proposed  # curated maker (tenant.vrf)
        assert wave.skipped_curated >= 6  # app/bd/vrf/l3out/filter/contract

    def test_roots_are_anchored_under_curated_parents(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        """A wave root contained by an already-curated class gets its
        anchoring maker line proposed.  Wave-agnostic: the test picks any
        class the tenant subtree still leaves uncurated."""
        from niwaki._codegen.propose_vocabulary import propose

        candidates = [child for child, _flags in tenant_wave.makers.get("fvTenant", {}).values()]
        if not candidates:
            pytest.skip("every direct fvTenant child is curated — nothing to anchor")
        root = candidates[0]
        wave = propose([root], max_depth=1)
        anchored = {child for child, _flags in wave.makers.get("fvTenant", {}).values()}
        assert root in anchored

    def test_already_anchored_roots_are_not_reproposed(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        assert "l3extOut" not in {
            child for child, _flags in l3out_wave.makers.get("fvTenant", {}).values()
        }

    def test_allow_pierces_the_family_denylist(self) -> None:
        """--allow exempts specific classes from the excluded families.

        vnsMscGraphXlateCont (multi-site service-graph translation) is out of
        scope for good — a permanently stable probe.  (The service-graph model
        itself is now curated, so it is no longer denylisted wholesale.)
        """
        from niwaki._codegen.propose_vocabulary import propose

        closed = propose(["fvTenant"], max_depth=1)
        proposed_closed = {
            child for table in closed.makers.values() for child, _flags in table.values()
        }
        assert "vnsMscGraphXlateCont" not in proposed_closed

        opened = propose(["fvTenant"], max_depth=1, allow=frozenset({"vnsMscGraphXlateCont"}))
        proposed_open = {
            child for table in opened.makers.values() for child, _flags in table.values()
        }
        assert "vnsMscGraphXlateCont" in proposed_open

    def test_long_names_carry_the_review_flag(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        flagged = [
            name
            for table in l3out_wave.makers.values()
            for name, (_child, flags) in table.items()
            if "long-name" in flags
        ]
        assert all(len(n) > 40 or n.count("_") >= 5 for n in flagged)


class TestBindsAndVerbs:
    def test_bind_aliases_come_from_reference_map(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        """Every proposed (owner, target) pair resolves in REFERENCE_MAP and
        is not already curated.  Invariant across waves."""
        from niwaki.design._cursor import _tables
        from niwaki.domain._child_map import REFERENCE_MAP

        assert tenant_wave.binds
        for owner, table in tenant_wave.binds.items():
            curated = set(_tables().binds.get(owner, {}).values())
            for _alias, (target, _flags) in table.items():
                assert target in REFERENCE_MAP[owner]
                assert target not in curated

    def test_already_curated_binds_are_skipped(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        targets = {t for t, _flags in l3out_wave.binds.get("l3extOut", {}).values()}
        assert "fvCtx" not in targets  # curated as l3out.bind(vrf=...)

    def test_proposed_verbs_are_complete_pairs(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        """The pair-or-nothing rule: a verbs proposal always carries both
        provide and consume, each with an rs class and a target."""
        for owner, verbs in tenant_wave.verbs.items():
            assert set(verbs) == {"provide", "consume"}, owner
            for spec in verbs.values():
                assert set(spec) == {"rs", "target"}

    def test_curated_verb_owners_are_not_reproposed(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        assert "fvAEPg" not in tenant_wave.verbs  # epg verbs are curated
        assert "l3extInstP" not in tenant_wave.verbs  # curated in wave 1


class TestRender:
    def test_output_is_valid_yaml_with_the_vocabulary_shape(self, tenant_wave) -> None:  # type: ignore[no-untyped-def]
        data = yaml.safe_load(tenant_wave.render())
        assert set(data) == {"makers", "binds", "verbs"}
        for parent, table in data["makers"].items():
            assert tenant_wave.makers[parent].keys() == table.keys()

    def test_review_flags_survive_as_comments(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        text = l3out_wave.render()
        assert "# REVIEW:" in text

    def test_report_counts_match_the_tables(self, l3out_wave) -> None:  # type: ignore[no-untyped-def]
        n_makers = sum(len(t) for t in l3out_wave.makers.values())
        assert f"{n_makers} makers" in l3out_wave.report()
