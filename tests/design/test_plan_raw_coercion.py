"""Plan mode over the wire-name channel — synonym spellings are not drift.

End-to-end twin of ``tests/utils/test_diff_raw_coercion.py``: a design that
declares a catalogue-served object through ``raw()`` (or a typed field
through ``raw_set()``) in one wire spelling, against an APIC that reads it
back in another spelling of the same value, must plan as **unchanged** —
otherwise ``mode="plan"`` reports a change that no push can ever converge.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import design, tenant
from niwaki.facade import Niwaki
from niwaki.types import JsonObject
from tests.conftest import HOST


def _plan_url(classes: str, dn: str = "uni/tn-prod") -> httpx.URL:
    """The plan's flat, class-scoped subtree read of the declared tenant."""
    return httpx.URL(
        f"{HOST}/api/mo/{dn}.json",
        params={
            "query-target": "subtree",
            "target-subtree-class": classes,
            "page": "0",
            "page-size": "500",
        },
    )


def _read(*objects: JsonObject) -> JsonObject:
    return {"totalCount": str(len(objects)), "imdata": list(objects)}


def _self_get(httpx_mock: HTTPXMock, *objects: JsonObject) -> None:
    """Answer the plan's bare self-GET of each catalogue-served object.

    Today the plan pairs read children by RN, and a catalogue-served read
    carries none of its own, so every such object is first booked as a
    create and then verified by a bare self-GET (the boundary probe) — the
    diff under test runs there.  Registered as optional: a plan that pairs
    them straight from the class-scoped read never asks.
    """
    for mo in objects:
        body = next(iter(mo.values()))
        assert isinstance(body, dict)
        attributes = body["attributes"]
        assert isinstance(attributes, dict)
        httpx_mock.add_response(
            method="GET", url=f"{HOST}/api/mo/{attributes['dn']}.json", json=_read(mo)
        )


TENANT: JsonObject = {"fvTenant": {"attributes": {"name": "prod", "dn": "uni/tn-prod"}}}

SVC_BD: JsonObject = {
    "fvSvcBD": {
        "attributes": {
            "name": "svc",
            "arpFlood": "yes",
            "unicastRoute": "no",
            "dn": "uni/tn-prod/svcBD-svc",
        }
    }
}
FLT_P: JsonObject = {"vzRFltP": {"attributes": {"id": "7", "dn": "uni/tn-prod/fp-7"}}}
FLT_E: JsonObject = {
    "vzRFltE": {
        "attributes": {
            "name": "e1",
            "tcpRules": "ack,syn",
            "stateful": "yes",
            "dn": "uni/tn-prod/fp-7/fe-e1",
        }
    }
}


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
class TestPlanCatalogueServedSynonyms:
    def test_bool_and_bitmask_synonyms_plan_unchanged(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        cfg = tenant("prod")
        cfg.raw("fvSvcBD", name="svc", arpFlood="true", unicastRoute="false")
        cfg.raw("vzRFltP", id="7").raw("vzRFltE", name="e1", tcpRules="syn,ack", stateful="true")
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvSvcBD,fvTenant,vzRFltE,vzRFltP"),
            json=_read(TENANT, SVC_BD, FLT_P, FLT_E),
        )
        _self_get(httpx_mock, SVC_BD, FLT_P, FLT_E)

        plan = cfg.push(aci, mode="plan")

        assert plan.updates == {}
        assert plan.creates == []
        assert sorted(plan.unchanged) == [
            "uni/tn-prod",
            "uni/tn-prod/fp-7",
            "uni/tn-prod/fp-7/fe-e1",
            "uni/tn-prod/svcBD-svc",
        ]
        assert not plan.has_changes

    def test_a_genuine_change_is_still_planned_in_wire_spelling(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        cfg = tenant("prod")
        cfg.raw("fvSvcBD", name="svc", arpFlood="true", unicastRoute="false")
        drifted: JsonObject = {
            "fvSvcBD": {
                "attributes": {
                    "name": "svc",
                    "arpFlood": "yes",
                    "unicastRoute": "yes",
                    "dn": "uni/tn-prod/svcBD-svc",
                }
            }
        }
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvSvcBD,fvTenant"),
            json=_read(TENANT, drifted),
        )
        _self_get(httpx_mock, drifted)

        plan = cfg.push(aci, mode="plan")

        # Only the genuine change: reported under the wire name, both sides
        # in wire spelling; the synonym arpFlood is not drift.
        assert plan.updates == {"uni/tn-prod/svcBD-svc": {"unicastRoute": ("yes", "false")}}


class TestPlanTypedFieldSpelledLikeItsWireName:
    def test_identical_raw_set_value_plans_unchanged(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        cfg = tenant("prod")
        cfg.bd("web").raw_set(type="regular")
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("fvBD,fvTenant"),
            json=_read(
                TENANT,
                {
                    "fvBD": {
                        "attributes": {"name": "web", "type": "regular", "dn": "uni/tn-prod/BD-web"}
                    }
                },
            ),
        )

        plan = cfg.push(aci, mode="plan")

        assert plan.updates == {}
        assert sorted(plan.unchanged) == ["uni/tn-prod", "uni/tn-prod/BD-web"]


RADIUS_PROVIDER_DN = "uni/userext/radiusext/radiusprovider-r1"


class TestPlanSecretAbsentFromTheRead:
    def test_empty_secret_raw_set_is_planned(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        # The APIC never echoes a secret: the read carries no "key".  Push
        # would POST key="" (clearing it), so plan must say so.
        cfg = design()
        cfg.aaa().raw("aaaRadiusEp").raw("aaaRadiusProvider", name="r1").raw_set(key="")
        httpx_mock.add_response(
            method="GET",
            url=_plan_url("aaaRadiusEp,aaaRadiusProvider,aaaUserEp", dn="uni/userext"),
            json=_read(
                {"aaaUserEp": {"attributes": {"dn": "uni/userext"}}},
                {"aaaRadiusEp": {"attributes": {"dn": "uni/userext/radiusext"}}},
                {
                    "aaaRadiusProvider": {
                        "attributes": {"name": "r1", "timeout": "5", "dn": RADIUS_PROVIDER_DN}
                    }
                },
            ),
        )

        plan = cfg.push(aci, mode="plan")

        # Only the desired side is pinned: how the current side of an
        # unechoed secret is displayed is the plan report's business.
        assert list(plan.updates) == [RADIUS_PROVIDER_DN]
        assert list(plan.updates[RADIUS_PROVIDER_DN]) == ["key"]
        _old, new = plan.updates[RADIUS_PROVIDER_DN]["key"]
        assert new == ""
        assert plan.has_changes
