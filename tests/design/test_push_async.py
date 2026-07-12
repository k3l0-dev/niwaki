"""Async push — same contract as sync through AsyncNiwaki (awaitable)."""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import PlanResult, PushReport, tenant
from niwaki.exceptions import UnresolvedReferenceError
from niwaki.facade import AsyncNiwaki
from tests.conftest import HOST, LOGIN_URL, login_payload, ok
from tests.design.conftest import mini_design

UNI_URL = f"{HOST}/api/mo/uni.json"


class TestAsyncStrict:
    async def test_single_atomic_post(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", url=UNI_URL, json=ok())
        design = mini_design()

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            report = await design.push(aci, mode="strict")

        assert isinstance(report, PushReport)
        assert report.request_count == 1
        request = httpx_mock.get_requests(method="POST", url=UNI_URL)[0]
        assert json.loads(request.content) == design.to_payload()

    async def test_validation_failure_before_io(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        cfg = tenant("prod")
        cfg.bd("web").bind(vrf="missing")

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(UnresolvedReferenceError):
                await cfg.push(aci, mode="strict")


class TestAsyncStaged:
    async def test_one_post_per_object(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        for dn in (
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-prod",
        ):
            httpx_mock.add_response(method="POST", url=f"{HOST}/api/mo/{dn}.json", json=ok())

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            report = await mini_design().push(aci, mode="staged")

        assert report.mode == "staged"
        assert report.request_count == 4
        assert len(report.dns) == 4


class TestAsyncPlan:
    async def test_plan_reads_and_diffs(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(
            method="GET",
            url=httpx.URL(
                f"{HOST}/api/mo/uni/tn-prod.json",
                params={"rsp-subtree": "full", "rsp-subtree-class": "fvBD,fvCtx,fvRsCtx,fvTenant"},
            ),
            json={"totalCount": "0", "imdata": []},
        )

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            plan = await mini_design().push(aci, mode="plan")

        assert isinstance(plan, PlanResult)
        assert plan.has_changes
        assert "uni/tn-prod" in plan.creates
