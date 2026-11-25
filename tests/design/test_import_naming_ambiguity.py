"""Import under RN ambiguity — the attributes, then the kinds, never a guess.

An RN whose unbracketed naming value contains the literal that follows it
(``leaves-a-typ-b-typ-range``) admits several structural parses.  The import
layer settles them with two authorities, in order:

1. the node's own **attributes** — a snapshot (or a payload) carries every
   naming property, and the value it holds is the identity;
2. the catalogue **kind** of each naming property — an enum member, an
   integer or named number, a MAC, an IP, flags; free text admits anything.

What neither settles is a genuine ambiguity: a collected ``"structure"``
problem on the import paths, a :class:`~niwaki.exceptions.DesignError` on the
scope walk's attribute-less ancestors — never a silently wrong identity.

Several fixtures place an operational class directly under ``polUni``: the
importer trusts the snapshot's containment (the fabric is the authority on its
own edges), and those classes carry the only shipped formats where a MAC or a
named number is what tells two parses apart.
"""

from __future__ import annotations

import time

import pytest

from niwaki._dn import AmbiguousRnError
from niwaki._json import JsonObject, JsonValue
from niwaki.design import Cursor, design, from_payload, to_design
from niwaki.design._import import (
    _bare_ancestor,
    _grammar_children,
    _Importer,
    _kind_admits,
    _resolve_child_class,
    _resolve_naming,
)
from niwaki.design._node import RawDesignNode
from niwaki.exceptions import DesignError, SnapshotImportError
from niwaki.query._catalog import catalog
from niwaki.snapshot import REDACTED, Snapshot, _TreeNode

# ── Helpers ───────────────────────────────────────────────────────────────────


def _node(
    aci_class: str,
    rn: str,
    attributes: JsonObject | None = None,
    children: list[JsonValue] | None = None,
) -> JsonObject:
    """One snapshot tree node, in the serialised JSON shape."""
    return {
        "class": aci_class,
        "rn": rn,
        "attributes": attributes if attributes is not None else {},
        "children": children if children is not None else [],
    }


def _uni(*children: JsonObject) -> Snapshot:
    """A whole-config snapshot rooted on polUni."""
    return Snapshot(scope="uni", tree=_node("polUni", "uni", {}, list(children)))


def _leaf_selector_snapshot(selector_attrs: JsonObject) -> Snapshot:
    """polUni → infra → leaf profile ``lp`` → the ambiguous leaf selector."""
    selector = _node("infraLeafS", "leaves-a-typ-b-typ-range", selector_attrs)
    profile = _node("infraNodeP", "nprof-lp", {"name": "lp"}, [selector])
    return _uni(_node("infraInfra", "infra", {}, [profile]))


def _user_self(rn: str, attributes: JsonObject | None = None) -> JsonObject:
    """polUni → userext → userconf → one ``aaaUserSelf`` (two free-text props)."""
    conf = _node("aaaUserConf", "userconf", {}, [_node("aaaUserSelf", rn, attributes)])
    return _node("aaaUserEp", "userext", {}, [conf])


def _envelopes(value: JsonValue, aci_class: str) -> list[JsonObject]:
    """Every ``aci_class`` envelope body anywhere in a payload, in document order."""
    found: list[JsonObject] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == aci_class and isinstance(inner, dict):
                found.append(inner)
            found.extend(_envelopes(inner, aci_class))
    elif isinstance(value, list):
        for item in value:
            found.extend(_envelopes(item, aci_class))
    return found


def _attrs_of(cfg: Cursor, aci_class: str) -> JsonObject:
    """The wire attributes the imported design emits for its one *aci_class*."""
    (body,) = _envelopes(cfg.to_payload(), aci_class)
    attributes = body.get("attributes", {})
    assert isinstance(attributes, dict)
    return attributes


def _naming(cfg: Cursor, aci_class: str, *props: str) -> dict[str, JsonValue]:
    attributes = _attrs_of(cfg, aci_class)
    return {prop: attributes.get(prop) for prop in props}


def _problems(snapshot: Snapshot) -> SnapshotImportError:
    with pytest.raises(SnapshotImportError) as exc:
        to_design(snapshot)
    return exc.value


# ── Authority 1: the attributes ───────────────────────────────────────────────


class TestAttributesAreTheAuthority:
    def test_from_payload_restores_a_selector_named_with_its_separator(self) -> None:
        original = design()
        original.infra().leaf_profile("lp").leaf_selector("a-typ-b", "range")
        clone = from_payload(original.to_payload())
        assert _naming(clone, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }
        assert clone.to_payload() == original.to_payload()

    def test_snapshot_attributes_settle_the_leaf_selector(self) -> None:
        cfg = to_design(_leaf_selector_snapshot({"name": "a-typ-b", "type": "range"}))
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_attributes_settle_what_no_kind_can(self) -> None:
        # Two free-text props: only the attributes know where the split is.
        cfg = to_design(_uni(_user_self("userself-a-b-c", {"uType": "a-b", "name": "c"})))
        assert _naming(cfg, "aaaUserSelf", "uType", "name") == {"uType": "a-b", "name": "c"}

    def test_one_wire_named_attribute_is_authority_enough(self) -> None:
        # The snapshot speaks wire names (uType); the generated model's RN
        # format speaks Python field names (user_type).  The attribute must be
        # matched across that boundary — on its own it settles the split.
        cfg = to_design(_uni(_user_self("userself-a-b-c", {"uType": "a-b"})))
        assert _naming(cfg, "aaaUserSelf", "uType", "name") == {"uType": "a-b", "name": "c"}

    def test_the_wire_named_selector_type_attribute_is_honoured(self) -> None:
        # Kind alone admits only "range" here; the attribute must not be
        # mis-keyed into a refusal of that very parse.
        cfg = to_design(_leaf_selector_snapshot({"type": "range"}))
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_attributes_pick_the_first_occurrence_parse_too(self) -> None:
        cfg = to_design(_uni(_user_self("userself-a-b-c", {"uType": "a", "name": "b-c"})))
        assert _naming(cfg, "aaaUserSelf", "uType", "name") == {"uType": "a", "name": "b-c"}

    def test_from_payload_settles_two_free_text_props(self) -> None:
        payload: JsonObject = {
            "polUni": {
                "attributes": {},
                "children": [
                    {
                        "aaaUserEp": {
                            "attributes": {},
                            "children": [
                                {
                                    "aaaUserConf": {
                                        "attributes": {},
                                        "children": [
                                            {
                                                "aaaUserSelf": {
                                                    "attributes": {"uType": "a-b", "name": "c"}
                                                }
                                            }
                                        ],
                                    }
                                }
                            ],
                        }
                    }
                ],
            }
        }
        cfg = from_payload(payload)
        assert _naming(cfg, "aaaUserSelf", "uType", "name") == {"uType": "a-b", "name": "c"}

    def test_a_scoped_pod_filter_keeps_its_kubernetes_key(self) -> None:
        rn = "podfilter-my-app-in"
        snap = Snapshot(
            scope=f"uni/tn-t/pol-p/subj-s/rule-r/filterContainer/{rn}",
            tree=_node("hostprotPodFilter", rn, {"key": "my-app", "operator": "in"}),
        )
        assert _naming(to_design(snap), "hostprotPodFilter", "key", "operator") == {
            "key": "my-app",
            "operator": "in",
        }

    def test_the_catalogue_raw_route_honours_the_attributes(self) -> None:
        # acllogDropL2Flow is outside the generated models: the catalogue
        # route parses its RN; two free-text EPG names meet at "-depgname-".
        rn = (
            "dropl2flow-spctag-1-dpctag-2-sepgname-a-depgname-b-depgname-c"
            "-smac-00:11:22:33:44:55-dmac-00:11:22:33:44:66-sintf-[eth1/1]-vlan-10"
        )
        attrs: JsonObject = {"srcEpgName": "a-depgname-b", "dstEpgName": "c"}
        cfg = to_design(_uni(_node("acllogDropL2Flow", rn, attrs)))
        assert _naming(cfg, "acllogDropL2Flow", "srcEpgName", "dstEpgName") == {
            "srcEpgName": "a-depgname-b",
            "dstEpgName": "c",
        }


# ── Authority 2: the catalogue kind ───────────────────────────────────────────


class TestKindSettlesWhenAttributesAreSilent:
    def test_enum_absent_attributes(self) -> None:
        cfg = to_design(_leaf_selector_snapshot({}))
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_enum_redacted_attributes_are_no_authority(self) -> None:
        snap = _leaf_selector_snapshot({"name": REDACTED, "type": REDACTED})
        cfg = to_design(snap, redacted="skip")
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_enum_attributes_matching_no_parse_fall_back_to_the_kind(self) -> None:
        # An attribute value no parse produces cannot be the authority; the
        # chain falls back to the kind, which still settles the selector.
        cfg = to_design(_leaf_selector_snapshot({"name": "zzz"}))
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_enum_scoped_pod_filter_without_attributes(self) -> None:
        rn = "podfilter-my-app-in"
        snap = Snapshot(
            scope=f"uni/tn-t/pol-p/subj-s/rule-r/filterContainer/{rn}",
            tree=_node("hostprotPodFilter", rn),
        )
        assert _naming(to_design(snap), "hostprotPodFilter", "key", "operator") == {
            "key": "my-app",
            "operator": "in",
        }

    def test_integer(self) -> None:
        rn = "trapdest-h-port-1-port-162"
        snap = Snapshot(scope=f"uni/fabric/snmpgroup-g/{rn}", tree=_node("snmpTrapDest", rn))
        assert _naming(to_design(snap), "snmpTrapDest", "host", "port") == {
            "host": "h-port-1",
            "port": "162",
        }

    def test_named_number(self) -> None:
        # shard is an integer whose catalogue names "local"/"undefined";
        # the other two parses put "b" / "b-local" there — neither admissible.
        cfg = to_design(_uni(_node("syntheticClusterTestShardInstance", "cltsi-a-b-local-7")))
        assert _naming(cfg, "syntheticClusterTestShardInstance", "svc", "shard", "systemId") == {
            "svc": "a-b",
            "shard": "local",
            "systemId": "7",
        }

    def test_mac(self) -> None:
        # Three structural parses; "eti" (integer) drops one, and only the
        # MAC check drops the parse whose MAC swallowed "-eti-2-mac-".
        rn = "evpnmaciprt-r-eti-1-mac-m-eti-2-mac-00:11:22:33:44:55-ip-[10.0.0.1/32]"
        cfg = to_design(_uni(_node("bgpEVpnMacIpRoute", rn)))
        assert _naming(cfg, "bgpEVpnMacIpRoute", "rd", "eti", "mac", "pfx") == {
            "rd": "r-eti-1-mac-m",
            "eti": "2",
            "mac": "00:11:22:33:44:55",
            "pfx": "10.0.0.1/32",
        }

    def test_unambiguous_rns_import_as_before(self) -> None:
        cfg = to_design(_uni(_user_self("userself-a-bc")))
        assert _naming(cfg, "aaaUserSelf", "uType", "name") == {"uType": "a", "name": "bc"}


# ── Genuine ambiguity: collected, never guessed ───────────────────────────────


class TestGenuineAmbiguityIsCollected:
    def test_two_free_text_props_without_attributes(self) -> None:
        dn = "uni/userext/userconf/userself-a-b-c"
        error = _problems(_uni(_user_self("userself-a-b-c")))
        (problem,) = error.problems
        assert problem.dn == dn
        assert problem.kind == "structure"
        assert "ambiguous" in problem.detail

    def test_free_text_attributes_matching_no_parse_stay_ambiguous(self) -> None:
        error = _problems(_uni(_user_self("userself-a-b-c", {"uType": "x", "name": "y"})))
        (problem,) = error.problems
        assert problem.kind == "structure"
        assert "ambiguous" in problem.detail

    def test_the_catalogue_raw_route_collects_it(self) -> None:
        rn = (
            "dropl2flow-spctag-1-dpctag-2-sepgname-a-depgname-b-depgname-c"
            "-smac-00:11:22:33:44:55-dmac-00:11:22:33:44:66-sintf-[eth1/1]-vlan-10"
        )
        error = _problems(_uni(_node("acllogDropL2Flow", rn)))
        (problem,) = error.problems
        assert problem.dn == f"uni/{rn}"
        assert problem.kind == "structure"
        assert "ambiguous" in problem.detail

    def test_a_maker_route_rn_no_kind_admits_is_collected(self) -> None:
        # "c" and "b-typ-c" are both outside the enum: the kind admits no
        # parse, the structural fallback finds two — ambiguous.
        selector = _node("infraLeafS", "leaves-a-typ-b-typ-c")
        profile = _node("infraNodeP", "nprof-lp", {"name": "lp"}, [selector])
        error = _problems(_uni(_node("infraInfra", "infra", {}, [profile])))
        (problem,) = error.problems
        assert problem.dn == "uni/infra/nprof-lp/leaves-a-typ-b-typ-c"
        assert problem.kind == "structure"
        assert "ambiguous" in problem.detail

    def test_collected_with_the_other_problems(self) -> None:
        tenant = _node("fvTenant", "tn-prod", {"name": "prod", "brandNewProp": "x"})
        error = _problems(
            _uni(
                _node(
                    "aaaUserEp",
                    "userext",
                    {},
                    [
                        _node(
                            "aaaUserConf",
                            "userconf",
                            {},
                            [
                                _node("aaaUserSelf", "userself-a-b-c"),
                                _node("aaaUserSelf", "userself-d-e-f"),
                            ],
                        )
                    ],
                ),
                tenant,
            )
        )
        by_dn = {(p.dn, p.kind) for p in error.problems}
        assert by_dn == {
            ("uni/userext/userconf/userself-a-b-c", "structure"),
            ("uni/userext/userconf/userself-d-e-f", "structure"),
            ("uni/tn-prod", "unknown-property"),
        }
        assert all("ambiguous" in p.detail for p in error.problems if p.kind == "structure")


# ── The scope walk: class resolution and attribute-less ancestors ─────────────


class TestScopeWalk:
    def test_an_ambiguous_segment_still_resolves_its_class(self) -> None:
        # Structurally ambiguous, and no kind admits it — but the segment does
        # parse, so it names exactly one child class.
        assert _resolve_child_class("infraNodeP", "leaves-a-typ-b-typ-c") == "infraLeafS"
        assert _resolve_child_class("aaaUserConf", "userself-a-b-c") == "aaaUserSelf"

    def test_the_grammar_authority_counts_an_ambiguous_segment_as_a_match(self) -> None:
        assert "aaaUserSelf" in _grammar_children("aaaUserConf", "userself-a-b-c")

    def test_a_bare_ancestor_settled_by_its_kind_imports(self) -> None:
        snap = Snapshot(
            scope="uni/infra/nprof-lp/leaves-a-typ-b-typ-range/nodeblk-blk1",
            tree=_node("infraNodeBlk", "nodeblk-blk1", {"name": "blk1"}),
        )
        cfg = to_design(snap)
        assert _naming(cfg, "infraLeafS", "name", "type") == {
            "name": "a-typ-b",
            "type": "range",
        }

    def test_an_ambiguous_bare_ancestor_is_refused(self) -> None:
        segment = "userself-a-b-c"
        snap = Snapshot(
            scope=f"uni/userext/userconf/{segment}/pref",
            tree=_node("aaaUserPreferences", "pref"),
        )
        with pytest.raises(DesignError, match="ambiguous") as exc:
            to_design(snap)
        assert segment in str(exc.value)
        assert "no child class" not in str(exc.value)

    def test_a_bare_ancestor_no_kind_admits_is_refused(self) -> None:
        segment = "leaves-a-typ-b-typ-c"
        snap = Snapshot(
            scope=f"uni/infra/nprof-lp/{segment}/nodeblk-blk1",
            tree=_node("infraNodeBlk", "nodeblk-blk1", {"name": "blk1"}),
        )
        with pytest.raises(DesignError, match="ambiguous") as exc:
            to_design(snap)
        assert segment in str(exc.value)
        assert "no child class" not in str(exc.value)


# ── The (class, primary name) index of the pre-pass ───────────────────────────


def _indexed(aci_class: str, rn: str, attributes: dict[str, JsonValue]) -> _Importer:
    importer = _Importer(
        on_unknown="raise",
        redacted="raise",
        problems=[],
        class_of={},
        primary={},
        names={},
    )
    importer.index(_TreeNode(aci_class, rn, attributes, []), f"uni/parent/{rn}")
    return importer


class TestPrimaryIndex:
    def test_attributes_name_the_primary(self) -> None:
        rn = "leaves-a-typ-b-typ-range"
        importer = _indexed("infraLeafS", rn, {"name": "a-typ-b", "type": "range"})
        assert importer.primary[f"uni/parent/{rn}"] == "a-typ-b"
        assert importer.names["infraLeafS"] == {"a-typ-b": [f"uni/parent/{rn}"]}

    def test_the_kind_names_the_primary_without_attributes(self) -> None:
        rn = "leaves-a-typ-b-typ-range"
        importer = _indexed("infraLeafS", rn, {})
        assert importer.primary[f"uni/parent/{rn}"] == "a-typ-b"

    def test_attributes_settle_two_free_text_props(self) -> None:
        rn = "userself-a-b-c"
        importer = _indexed("aaaUserSelf", rn, {"uType": "a-b", "name": "c"})
        assert importer.primary[f"uni/parent/{rn}"] == "a-b"

    def test_an_ambiguous_rn_indexes_by_dn_only(self) -> None:
        rn = "userself-a-b-c"
        importer = _indexed("aaaUserSelf", rn, {})
        assert importer.primary[f"uni/parent/{rn}"] == ""
        assert "aaaUserSelf" not in importer.names
        assert importer.class_of[f"uni/parent/{rn}"] == "aaaUserSelf"
        assert not importer.problems  # the walk reports it, not the index


# ── Round 2 (adversarial review) ──────────────────────────────────────────────

_SELECTOR_FMT = "leaves-{name}-typ-{type}"
_ANOMALY_FMT = "anomaly-{nodeName}-{resource}-{severity}-{startTs}"


class TestAttributesAloneComeFirst:
    """Round 2, finding 1: the attributes are the identity, even a wrong one.

    Step one of the chain is equality to the attributes ALONE.  One parse
    left → it wins, and the model's validation reports the invalid value
    (as before the parser change).  Two or more left (incomplete attributes)
    → the kinds narrow those.  None left → the kinds, then the structure.
    """

    def test_from_payload_reports_the_invalid_type_again(self) -> None:
        payload: JsonObject = {
            "polUni": {
                "attributes": {},
                "children": [
                    {
                        "infraInfra": {
                            "attributes": {},
                            "children": [
                                {
                                    "infraNodeP": {
                                        "attributes": {"name": "np"},
                                        "children": [
                                            {
                                                "infraLeafS": {
                                                    "attributes": {
                                                        "name": "a",
                                                        "type": "range-typ-ALL",
                                                    }
                                                }
                                            }
                                        ],
                                    }
                                }
                            ],
                        }
                    }
                ],
            }
        }
        with pytest.raises(SnapshotImportError) as exc:
            from_payload(payload)
        (problem,) = exc.value.problems
        assert problem.dn == "uni/infra/nprof-np/leaves-a-typ-range-typ-ALL"
        assert problem.kind == "invalid-value"

    def test_a_snapshot_attribute_the_kind_contradicts_still_names_the_object(self) -> None:
        # name="a" leaves one parse (type "b-typ-range"); the kind would have
        # preferred name "a-typ-b" — the attribute wins, the model refuses.
        error = _problems(_leaf_selector_snapshot({"name": "a"}))
        (problem,) = error.problems
        assert problem.dn == "uni/infra/nprof-lp/leaves-a-typ-b-typ-range"
        assert problem.kind == "invalid-value"

    def test_one_parse_left_by_the_attributes_wins_over_the_kind(self) -> None:
        assert _resolve_naming(
            "leaves-a-typ-range-typ-ALL",
            _SELECTOR_FMT,
            "infraLeafS",
            attrs={"name": "a", "type": "range-typ-ALL"},
        ) == {"name": "a", "type": "range-typ-ALL"}

    def test_several_parses_left_by_the_attributes_are_narrowed_by_kind(self) -> None:
        # nodeName="n" leaves three parses of "r-x-ERROR-t"; only one puts an
        # enum member in severity.
        assert _resolve_naming(
            "anomaly-n-r-x-ERROR-t", _ANOMALY_FMT, "apAnomaly", attrs={"nodeName": "n"}
        ) == {"nodeName": "n", "resource": "r-x", "severity": "ERROR", "startTs": "t"}

    def test_without_the_attribute_the_same_rn_is_ambiguous(self) -> None:
        with pytest.raises(AmbiguousRnError):
            _resolve_naming("anomaly-n-r-x-ERROR-t", _ANOMALY_FMT, "apAnomaly")

    def test_attributes_and_kind_leaving_several_parses_are_ambiguous(self) -> None:
        with pytest.raises(AmbiguousRnError):
            _resolve_naming(
                "anomaly-n-r-x-ERROR-t", _ANOMALY_FMT, "apAnomaly", attrs={"startTs": "t"}
            )

    def test_attributes_matching_no_parse_fall_back_to_the_kind(self) -> None:
        assert _resolve_naming(
            "leaves-a-typ-b-typ-range", _SELECTOR_FMT, "infraLeafS", attrs={"name": "zzz"}
        ) == {"name": "a-typ-b", "type": "range"}

    def test_then_to_the_structure(self) -> None:
        # Neither "c" nor "b-typ-c" is a type: the kinds admit nothing, the
        # structure has two parses — ambiguous.
        with pytest.raises(AmbiguousRnError):
            _resolve_naming(
                "leaves-a-typ-b-typ-c", _SELECTOR_FMT, "infraLeafS", attrs={"name": "zzz"}
            )

    def test_a_unique_structure_ignores_contradicting_attributes(self) -> None:
        assert _resolve_naming(
            "leaves-a-typ-c", _SELECTOR_FMT, "infraLeafS", attrs={"name": "zzz"}
        ) == {"name": "a", "type": "c"}


def _anomaly(dashes: int) -> tuple[str, dict[str, str]]:
    """An adversarial apAnomaly RN — three free-text values full of dashes."""
    node = "-".join(["n"] * (dashes + 1))
    resource = "-".join(["r"] * (dashes + 1))
    stamp = "-".join(["t"] * (dashes + 1))
    naming = {"nodeName": node, "resource": resource, "severity": "ERROR", "startTs": stamp}
    return f"anomaly-{node}-{resource}-ERROR-{stamp}", naming


class TestResolutionCost:
    """Round 2, finding 2: settling an RN never pays the full enumeration."""

    def test_attributes_settle_an_explosive_rn_fast(self) -> None:
        rn, naming = _anomaly(60)
        start = time.perf_counter()
        got = _resolve_naming(rn, _ANOMALY_FMT, "apAnomaly", attrs=dict(naming))
        assert time.perf_counter() - start < 0.5
        assert got == naming

    def test_an_unsettled_explosive_rn_fails_fast_and_small(self) -> None:
        rn, _ = _anomaly(60)
        start = time.perf_counter()
        with pytest.raises(AmbiguousRnError) as exc:
            _resolve_naming(rn, _ANOMALY_FMT, "apAnomaly")
        assert time.perf_counter() - start < 1.0
        assert len(str(exc.value)) < 5_000

    def test_to_design_settles_it_fast(self) -> None:
        rn, naming = _anomaly(60)
        attrs: JsonObject = dict(naming)
        start = time.perf_counter()
        cfg = to_design(_uni(_node("apAnomaly", rn, attrs)))
        assert time.perf_counter() - start < 1.0
        assert _naming(cfg, "apAnomaly", *naming) == dict(naming)

    def test_an_unsettled_import_problem_stays_small(self) -> None:
        rn, _ = _anomaly(60)
        error = _problems(_uni(_node("apAnomaly", rn)))
        (problem,) = error.problems
        assert problem.kind == "structure"
        assert "ambiguous" in problem.detail
        assert len(problem.detail) < 5_000

    def test_class_resolution_only_probes_for_one_parse(self) -> None:
        segment = "mChassis-" + "-".join(["v"] * 801)
        start = time.perf_counter()
        assert _resolve_child_class("infraInfra", segment) == "vnsMChassis"
        assert time.perf_counter() - start < 0.25

    def test_the_grammar_authority_only_probes_for_one_parse(self) -> None:
        segment = "mChassis-" + "-".join(["v"] * 801)
        start = time.perf_counter()
        assert "vnsMChassis" in _grammar_children("infraInfra", segment)
        assert time.perf_counter() - start < 0.5


class TestUnknownKinds:
    """Round 2, finding 3: a class the catalogue cannot describe admits anything.

    ``smarteventRuleInst`` is one of five classes whose catalogue description
    raises (a field-name collision); its kinds are unknowable, so its values
    are free text — never an escaped ValueError from the catalogue.
    """

    _FMT = "ruleinst-{genericRuleName}-{eventID}"

    def test_an_ambiguous_rn_is_an_ambiguity_not_a_catalogue_error(self) -> None:
        with pytest.raises(AmbiguousRnError):
            _resolve_naming("ruleinst-a-b-5", self._FMT, "smarteventRuleInst")

    def test_attributes_still_settle_it(self) -> None:
        assert _resolve_naming(
            "ruleinst-a-b-5",
            self._FMT,
            "smarteventRuleInst",
            attrs={"genericRuleName": "a-b"},
        ) == {"genericRuleName": "a-b", "eventID": "5"}

    def test_a_bare_ancestor_raises_only_a_design_error(self) -> None:
        root = design()
        rule_def = RawDesignNode("smarteventRuleDef", {}, "ruledef-r", {}, root.design_node)
        with pytest.raises(DesignError, match="ambiguous") as exc:
            _bare_ancestor(Cursor(rule_def), "eventstore/ruledef-r", "ruleinst-a-b-5")
        assert "ruleinst-a-b-5" in str(exc.value)


class TestIpNamedMembers:
    """Round 2, finding 5: an ``ip`` property admits its catalogue-named members.

    No import path can reach this (no shipped format puts an unbracketed IP
    next to free text), so the kind helper is pinned directly.
    """

    def test_wildcard_source_is_admitted(self) -> None:
        members = frozenset(catalog().prop_meta("igmpSnoopStaticGroup", "source").enum_values)
        assert "wildcard" in members
        assert _kind_admits("ip", members, "wildcard") is True

    def test_backbone_area_is_admitted(self) -> None:
        members = frozenset(catalog().prop_meta("ospfArea", "id").enum_values)
        assert "backbone" in members
        assert _kind_admits("ip", members, "backbone") is True

    def test_addresses_are_still_admitted(self) -> None:
        members = frozenset({"defaultValue", "wildcard"})
        assert _kind_admits("ip", members, "10.0.0.1") is True
        assert _kind_admits("ip", members, "10.0.0.0/8") is True
        assert _kind_admits("ip", frozenset(), "2001:db8::1") is True

    def test_anything_else_is_refused(self) -> None:
        members = frozenset({"defaultValue", "wildcard"})
        assert _kind_admits("ip", members, "not-an-address") is False
        assert _kind_admits("ip", frozenset(), "wildcard") is False
