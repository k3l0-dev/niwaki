"""The deterministic snapshot — assembly, redaction, serialisation, diff.

Everything here is offline: ``_assemble`` is the pure second half of ``take``
(the shipped catalogue provides the per-class prop flags), and ``diff`` is
pure over two snapshots.  The live properties — byte-determinism on a real
fabric, surgical drift, cleartext echo of the curated secret — were proven on
the 6.0(9c) sim and are re-provable there; these tests pin the logic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from niwaki import snapshot as snap
from niwaki.query import _catalog
from niwaki.snapshot import REDACTED, Snapshot, SnapshotDiff, _assemble, _snapshot_universe


def _flat(cls: str, dn: str, **attrs: str) -> dict[str, Any]:
    return {cls: {"attributes": {"dn": dn, **attrs}}}


_KEEP = frozenset(
    {"fvTenant", "fvBD", "fvCtx", "fvSubnet", "vnsCCred", "vnsLDevVip", "snmpCommunityP"}
)


class TestAssemble:
    def test_builds_a_sorted_tree_regardless_of_input_order(self) -> None:
        items = [
            _flat("fvCtx", "uni/tn-p/ctx-b", name="b"),
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-a", name="a"),
        ]
        one = _assemble("uni/tn-p", items, _KEEP)
        other = _assemble("uni/tn-p", list(reversed(items)), _KEEP)
        assert one.to_json() == other.to_json()
        assert one.tree is not None
        assert [c["rn"] for c in one.tree["children"]] == ["BD-a", "ctx-b"]

    def test_non_configurable_properties_are_dropped(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-a", name="a", modTs="2026-08-10T10:00:00", lcOwn="local"),
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.tree is not None
        (bd,) = result.tree["children"]
        assert "modTs" not in bd["attributes"] and "lcOwn" not in bd["attributes"]
        assert bd["attributes"]["name"] == "a"

    def test_a_curated_secret_value_is_redacted(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("vnsLDevVip", "uni/tn-p/lDevVip-fw", name="fw"),
            _flat("vnsCCred", "uni/tn-p/lDevVip-fw/cCred", name="admin", value="S3cret"),
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        text = result.to_json()
        assert "S3cret" not in text
        assert REDACTED in text

    def test_a_secure_flagged_value_is_redacted_if_ever_echoed(self) -> None:
        """The APIC omits secure props (measured), but defence stays in depth."""
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("vnsLDevVip", "uni/tn-p/lDevVip-fw", name="fw"),
            _flat("vnsCCredSecret", "uni/tn-p/lDevVip-fw/cCredSecret", value="S3cret"),
        ]
        keep = _KEEP | {"vnsCCredSecret"}
        assert "S3cret" not in _assemble("uni/tn-p", items, keep).to_json()

    def test_a_dn_secret_object_is_reported(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("snmpCommunityP", "uni/tn-p/community-s3cret", name="s3cret"),
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert len(result.warnings) == 1
        assert "snmpCommunityP" in result.warnings[0]
        assert "community-s3cret" in result.warnings[0]

    def test_an_absent_scope_yields_no_tree(self) -> None:
        assert _assemble("uni/tn-p", [], _KEEP).tree is None

    def test_a_non_keep_node_survives_only_as_shelter(self) -> None:
        """A non-exportable container is kept when an exportable child needs
        the chain, pruned when it shelters nothing."""
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("aaaUserEp", "uni/tn-p/userext"),
            _flat("fvBD", "uni/tn-p/userext/BD-x", name="x"),  # contrived child
            _flat("aaaUserEp", "uni/tn-p/userext2"),  # shelters nothing
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.tree is not None
        rns = [c["rn"] for c in result.tree["children"]]
        assert "userext" in rns and "userext2" not in rns

    def test_coverage_counts_kept_classes_only(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("fvBD", "uni/tn-p/BD-a", name="a"),
            _flat("fvBD", "uni/tn-p/BD-b", name="b"),
            _flat("aaaUserEp", "uni/tn-p/userext"),  # pruned, not counted
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.coverage == {"fvTenant": 1, "fvBD": 2}

    def test_coverage_never_counts_what_the_tree_omits(self) -> None:
        """A kept node in a disconnected component (its parent chain to the
        root is broken) must not be counted — the fact and its tally agree.

        Regression for the adversarial-review finding: coverage was read off
        an ``attached`` set that admitted nodes the pruned tree dropped.
        """
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            # inner BD whose parent uni/tn-p/gap is absent from the read
            _flat("fvBD", "uni/tn-p/gap/BD-orphan", name="orphan"),
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.tree is not None
        assert result.tree["children"] == []  # nothing reached the root
        assert result.coverage == {"fvTenant": 1}  # and nothing is over-counted

    def test_a_dn_secret_object_pruned_from_the_tree_does_not_warn(self) -> None:
        """A login session read only as a structural parent is pruned, and so
        must not warn — its token-hash DN must never enter the artifact.

        Regression for the review finding that broke byte-determinism (the
        warning set varied with who was logged in) and leaked token hashes.
        """
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            # A login session read only because it parents a tag; not in keep,
            # shelters nothing here, so it is pruned before accounting runs.
            _flat("aaaActiveUserSession", "uni/tn-p/actsession-TOKENHASH"),
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.warnings == ()
        assert "TOKENHASH" not in result.to_json()

    def test_a_surviving_dn_secret_object_warns_once(self) -> None:
        items = [
            _flat("fvTenant", "uni/tn-p", name="p"),
            _flat("snmpCommunityP", "uni/tn-p/community-s3cret", name="s3cret"),
            _flat("snmpCommunityP", "uni/tn-p/community-s3cret", name="s3cret"),  # dup page echo
        ]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert len(result.warnings) == 1

    def test_an_unknown_root_class_keeps_its_attributes_verbatim(self) -> None:
        """The scope root is read unfiltered and can be any class; a class the
        catalogue does not know must not crash the capture (review nit)."""
        items = [{"zzNotAClass": {"attributes": {"dn": "uni/tn-p", "custom": "v"}}}]
        result = _assemble("uni/tn-p", items, _KEEP)
        assert result.tree is not None
        assert result.tree["attributes"] == {"custom": "v"}


class TestSerialisation:
    def test_round_trip(self) -> None:
        items = [_flat("fvTenant", "uni/tn-p", name="p", descr="d")]
        original = _assemble("uni/tn-p", items, _KEEP)
        again = Snapshot.from_json(original.to_json())
        assert again == original

    def test_not_a_snapshot_document_raises(self) -> None:
        with pytest.raises(KeyError):
            Snapshot.from_json(json.dumps({"something": "else"}))

    def test_wire_format_only(self) -> None:
        """The capture speaks APIC names, never the SDK's readable names."""
        items = [
            _flat("fvBD", "uni/tn-p/BD-a", name="a", unicastRoute="yes"),
            _flat("fvTenant", "uni/tn-p", name="p"),
        ]
        text = _assemble("uni/tn-p", items, _KEEP).to_json()
        assert "unicastRoute" in text and "unicast_routing" not in text


class TestDiff:
    def _snap(self, *items: dict[str, Any]) -> Snapshot:
        return _assemble("uni/tn-p", [_flat("fvTenant", "uni/tn-p", name="p"), *items], _KEEP)

    def test_no_changes(self) -> None:
        a = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a"))
        b = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a"))
        delta = snap.diff(a, b)
        assert not delta.has_changes
        assert delta == SnapshotDiff(added=(), removed=(), changed={})

    def test_added_and_removed(self) -> None:
        a = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a"))
        b = self._snap(_flat("fvCtx", "uni/tn-p/ctx-c", name="c"))
        delta = snap.diff(a, b)
        assert delta.added == ("uni/tn-p/ctx-c",)
        assert delta.removed == ("uni/tn-p/BD-a",)

    def test_changed_attributes_with_one_sided_values(self) -> None:
        a = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a", unicastRoute="yes"))
        b = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a", unicastRoute="no", descr="x"))
        delta = snap.diff(a, b)
        assert delta.changed == {
            "uni/tn-p/BD-a": {"descr": (None, "x"), "unicastRoute": ("yes", "no")}
        }

    def test_scope_mismatch_raises(self) -> None:
        a = self._snap()
        b = Snapshot(scope="uni", tree=None)
        with pytest.raises(ValueError, match="scopes differ"):
            snap.diff(a, b)

    def test_a_bracketed_rn_diffs_at_the_right_dn(self) -> None:
        a = self._snap(_flat("fvBD", "uni/tn-p/BD-a", name="a"))
        b = self._snap(
            _flat("fvBD", "uni/tn-p/BD-a", name="a"),
            _flat("fvSubnet", "uni/tn-p/BD-a/subnet-[10.0.0.1/24]", ip="10.0.0.1/24"),
        )
        delta = snap.diff(a, b)
        assert delta.added == ("uni/tn-p/BD-a/subnet-[10.0.0.1/24]",)


class TestUniverse:
    def test_keep_is_a_subset_of_request(self) -> None:
        request, keep = _snapshot_universe()
        assert keep <= request

    def test_the_universe_is_catalogue_shaped(self) -> None:
        _request, keep = _snapshot_universe()
        assert "fvTenant" in keep and "fvBD" in keep
        assert "polUni" in keep  # the whole-config scope root
        # Runtime state stays out of a configuration backup:
        assert "faultInst" not in keep  # FaultCurrent category (measured live)
        assert "aaaActiveUserSession" not in keep  # not exportable
        assert "topSystem" not in keep  # operational, not configuration

    def test_the_universe_is_stable(self) -> None:
        assert _snapshot_universe() == _snapshot_universe()

    def test_ancestor_closure_is_transitive(self) -> None:
        """Every concrete containment ancestor of a kept class is requested,
        not just the immediate parent — else a deep object drops as an orphan.
        """
        import sqlite3

        from niwaki.domain._child_map import CHILD_MAP

        request, keep = _snapshot_universe()
        parents_of: dict[str, set[str]] = {}
        for parent, children in CHILD_MAP.items():
            for child in children.values():
                parents_of.setdefault(child, set()).add(parent)
        con = sqlite3.connect(f"file:{_catalog.DEFAULT_PATH}?mode=ro", uri=True)
        concrete = {n for (n,) in con.execute("SELECT class_name FROM mo WHERE is_abstract=0")}
        con.close()

        missing: list[str] = []
        seen: set[str] = set()
        frontier = set(keep)
        while frontier:
            nxt: set[str] = set()
            for cls in frontier:
                for parent in parents_of.get(cls, ()):
                    if parent in seen:
                        continue
                    seen.add(parent)
                    nxt.add(parent)
                    if parent in concrete and parent not in request:
                        missing.append(parent)
            frontier = nxt
        assert not missing, f"concrete ancestors not requested: {missing[:10]}"
        # And the everyday deep chain is covered: tenant → BD → subnet.
        assert {"fvTenant", "fvBD", "fvSubnet"} <= request
