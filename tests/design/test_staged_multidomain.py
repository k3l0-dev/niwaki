"""Staged compilation — brackets, atomic classes, plan.

Wave ordering must survive DNs whose naming values contain slashes, atomic
classes must ship their subtree in one op, and plan mode must read once per
declared domain.  No live APIC — pure compilation plus mocked plan reads.
"""

from __future__ import annotations

from typing import Any

import httpx
from pytest_httpx import HTTPXMock

from niwaki.design import PlanResult, design, fabric, tenant
from niwaki.design._compiler import compile_ops
from niwaki.design._engine import _toposort
from niwaki.design._resolver import resolve
from niwaki.facade import Niwaki
from tests.conftest import HOST

_EMPTY = {"totalCount": "0", "imdata": []}


class TestBracketAwareDepth:
    def test_waves_group_bracketed_siblings_correctly(self) -> None:
        """A bracketed subnet runs in the same wave as its plain sibling."""
        cfg = tenant("p")
        bd = cfg.bd("w")
        bd.subnet("10.0.1.1/24")
        epg = cfg.app("a").epg("e")
        epg.static_path("topology/pod-1/paths-101/pathep-[eth1/1]", encap="vlan-100")
        root = cfg.design_node.root()
        waves = _toposort(compile_ops(root, resolve(root)))
        by_wave = {op.dn: i for i, wave in enumerate(waves) for op in wave}
        # depth 3: the subnet and the EPG (uni/tn-p/ap-a/epg-e) — same wave.
        assert by_wave["uni/tn-p/BD-w/subnet-[10.0.1.1/24]"] == by_wave["uni/tn-p/ap-a/epg-e"]
        # the static path sits one wave below the EPG despite its inner slashes.
        path_dn = "uni/tn-p/ap-a/epg-e/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
        assert by_wave[path_dn] == by_wave["uni/tn-p/ap-a/epg-e"] + 1


class TestAtomicGroups:
    def test_vpc_pair_ships_as_one_nested_op(self) -> None:
        """fabricExplicitGEp + its fabricNodePEp children = exactly one op."""
        cfg = fabric()
        pair = cfg.vpc_protection().vpc_pair("vpc-101-102", logical_pair_id=101)
        pair.node(101).node(102)
        root = cfg.design_node.root()
        ops = compile_ops(root, resolve(root))

        dns = [op.dn for op in ops]
        pair_dn = "uni/fabric/protpol/expgep-vpc-101-102"
        assert pair_dn in dns
        assert not any(dn.startswith(f"{pair_dn}/") for dn in dns), (
            "atomic children must not emit separate ops"
        )
        (pair_op,) = [op for op in ops if op.dn == pair_dn]
        assert pair_op.payload is not None
        nested = pair_op.payload["fabricExplicitGEp"]["children"]
        assert [next(iter(child)) for child in nested] == ["fabricNodePEp", "fabricNodePEp"]

    def test_non_atomic_children_still_emit_ops(self) -> None:
        cfg = fabric()
        cfg.datetime_policy("t").ntp_provider("10.0.0.1")
        root = cfg.design_node.root()
        dns = [op.dn for op in compile_ops(root, resolve(root))]
        assert "uni/fabric/time-t" in dns
        assert "uni/fabric/time-t/ntpprov-10.0.0.1" in dns


class TestPlanMultiDomain:
    def test_one_read_per_declared_domain(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            url=httpx.URL(
                f"{HOST}/api/mo/uni/fabric.json",
                params={"rsp-subtree": "full", "rsp-subtree-class": "datetimePol,fabricInst"},
            ),
            json=_EMPTY,
        )
        httpx_mock.add_response(
            method="GET",
            url=httpx.URL(
                f"{HOST}/api/mo/uni/tn-prod.json",
                params={"rsp-subtree": "full", "rsp-subtree-class": "fvCtx,fvTenant"},
            ),
            json=_EMPTY,
        )

        cfg = design()
        cfg.fabric().datetime_policy("t")
        cfg.tenant("prod").vrf("main")
        plan = cfg.push(aci, mode="plan")

        assert isinstance(plan, PlanResult)
        assert plan.creates == [
            "uni/fabric",
            "uni/fabric/time-t",
            "uni/tn-prod",
            "uni/tn-prod/ctx-main",
        ]
        reads = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert len(reads) == 2

    def test_partial_existing_domain(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        """An existing carrier counts as unchanged; only the leaf is created."""
        existing: dict[str, Any] = {
            "totalCount": "1",
            "imdata": [{"fabricInst": {"attributes": {}}}],
        }
        httpx_mock.add_response(
            method="GET",
            url=httpx.URL(
                f"{HOST}/api/mo/uni/fabric.json",
                params={"rsp-subtree": "full", "rsp-subtree-class": "datetimePol,fabricInst"},
            ),
            json=existing,
        )
        cfg = fabric()
        cfg.datetime_policy("t")
        plan = cfg.push(aci, mode="plan")
        assert isinstance(plan, PlanResult)
        assert plan.creates == ["uni/fabric/time-t"]
        assert plan.unchanged == ["uni/fabric"]
