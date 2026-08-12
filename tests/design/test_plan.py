"""Plan mode — dry-run diff against the current APIC state.  Nothing pushed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import PlanResult, tenant
from niwaki.facade import Niwaki
from tests.conftest import HOST
from tests.design.conftest import mini_design


def _plan_url(classes: str, dn: str = "uni/tn-prod") -> httpx.URL:
    """Expected plan read URL — one flat shard scoped to the design's classes (R-3)."""
    return httpx.URL(
        f"{HOST}/api/mo/{dn}.json",
        params={
            "query-target": "subtree",
            "target-subtree-class": classes,
            "page": "0",
            "page-size": "500",
        },
    )


# mini_design: tenant + BD + VRF + the resolved vrf binding.
PLAN_URL = _plan_url("fvBD,fvCtx,fvRsCtx,fvTenant")


def _absent(httpx_mock: HTTPXMock, dn: str) -> None:
    """Answer a boundary-create probe: nothing exists at this DN.

    The plan verifies each absent-subtree root with a bare self-GET (the
    class-scoped read is not gospel — measured live), so a test simulating
    an absent object must answer that probe too.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{HOST}/api/mo/{dn}.json",
        json={"totalCount": "0", "imdata": []},
    )


def _current_tree() -> dict[str, Any]:
    """APIC state: tenant + BD (unicast routing off) + VRF, no rsctx yet.

    Flat, as a ``query-target=subtree`` class read answers — each object on its
    own, carrying its ``dn``; the reader rebuilds the hierarchy client-side.
    """
    return {
        "totalCount": "3",
        "imdata": [
            {"fvTenant": {"attributes": {"name": "prod", "dn": "uni/tn-prod"}}},
            {
                "fvBD": {
                    "attributes": {
                        "name": "web",
                        "unicastRoute": "no",
                        "dn": "uni/tn-prod/BD-web",
                    }
                }
            },
            {"fvCtx": {"attributes": {"name": "prod", "dn": "uni/tn-prod/ctx-prod"}}},
        ],
    }


class TestPlan:
    def test_everything_created_when_tenant_absent(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", url=PLAN_URL, json={"totalCount": "0", "imdata": []})
        _absent(httpx_mock, "uni/tn-prod")  # the one probe: the absent subtree's root

        plan = mini_design().push(aci, mode="plan")

        assert isinstance(plan, PlanResult)
        assert plan.creates == [
            "uni/tn-prod",
            "uni/tn-prod/BD-web",
            "uni/tn-prod/BD-web/rsctx",
            "uni/tn-prod/ctx-prod",
        ]
        assert plan.updates == {}
        assert plan.unchanged == []
        assert plan.has_changes

    def test_mixed_create_update_unchanged(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", url=PLAN_URL, json=_current_tree())
        _absent(httpx_mock, "uni/tn-prod/BD-web/rsctx")

        plan = mini_design().push(aci, mode="plan")

        # BD exists but unicast routing differs: "no" on APIC, True desired.
        assert plan.updates == {"uni/tn-prod/BD-web": {"unicast_routing": (False, True)}}
        # The vrf binding does not exist yet.
        assert plan.creates == ["uni/tn-prod/BD-web/rsctx"]
        # Tenant and VRF match the design.
        assert sorted(plan.unchanged) == ["uni/tn-prod", "uni/tn-prod/ctx-prod"]
        assert plan.has_changes

    def test_untouched_fields_never_reported(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        """A design that sets nothing must not diff against schema defaults."""
        cfg = tenant("prod")
        cfg.bd("web")  # arpFlood etc. never set — APIC values must be ignored
        cfg.vrf("prod")
        current = _current_tree()
        current["imdata"][1]["fvBD"]["attributes"]["arpFlood"] = "yes"
        # No bind in this design — no fvRsCtx in the scoped read.
        httpx_mock.add_response(method="GET", url=_plan_url("fvBD,fvCtx,fvTenant"), json=current)

        plan = cfg.push(aci, mode="plan")

        assert plan.updates == {}
        assert plan.creates == []
        assert not plan.has_changes

    def test_plan_issues_no_writes(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", url=PLAN_URL, json=_current_tree())
        _absent(httpx_mock, "uni/tn-prod/BD-web/rsctx")

        mini_design().push(aci, mode="plan")

        assert httpx_mock.get_requests(method="POST", url=f"{HOST}/api/mo/uni.json") == []

    def test_a_design_too_big_for_one_query_string_shards_its_read(
        self, aci: Niwaki, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The R-3 regression: a class list over the URL budget splits into
        several flat reads, and the diff sees one whole tree regardless.

        The production budget (3 500 bytes ≈ 250 classes) would need an
        impractically wide design, so the budget is narrowed instead — the
        wiring under test is identical.
        """
        monkeypatch.setattr("niwaki._read._CLASS_LIST_BUDGET", 12)
        flat = _current_tree()["imdata"]
        by_class = {next(iter(item)): item for item in flat}
        # Each shard answers only its own classes — the tenant (the tree's
        # root) arrives in the LAST response, and the rebuild must not care.
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvBD,fvCtx"),
            json={"totalCount": "2", "imdata": [by_class["fvBD"], by_class["fvCtx"]]},
        )
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvRsCtx"),
            json={"totalCount": "0", "imdata": []},
        )
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvTenant"),
            json={"totalCount": "1", "imdata": [by_class["fvTenant"]]},
        )

        _absent(httpx_mock, "uni/tn-prod/BD-web/rsctx")

        plan = mini_design().push(aci, mode="plan")

        # Same verdict as the single-request read: BD update, rsctx creation.
        assert plan.updates == {"uni/tn-prod/BD-web": {"unicast_routing": (False, True)}}
        assert plan.creates == ["uni/tn-prod/BD-web/rsctx"]
        # Three shard reads plus the one boundary-create probe.
        assert len(httpx_mock.get_requests(method="GET")) == 4
