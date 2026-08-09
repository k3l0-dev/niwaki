"""The DN parser — the inverse of naming, and the foundation of reading a fabric.

The load-bearing property is that ``parse`` is the exact inverse of DN
computation across the whole model: a round-trip test fills every class's RN
format with sentinels and checks the parser recovers them, over all ~13,500
classes the shipped catalogue knows. The bespoke cases below pin the two things
a naive split or a single regex gets wrong — bracketed values that contain
slashes and nested brackets, and RNs carrying several values with literal
separators.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import niwaki
from niwaki._dn import DnParts, naming_values, parent_dn, parse, rn_of, split_dn


class TestSplitDn:
    def test_a_plain_dn_splits_on_slashes(self) -> None:
        assert split_dn("uni/tn-p/BD-w") == ["uni", "tn-p", "BD-w"]

    def test_a_slash_inside_brackets_is_not_a_separator(self) -> None:
        assert split_dn("uni/tn-p/BD-w/subnet-[10.0.1.1/24]") == [
            "uni",
            "tn-p",
            "BD-w",
            "subnet-[10.0.1.1/24]",
        ]

    def test_nested_brackets_are_one_segment(self) -> None:
        dn = "uni/tn-p/out-o/lnodep-n/lifp-l/rspathL3OutAtt-[topology/pod-1/pathep-[eth1/1]]"
        assert split_dn(dn)[-1] == "rspathL3OutAtt-[topology/pod-1/pathep-[eth1/1]]"

    def test_a_single_segment_stays_whole(self) -> None:
        assert split_dn("uni") == ["uni"]

    def test_empty_is_empty(self) -> None:
        assert split_dn("") == []


class TestParentAndRn:
    def test_parent_strips_the_last_segment(self) -> None:
        assert parent_dn("uni/tn-p/BD-w") == "uni/tn-p"

    def test_parent_of_a_bracketed_child(self) -> None:
        assert parent_dn("uni/tn-p/BD-w/subnet-[10.0.1.1/24]") == "uni/tn-p/BD-w"

    def test_a_top_level_object_has_no_parent(self) -> None:
        assert parent_dn("uni") is None
        assert parent_dn("topology") is None
        assert parent_dn("") is None

    def test_rn_is_the_last_segment(self) -> None:
        assert rn_of("uni/tn-p/BD-w") == "BD-w"
        assert rn_of("uni/tn-p/subnet-[10/8]") == "subnet-[10/8]"


class TestNamingValues:
    def test_a_single_value(self) -> None:
        assert naming_values("BD-web", "BD-{name}") == {"name": "web"}

    def test_a_fixed_rn_has_no_values(self) -> None:
        assert naming_values("rsctx", "rsctx") == {}

    def test_a_bracketed_value_may_contain_slashes(self) -> None:
        assert naming_values("subnet-[10.0.1.1/24]", "subnet-[{ip}]") == {"ip": "10.0.1.1/24"}

    def test_a_bracketed_value_may_contain_nested_brackets(self) -> None:
        rn = "rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
        assert naming_values(rn, "rspathL3OutAtt-[{tDn}]") == {
            "tDn": "topology/pod-1/paths-101/pathep-[eth1/1]"
        }

    def test_several_values_separated_by_literals(self) -> None:
        assert naming_values(
            "iprule-[uni/tn-x]-dom-common", "iprule-[{objectDn}]-dom-{domain}"
        ) == {"objectDn": "uni/tn-x", "domain": "common"}

    def test_two_bare_values_with_a_literal_between(self) -> None:
        assert naming_values("type-remote-user-alice", "type-{utype}-user-{username}") == {
            "utype": "remote",
            "username": "alice",
        }

    def test_the_worst_real_format_bare_and_bracketed_values_interleaved(self) -> None:
        """The intersection the sentinel round-trip cannot reach.

        ``acllogPermitL3Flow`` (13 naming props) mixes bare values with
        bracketed ones, and the bracketed ones carry slashes and colons — an IP
        prefix and a MAC. Pinned with realistic content so special characters
        inside a bracket sit next to literal separators outside it.
        """
        fmt = (
            "permitl3flow-spctag-{srcPcTag}-dpctag-{dstPcTag}-sepgname-{srcEpgName}"
            "-depgname-{dstEpgName}-sip-[{srcIp}]-dip-[{dstIp}]-proto-{protocol}"
            "-sport-{srcPort}-dport-{dstPort}-smac-{srcMacAddr}-dmac-{dstMacAddr}"
            "-sintf-[{srcIntf}]-vrfencap-{vrfEncap}"
        )
        rn = (
            "permitl3flow-spctag-32770-dpctag-16386-sepgname-web-depgname-db"
            "-sip-[10.0.1.5/32]-dip-[10.0.2.9/32]-proto-tcp-sport-443-dport-8080"
            "-smac-00:11:22:33:44:55-dmac-AA:BB:CC:DD:EE:FF"
            "-sintf-[topology/pod-1/pathep-[eth1/1]]-vrfencap-vxlan-2523136"
        )
        assert naming_values(rn, fmt) == {
            "srcPcTag": "32770",
            "dstPcTag": "16386",
            "srcEpgName": "web",
            "dstEpgName": "db",
            "srcIp": "10.0.1.5/32",
            "dstIp": "10.0.2.9/32",
            "protocol": "tcp",
            "srcPort": "443",
            "dstPort": "8080",
            "srcMacAddr": "00:11:22:33:44:55",
            "dstMacAddr": "AA:BB:CC:DD:EE:FF",
            "srcIntf": "topology/pod-1/pathep-[eth1/1]",
            "vrfEncap": "vxlan-2523136",
        }

    def test_a_mismatched_literal_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="does not match"):
            naming_values("VRF-web", "BD-{name}")

    def test_an_unterminated_bracket_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="unterminated bracket"):
            naming_values("subnet-[10.0.1.1/24", "subnet-[{ip}]")

    def test_trailing_content_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="trailing content"):
            naming_values("rsctx-extra", "rsctx")


class TestParse:
    def test_ties_the_pieces_together(self) -> None:
        assert parse("uni/tn-p/BD-web", "BD-{name}") == DnParts(
            parent="uni/tn-p", rn="BD-web", naming={"name": "web"}
        )

    def test_a_top_level_object_parses_with_no_parent(self) -> None:
        assert parse("uni/tn-prod", "tn-{name}") == DnParts(
            parent="uni", rn="tn-prod", naming={"name": "prod"}
        )


class TestEndToEnd:
    """A real DN through a real RN format, as a reader will drive it."""

    def test_parse_a_real_subnet_dn(self) -> None:
        dn = "uni/tn-prod/BD-web/subnet-[10.0.0.1/24]"
        parts = parse(dn, "subnet-[{ip}]")
        assert parts.parent == "uni/tn-prod/BD-web"
        assert parts.naming == {"ip": "10.0.0.1/24"}


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _catalog_rn_formats() -> list[tuple[str, str]]:
    """Every (class, rn_format) the shipped catalogue carries a format for."""
    db = Path(niwaki.__file__).parent / "query" / "_catalog" / "catalog.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT class_name, rn_format FROM mo WHERE rn_format != ''").fetchall()
    finally:
        con.close()


def test_parse_inverts_naming_for_every_class_in_the_catalogue() -> None:
    """The whole point: parse is the exact inverse of DN computation.

    For each class, fill its RN format with per-position sentinels, then parse
    the result back — every value must come out exactly as it went in. Run over
    the entire shipped model, so a format the parser cannot invert fails here
    rather than surfacing as silent data loss during a fabric read.
    """
    rows = _catalog_rn_formats()
    assert len(rows) > 10_000, "the catalogue should carry thousands of RN formats"

    failures: list[tuple[str, str, str]] = []
    for class_name, rn_format in rows:
        props = _PLACEHOLDER.findall(rn_format)
        # Sentinels with no bracket or separator character, so the round trip
        # measures the parser, not an ambiguous input.
        expected = {prop: f"val{i}x" for i, prop in enumerate(props)}
        filled = rn_format
        for prop, value in expected.items():
            filled = filled.replace(f"{{{prop}}}", value, 1)
        try:
            got = naming_values(filled, rn_format)
        except ValueError as exc:
            failures.append((class_name, rn_format, f"raised {exc}"))
            continue
        if got != expected:
            failures.append((class_name, rn_format, f"got {got}, expected {expected}"))

    assert not failures, f"{len(failures)} classes did not round-trip, e.g. {failures[:5]}"
