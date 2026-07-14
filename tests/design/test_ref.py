"""``ref()`` — references that configure the relationship itself.

Most relationships are pure edges, but 26 curated binds resolve to an Rs class
that carries configuration: the encap and immediacy of an EPG-to-domain
attachment, the ``directives`` of a filter under a subject (contract logging),
the ``direction`` of a route-control profile, the management address of a
node.  ``ref()`` reaches them without leaving the closed world.

Nominal, edge and error paths — no I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from niwaki.design import Cursor, ref, tenant
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve
from niwaki.exceptions import DesignError


def _flatten(cursor: Cursor) -> dict[str, dict[str, str]]:
    root = cursor.design_node.root()
    flat = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        flat[op.dn] = {k: v for k, v in body["attributes"].items() if k != "dn"}
    return flat


def _base() -> Cursor:
    cfg = tenant("prod")
    cfg.vrf("main")
    cfg.bd("web").bind(vrf="main")
    cfg.filter("http").entry("tcp-8080", tcp=8080)
    cfg.phys_dom("phys")
    return cfg


class TestNominal:
    def test_name_flavor_relation_takes_attributes(self) -> None:
        """Contract logging — the directive lives on the filter attachment."""
        cfg = _base()
        cfg.contract("web").subject("http").bind(filter=ref("http", directives="log"))

        assert _flatten(cfg)["uni/tn-prod/brc-web/subj-http/rssubjFiltAtt-http"] == {
            "tnVzFilterName": "http",
            "directives": "log",
        }

    def test_dn_flavor_relation_takes_attributes(self) -> None:
        """Domain attachment — the static encap and the resolution immediacy."""
        cfg = _base()
        cfg.app("shop").epg("web").bind(
            bd="web",
            domain=ref("phys", resolution_immediacy="immediate", deployment_immediacy="lazy"),
        )

        assert _flatten(cfg)["uni/tn-prod/ap-shop/epg-web/rsdomAtt-[uni/phys-phys]"] == {
            "tDn": "uni/phys-phys",
            "resImedcy": "immediate",
            "instrImedcy": "lazy",
        }

    def test_bind_dn_takes_attributes(self) -> None:
        """The raw-DN escape hatch carries them too."""
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd="web").bind_dn(
            domain=ref("uni/phys-outside", resolution_immediacy="pre-provision")
        )

        assert _flatten(cfg)["uni/tn-prod/ap-shop/epg-web/rsdomAtt-[uni/phys-outside]"] == {
            "tDn": "uni/phys-outside",
            "resImedcy": "pre-provision",
        }

    def test_a_verb_takes_attributes(self) -> None:
        """``provide``/``consume`` are references like any other."""
        cfg = _base()
        cfg.contract("web").subject("http").bind(filter="http")
        cfg.app("shop").epg("web").bind(bd="web").provide(ref("web", priority="level1"))

        assert _flatten(cfg)["uni/tn-prod/ap-shop/epg-web/rsprov-web"] == {
            "tnVzBrCPName": "web",
            "prio": "level1",
        }

    def test_plain_string_stays_a_pure_edge(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd="web")
        assert _flatten(cfg)["uni/tn-prod/ap-shop/epg-web/rsbd"] == {"tnFvBDName": "web"}


class TestEdges:
    def test_ref_without_attributes_equals_a_plain_name(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd=ref("web"))
        assert _flatten(cfg)["uni/tn-prod/ap-shop/epg-web/rsbd"] == {"tnFvBDName": "web"}

    def test_ref_is_an_immutable_value(self) -> None:
        reference = ref("web", directives="log")
        assert reference.target == "web"
        assert reference.attrs == {"directives": "log"}
        with pytest.raises(AttributeError):
            reference.target = "other"  # type: ignore[misc]

    def test_several_refs_in_one_bind_call(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(
            bd=ref("web"), domain=ref("phys", resolution_immediacy="immediate")
        )
        flat = _flatten(cfg)
        assert flat["uni/tn-prod/ap-shop/epg-web/rsbd"] == {"tnFvBDName": "web"}
        assert "resImedcy" in flat["uni/tn-prod/ap-shop/epg-web/rsdomAtt-[uni/phys-phys]"]


class TestErrors:
    def test_unknown_attribute_is_rejected_with_a_suggestion(self) -> None:
        cfg = _base()
        cfg.contract("web").subject("http").bind(filter=ref("http", directive="log"))
        with pytest.raises(DesignError, match="did you mean 'directives'"):
            _flatten(cfg)

    def test_wire_name_is_redirected_to_the_python_field(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd="web", domain=ref("phys", resImedcy="immediate"))
        with pytest.raises(DesignError, match="resolution_immediacy"):
            _flatten(cfg)

    def test_invalid_value_fails_the_relation_model(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd="web", domain=ref("phys", resolution_immediacy="nope"))
        with pytest.raises(ValidationError):
            _flatten(cfg)

    def test_an_unresolvable_ref_still_reports_the_closed_world(self) -> None:
        cfg = _base()
        cfg.app("shop").epg("web").bind(bd=ref("absent", directives="log"))
        with pytest.raises(DesignError, match="does not resolve"):
            _flatten(cfg)
