"""The live verifier's pure half — DN arithmetic and expectation building.

The auditing itself needs an APIC, but everything that decides *what* to check
is offline and deterministic: it must be right, or a green run means nothing.
"""

from __future__ import annotations

import pytest
from scripts.verify_live import _acts, _expectations, _parent_dn

from niwaki.design import tenant


class TestParentDn:
    """A DN's parent — the slashes inside bracketed naming values are not separators."""

    @pytest.mark.parametrize(
        ("dn", "parent"),
        [
            ("uni/tn-prod", "uni"),
            ("uni/tn-prod/BD-web", "uni/tn-prod"),
            # A subnet's naming value carries a slash of its own.
            ("uni/tn-prod/BD-web/subnet-[10.0.1.1/24]", "uni/tn-prod/BD-web"),
            # And a path attachment nests brackets inside brackets.
            (
                "uni/tn-p/ap-a/epg-e/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]",
                "uni/tn-p/ap-a/epg-e",
            ),
            # A fault hangs under the object it complains about.
            ("uni/tn-mgmt/mgmtp-default/oob-x/fault-F0523", "uni/tn-mgmt/mgmtp-default/oob-x"),
            ("uni", ""),
        ],
    )
    def test_strips_the_last_rn(self, dn: str, parent: str) -> None:
        assert _parent_dn(dn) == parent


class TestExpectations:
    def test_every_declared_object_becomes_an_expectation(self) -> None:
        cfg = tenant("prod")
        cfg.vrf("main")
        cfg.bd("web").bind(vrf="main").subnet("10.0.1.1/24")

        expected = {item.dn: item for item in _expectations(cfg)}
        assert set(expected) == {
            "uni/tn-prod",
            "uni/tn-prod/ctx-main",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/rsctx",  # the relation is an object too
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
        }

    def test_the_payload_is_what_the_push_would_send(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web", unicast_routing=True)

        bd = next(i for i in _expectations(cfg) if i.dn == "uni/tn-prod/BD-web")
        assert bd.aci_class == "fvBD"
        assert bd.payload == {"name": "web", "unicastRoute": "true"}

    def test_children_are_attributed_to_their_parent(self) -> None:
        cfg = tenant("prod")
        cfg.vrf("main")
        cfg.bd("web").bind(vrf="main").subnet("10.0.1.1/24")

        expected = {item.dn: item for item in _expectations(cfg)}
        assert expected["uni/tn-prod/BD-web"].children == (
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]",
        )
        assert expected["uni/tn-prod/BD-web/subnet-[10.0.1.1/24]"].children == ()

    def test_a_bracketed_child_is_not_mistaken_for_a_grandchild(self) -> None:
        """The subnet hangs under the BD, not under a phantom '10.0.1.1' node."""
        cfg = tenant("prod").bd("web")
        cfg.subnet("10.0.1.1/24")

        expected = {item.dn: item for item in _expectations(cfg)}
        assert expected["uni/tn-prod"].children == ("uni/tn-prod/BD-web",)


class TestActs:
    def test_every_act_design_compiles(self) -> None:
        """The verifier audits the acts themselves — they must all be reachable."""
        acts = _acts()
        assert set(acts) == {"1", "2", "3", "4", "5", "6"}
        for designs in acts.values():
            for _name, factory in designs:
                assert _expectations(factory()), "a design with nothing to verify"
