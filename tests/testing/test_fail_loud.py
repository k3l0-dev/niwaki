"""Fail loud, log everything, fail on demand.

Contract pinned here (decisions recorded for the implementer):

- **Fail loud.**  A request the fake cannot answer faithfully — an unknown
  query parameter, a bad value for a known one, a filter it cannot parse or
  whose operator is not in the APIC grammar, an unsupported path or method,
  a subtree facet it cannot honestly evaluate — is answered with an APIC
  error envelope, HTTP **400**, a non-empty ``code``, and a ``text`` that
  starts with ``"FakeApic:"`` and names the offending piece.  Through the
  SDK this is the typed :class:`~niwaki.exceptions.APIError` whose
  ``apic_message`` starts with ``"FakeApic:"``.  Never a silent full-store
  answer.
- **Request log.**  ``fake.requests`` lists every request received, in
  order — login, refresh, rejected and injected-failure requests included.
  The fake's token lifetime is a realistic one (well above the SDK's default
  60 s refresh threshold), so a default client never refreshes mid-test.
- **Fault injection.**  ``fake.fail(dn, *, code=..., text=..., status=400,
  method=None, times=None)``: the next ``times`` requests (every one when
  ``None``) that target *dn* answer an APIC error envelope with that
  ``status``/``code``/``text``.  A request targets *dn* when it is a GET or
  DELETE of ``/api/mo/<dn>.json``, or a POST that would write *dn* (posted at
  *dn*, or *dn* materialised anywhere inside the posted envelope).
  ``method`` narrows to one HTTP verb.  A failed write changes nothing
  (an APIC POST is atomic).  ``clear_failures()`` drops every pending
  failure.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, tenant
from niwaki.exceptions import APIError, ServerError, StagedPushError
from niwaki.models.fv.fvBD import fvBD
from niwaki.query import SubtreeInclude, raw
from niwaki.testing import FakeApic
from niwaki.transport import ApicSession, RetryConfig

from .conftest import FABRIC, attrs_of, error_text, imdata, request


def _design() -> Cursor:
    config = tenant("prod")
    config.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")
    config.vrf("main")
    return config


def _assert_fake_error(caught: pytest.ExceptionInfo[APIError], *fragments: str) -> None:
    error = caught.value
    assert error.status_code == 400
    assert error.apic_code is not None
    assert error.apic_message.startswith("FakeApic:"), error.apic_message
    for fragment in fragments:
        assert fragment in error.apic_message


class TestFailLoud:
    def test_an_unknown_query_parameter(self, seeded: FakeApic, session: ApicSession) -> None:
        with pytest.raises(APIError) as caught:
            session.get("/api/class/fvBD.json", **{"rsp-frobnicate": "yes"})
        _assert_fake_error(caught, "rsp-frobnicate")

    def test_an_unknown_value_of_a_known_parameter(
        self, seeded: FakeApic, session: ApicSession
    ) -> None:
        with pytest.raises(APIError) as caught:
            session.get("/api/mo/uni/tn-prod.json", **{"query-target": "everything"})
        _assert_fake_error(caught, "query-target")
        with pytest.raises(APIError) as caught:
            session.get("/api/mo/uni/tn-prod.json", **{"rsp-subtree": "deep"})
        _assert_fake_error(caught, "rsp-subtree")

    def test_an_operator_outside_the_grammar(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).where(raw('frobnicate(fvBD.name,"web")')).fetch()
        _assert_fake_error(caught, "frobnicate")

    def test_a_malformed_filter(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).where(raw('eq(fvBD.name,"web"')).fetch()
        _assert_fake_error(caught)

    def test_a_malformed_subtree_filter(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).include("fvSubnet").subtree_where(raw("eq(fvSubnet.ip")).fetch()
        _assert_fake_error(caught)

    def test_a_facet_the_fake_cannot_answer(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.query(fvBD).include_subtree(SubtreeInclude.AUDIT_LOGS).fetch()
        _assert_fake_error(caught, "audit-logs")

    def test_an_unsupported_path(self, seeded: FakeApic, session: ApicSession) -> None:
        with pytest.raises(APIError) as caught:
            session.get("/api/node/mo/uni/tn-prod.xml")
        _assert_fake_error(caught, "/api/node/mo/uni/tn-prod.xml")

    def test_an_unsupported_method_on_the_wire(self, seeded: FakeApic) -> None:
        response = seeded.handle(request("PUT", "/api/mo/uni/tn-prod.json", FABRIC))
        assert response.status_code == 400
        assert error_text(response).startswith("FakeApic:")
        assert "PUT" in error_text(response)

    def test_a_rejected_request_changes_nothing(self, fake: FakeApic) -> None:
        fake.handle(request("PUT", "/api/mo/uni.json", FABRIC))
        assert [o.dn for o in fake.objects()] == ["uni"]

    def test_the_wire_error_envelope_is_the_apic_one(self, seeded: FakeApic) -> None:
        response = seeded.handle(request("GET", "/api/class/fvBD.json?bogus=1"))
        (item,) = imdata(response)
        attributes = attrs_of(item)
        assert attributes["code"]
        assert str(attributes["text"]).startswith("FakeApic:")


class TestRequestLog:
    def test_everything_is_logged_in_order(self, seeded: FakeApic) -> None:
        with seeded.client() as aci:
            aci.tenant("prod").bd("web").read()
            with pytest.raises(APIError):
                aci.query(fvBD).where(raw("nonsense(")).fetch()
        assert [(r.method, r.url.path) for r in seeded.requests] == [
            ("POST", "/api/aaaLogin.json"),
            ("GET", "/api/mo/uni/tn-prod/BD-web.json"),
            ("GET", "/api/class/fvBD.json"),
        ]

    def test_a_push_shows_its_payload(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        (post,) = [
            r for r in fake.requests if r.method == "POST" and r.url.path != "/api/aaaLogin.json"
        ]
        assert post.url.path == "/api/mo/uni.json"
        assert b'"fvTenant"' in post.content


class TestInjectedFailures:
    def test_a_strict_push_raises_the_typed_error_and_writes_nothing(
        self, fake: FakeApic, aci: Niwaki
    ) -> None:
        fake.fail("uni/tn-prod/BD-web", code="103", text="boom")
        with pytest.raises(APIError) as caught:
            _design().push(aci)
        assert caught.value.status_code == 400
        assert caught.value.apic_code == "103"
        assert caught.value.apic_message == "boom"
        assert [o.dn for o in fake.objects()] == ["uni"]

    def test_a_staged_push_names_the_failed_dn(self, fake: FakeApic, aci: Niwaki) -> None:
        fake.fail("uni/tn-prod/BD-web", code="103", text="boom")
        with pytest.raises(StagedPushError) as caught:
            _design().push(aci, mode="staged")
        failures = dict(caught.value.failures)
        assert list(failures) == ["uni/tn-prod/BD-web"]
        cause = failures["uni/tn-prod/BD-web"]
        assert isinstance(cause, APIError)
        assert cause.apic_code == "103"
        assert fake.get("uni/tn-prod/ctx-main") is not None
        assert fake.get("uni/tn-prod/BD-web") is None

    def test_a_read_failure_carries_the_apic_code(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="102", text="parent not found", method="GET")
        with pytest.raises(APIError) as caught:
            aci.tenant("prod").bd("web").read()
        assert caught.value.apic_code == "102"
        assert caught.value.apic_message == "parent not found"

    def test_times_is_honoured(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="102", method="GET", times=2)
        node = aci.tenant("prod").bd("web")
        for _ in range(2):
            with pytest.raises(APIError):
                node.read()
        assert node.read().name == "web"

    def test_no_times_fails_until_cleared(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="102", method="GET")
        node = aci.tenant("prod").bd("web")
        for _ in range(3):
            with pytest.raises(APIError):
                node.read()
        seeded.clear_failures()
        assert node.read().name == "web"

    def test_method_narrows_the_failure(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="107", method="DELETE")
        assert aci.tenant("prod").bd("web").read().name == "web"
        with pytest.raises(APIError) as caught:
            aci.tenant("prod").bd("web").delete()
        assert caught.value.apic_code == "107"
        assert seeded.get("uni/tn-prod/BD-web") is not None

    def test_other_dns_are_untouched(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="102")
        assert aci.tenant("prod").bd("db").read().name == "db"

    def test_a_read_targets_its_url_dn_only(self, seeded: FakeApic, aci: Niwaki) -> None:
        """A class query or an ancestor's subtree read is not a read *of* the DN."""
        seeded.fail("uni/tn-prod/BD-web", code="102", method="GET")
        assert len(aci.query(fvBD).fetch()) == 3
        assert len(aci.query(fvBD).under("uni/tn-prod").fetch()) == 2

    def test_a_write_targets_the_dn_itself_not_its_descendants(self, seeded: FakeApic) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="103", method="POST")
        subnet = "uni/tn-prod/BD-web/subnet-[10.0.9.1/24]"
        body = {"fvSubnet": {"attributes": {"ip": "10.0.9.1/24"}}}
        response = seeded.handle(request("POST", f"/api/mo/{subnet}.json", body))
        assert response.status_code == 200
        assert seeded.get(subnet) is not None

    def test_the_status_is_honoured(self, seeded: FakeApic) -> None:
        seeded.fail("uni/tn-prod/BD-web", code="500", text="down", status=500, method="GET")
        with (
            seeded.client(retry=RetryConfig(attempts=1)) as aci,
            pytest.raises(ServerError) as caught,
        ):
            aci.tenant("prod").bd("web").read()
        assert caught.value.status_code == 500
        assert caught.value.apic_code == "500"

    def test_defaults_answer_an_apic_error(self, seeded: FakeApic, aci: Niwaki) -> None:
        seeded.fail("uni/tn-prod/BD-web")
        with pytest.raises(APIError) as caught:
            aci.tenant("prod").bd("web").read()
        assert caught.value.status_code == 400
        assert caught.value.apic_code is not None
