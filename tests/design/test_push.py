"""Sync push — strict (atomic polUni POST), staged (waves), to_payload."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import PushReport, tenant
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve
from niwaki.exceptions import StagedPushError, UnresolvedReferenceError
from niwaki.facade import Niwaki
from tests.conftest import HOST, ok
from tests.design.conftest import mini_design

UNI_URL = f"{HOST}/api/mo/uni.json"


class TestToPayload:
    def test_no_session_required(self) -> None:
        payload = mini_design().to_payload()
        assert "polUni" in payload
        (tenant_env,) = payload["polUni"]["children"]
        assert tenant_env["fvTenant"]["attributes"]["name"] == "prod"

    def test_payload_is_repeatable(self) -> None:
        design = mini_design()
        assert design.to_payload() == design.to_payload()

    def test_unresolved_reference_fails_before_any_output(self) -> None:
        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="missing")
        with pytest.raises(UnresolvedReferenceError):
            cfg.to_payload()


class TestStrictPush:
    def test_single_atomic_post_to_uni(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        design = mini_design()
        httpx_mock.add_response(method="POST", url=UNI_URL, json=ok())

        report = design.push(aci, mode="strict")

        request = httpx_mock.get_requests(method="POST", url=UNI_URL)[0]
        assert json.loads(request.content) == design.to_payload()
        assert isinstance(report, PushReport)
        assert report.mode == "strict"
        assert report.request_count == 1
        assert report.dns == [
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-prod",
        ]

    def test_default_mode_is_strict(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=UNI_URL, json=ok())
        report = mini_design().push(aci)
        assert report.mode == "strict"

    def test_validation_failure_sends_nothing(self, aci: Niwaki) -> None:
        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="missing")
        with pytest.raises(UnresolvedReferenceError):
            cfg.push(aci, mode="strict")
        # No POST to uni was registered — httpx_mock would fail on any request.

    def test_push_from_any_cursor_pushes_whole_design(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        """push() on a leaf cursor still pushes the full tree."""
        cfg = tenant("prod")
        leaf = cfg.bd("web").subnet("10.0.1.1/24")
        httpx_mock.add_response(method="POST", url=UNI_URL, json=ok())

        leaf.push(aci, mode="strict")

        request = httpx_mock.get_requests(method="POST", url=UNI_URL)[0]
        body = json.loads(request.content)
        assert body["polUni"]["children"][0]["fvTenant"]["attributes"]["name"] == "prod"


class TestStagedPush:
    def test_one_post_per_object_parents_first(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        design = mini_design()
        expected_dns = [
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-prod",
        ]
        for dn in expected_dns:
            httpx_mock.add_response(method="POST", url=f"{HOST}/api/mo/{dn}.json", json=ok())

        report = design.push(aci, mode="staged")

        assert report.mode == "staged"
        assert report.request_count == 4
        assert sorted(report.dns) == sorted(expected_dns)
        # Wave ordering: the tenant POST must land before any depth-2 object.
        posted = [
            str(r.url)
            for r in httpx_mock.get_requests(method="POST")
            if "aaaLogin" not in str(r.url)
        ]
        assert posted[0].endswith("/api/mo/uni/tn-prod.json")

    def test_failing_op_raises_staged_push_error(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        design = mini_design()
        httpx_mock.add_response(
            method="POST", url=f"{HOST}/api/mo/uni/tn-prod.json", status_code=403, json={}
        )

        with pytest.raises(StagedPushError) as excinfo:
            design.push(aci, mode="staged")
        exc = excinfo.value
        assert [dn for dn, _ in exc.failures] == ["uni/tn-prod"]
        assert len(exc.not_run) == 3  # deeper waves never attempted
        assert exc.report.dns == []  # nothing was written
        # The public surface carries plain DNs — no engine internals.
        assert all(isinstance(dn, str) for dn in exc.not_run)

    def test_ops_payloads_have_no_children(self) -> None:
        """Staged payloads are flat — nesting is the strict mode's job."""
        design = mini_design()
        root = design.design_node.root()
        ops = compile_ops(root, resolve(root))
        for op in ops:
            assert op.payload is not None
            (body,) = op.payload.values()
            assert "children" not in body

    def test_bracketed_rn_depth_still_parents_first(self) -> None:
        """subnet-[10.0.1.1/24] inflates DN depth but never above its parent."""
        cfg = tenant("prod")
        cfg.bd("web").subnet("10.0.1.1/24")
        root = cfg.design_node.root()
        ops = compile_ops(root, resolve(root))
        depth = {op.dn: op.depth for op in ops}
        assert depth["uni/tn-prod/BD-web"] > depth["uni/tn-prod"]
        assert depth["uni/tn-prod/BD-web/subnet-[10.0.1.1/24]"] > depth["uni/tn-prod/BD-web"]

    def test_carrier_emits_no_op_but_its_children_do(self) -> None:
        """A curated carrier (a VMM provider) posts nothing on its own; the
        declared domain under it posts at its full DN and materialises the path."""
        from niwaki.design import design

        cfg = design()
        cfg.vmm_provider("VMware").vmm_dom("prod")
        root = cfg.design_node.root()
        dns = {op.dn for op in compile_ops(root, resolve(root))}
        assert "uni/vmmp-VMware" not in dns  # the carrier itself
        assert "uni/vmmp-VMware/dom-prod" in dns  # its child, at the full DN
