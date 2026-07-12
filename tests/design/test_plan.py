"""Plan mode — dry-run diff against the current APIC state.  Nothing pushed."""

from __future__ import annotations

from typing import Any

import httpx
from pytest_httpx import HTTPXMock

from niwaki.design import PlanResult, tenant
from niwaki.facade import Niwaki
from tests.conftest import HOST
from tests.design.conftest import mini_design


def _plan_url(classes: str, dn: str = "uni/tn-prod") -> httpx.URL:
    """Expected plan read URL — scoped to the design's classes (R-3)."""
    return httpx.URL(
        f"{HOST}/api/mo/{dn}.json",
        params={"rsp-subtree": "full", "rsp-subtree-class": classes},
    )


# mini_design: tenant + BD + VRF + the resolved vrf binding.
PLAN_URL = _plan_url("fvBD,fvCtx,fvRsCtx,fvTenant")


def _current_tree() -> dict[str, Any]:
    """APIC state: tenant + BD (unicast routing off) + VRF, no rsctx yet."""
    return {
        "totalCount": "1",
        "imdata": [
            {
                "fvTenant": {
                    "attributes": {"name": "prod"},
                    "children": [
                        {"fvBD": {"attributes": {"name": "web", "unicastRoute": "no"}}},
                        {"fvCtx": {"attributes": {"name": "prod"}}},
                    ],
                }
            }
        ],
    }


class TestPlan:
    def test_everything_created_when_tenant_absent(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", url=PLAN_URL, json={"totalCount": "0", "imdata": []})

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
        current["imdata"][0]["fvTenant"]["children"][0]["fvBD"]["attributes"]["arpFlood"] = "yes"
        # No bind in this design — no fvRsCtx in the scoped read.
        httpx_mock.add_response(method="GET", url=_plan_url("fvBD,fvCtx,fvTenant"), json=current)

        plan = cfg.push(aci, mode="plan")

        assert plan.updates == {}
        assert plan.creates == []
        assert not plan.has_changes

    def test_plan_issues_no_writes(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", url=PLAN_URL, json=_current_tree())

        mini_design().push(aci, mode="plan")

        assert httpx_mock.get_requests(method="POST", url=f"{HOST}/api/mo/uni.json") == []
