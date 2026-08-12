"""External-reference verification (``push(verify_refs=True)``) — M6.

Pure core: enumeration of every external-DN surface, catalogue accept-sets,
evaluation statuses. Wiring: reads-before-writes ordering, collect-all
failure semantics, zero writes on failure, plan-never-raises, and the
byte-identical no-flag path. Async mirrors the sync driver.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from niwaki import Niwaki, exceptions
from niwaki.design import design, tenant
from niwaki.design._push import _walk_dns
from niwaki.design._resolver import resolve
from niwaki.design._verify import (
    ExternalRef,
    _accept_set,
    collect_external_refs,
    evaluate,
)
from tests.conftest import HOST, ok

POOL_DN = "uni/infra/vlanns-[shared]-static"
PATH_DN = "topology/pod-1/paths-101/pathep-[eth1/1]"


def _collect(cursor):  # type: ignore[no-untyped-def]
    root = cursor.design_node.root()
    extras = resolve(root)
    return collect_external_refs(root, extras, set(_walk_dns(root, extras)))


def _envelope(cls: str, dn: str) -> dict[str, object]:
    return {"totalCount": "1", "imdata": [{cls: {"attributes": {"dn": dn}}}]}


# ── Enumeration (pure) ────────────────────────────────────────────────────────


class TestCollect:
    def test_bind_dn_extra_is_enumerated(self) -> None:
        dom = design().phys_dom("prod-phys")
        dom.bind_dn(vlan_pool=POOL_DN)
        (ref,) = _collect(dom)
        assert ref.dn == POOL_DN
        assert ref.rs_class == "infraRsVlanNs"
        assert "phys_dom" in ref.declared_at

    def test_static_path_node_is_enumerated(self) -> None:
        epg = tenant("t").app("a").epg("web")
        epg.static_path(PATH_DN, encap="vlan-100")
        refs = _collect(epg)
        assert [r.dn for r in refs] == [PATH_DN]
        assert refs[0].rs_class == "fvRsPathAtt"

    def test_in_design_targets_are_skipped(self) -> None:
        """A dn-flavor bind to a DECLARED node never enters the set."""
        cfg = tenant("t")
        cfg.bd("web").bind(vrf="prod")
        cfg.vrf("prod")
        assert _collect(cfg) == []

    def test_dedupe_by_dn_and_referencing_class(self) -> None:
        app = tenant("t").app("a")
        app.epg("one").static_path(PATH_DN, encap="vlan-100")
        app.epg("two").static_path(PATH_DN, encap="vlan-200")
        refs = _collect(app)
        assert len(refs) == 1  # same (dn, fvRsPathAtt) — one read, one check

    def test_deterministic_order(self) -> None:
        d = design()
        dom = d.phys_dom("p")
        dom.bind_dn(vlan_pool=POOL_DN)
        d.tenant("t").app("a").epg("web").static_path(PATH_DN, encap="vlan-100")
        refs = _collect(dom)
        assert len(refs) == 2
        assert refs == sorted(refs, key=lambda r: (r.dn, r.rs_class))


# ── Accept-sets and evaluation (pure) ─────────────────────────────────────────


class TestAcceptSet:
    def test_vlan_pool_accepts_the_pool_class(self) -> None:
        assert "fvnsVlanInstP" in _accept_set("infraRsVlanNs")

    def test_static_path_accepts_readonly_fabric_path(self) -> None:
        """THE pin: fabricPathEp is read-only and outside TARGET_SUBCLASSES —
        the catalogue accept-set must include it (false-positive guard)."""
        accepted = _accept_set("fvRsPathAtt")
        assert "fabricPathEp" in accepted

    def test_unknown_class_degrades_to_existence_only(self) -> None:
        assert _accept_set("notAClass") == ()


class TestEvaluate:
    REF = ExternalRef(dn=POOL_DN, rs_class="infraRsVlanNs", declared_at="phys_dom[p]")

    def test_missing(self) -> None:
        check = evaluate(self.REF, [])
        assert check.status == "missing"
        assert check.found is None

    def test_ok(self) -> None:
        check = evaluate(self.REF, [{"fvnsVlanInstP": {"attributes": {}}}])
        assert check.status == "ok"
        assert check.found == "fvnsVlanInstP"

    def test_wrong_class(self) -> None:
        check = evaluate(self.REF, [{"fvTenant": {"attributes": {}}}])
        assert check.status == "wrong_class"
        assert check.found == "fvTenant"
        assert "fvnsVlanInstP" in check.expected

    def test_unverifiable(self) -> None:
        check = evaluate(self.REF, None)
        assert check.status == "unverifiable"


# ── Push wiring (sync) ────────────────────────────────────────────────────────


def _dom_design():  # type: ignore[no-untyped-def]
    dom = design().phys_dom("prod-phys")
    dom.bind_dn(vlan_pool=POOL_DN)
    return dom


class TestVerifiedPush:
    def test_strict_reads_before_writing(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=_envelope("fvnsVlanInstP", POOL_DN))
        httpx_mock.add_response(method="POST", url=f"{HOST}/api/mo/uni.json", json=ok())

        report = _dom_design().push(aci, verify_refs=True)

        assert report.mode == "strict"
        methods = [r.method for r in httpx_mock.get_requests() if "aaaLogin" not in str(r.url)]
        assert methods == ["GET", "POST"]  # verification strictly precedes the write

    def test_strict_missing_target_raises_and_writes_nothing(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
            _dom_design().push(aci, verify_refs=True)

        assert POOL_DN in str(excinfo.value)
        assert [
            r.method for r in httpx_mock.get_requests(method="POST") if "aaaLogin" not in str(r.url)
        ] == []
        (failure,) = excinfo.value.failures
        assert failure.status == "missing"

    def test_staged_missing_target_writes_nothing(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        with pytest.raises(exceptions.DanglingReferenceError):
            _dom_design().push(aci, mode="staged", verify_refs=True)
        posts = [r for r in httpx_mock.get_requests(method="POST") if "aaaLogin" not in str(r.url)]
        assert posts == []

    def test_all_failures_collected_before_raising(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        d = design()
        dom = d.phys_dom("p")
        dom.bind_dn(vlan_pool=POOL_DN)
        d.tenant("t").app("a").epg("web").static_path(PATH_DN, encap="vlan-100")
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
            dom.push(aci, verify_refs=True)

        assert len(excinfo.value.failures) == 2
        dns = [f.ref.dn for f in excinfo.value.failures]
        assert dns == sorted(dns)

    def test_read_error_is_aggregated_not_raised(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            status_code=400,
            json={"imdata": [{"error": {"attributes": {"code": "400", "text": "bad rn"}}}]},
        )
        with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
            _dom_design().push(aci, verify_refs=True)
        (failure,) = excinfo.value.failures
        assert failure.status == "error"
        assert failure.detail

    def test_a_read_error_keeps_the_apic_code(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        """Verification is the one place an APIError is flattened to text.

        Every other path carries the exception object itself, so the code rides
        along for free.  Here the failure becomes a ``RefCheck``, and without
        this the machine-readable cause is lost precisely where a caller is
        looking at a list of things that went wrong.  The code differs from the
        status so the assertion cannot pass by accident.
        """
        httpx_mock.add_response(
            method="GET",
            status_code=400,
            json={"imdata": [{"error": {"attributes": {"code": "104", "text": "bad rn"}}}]},
        )
        with pytest.raises(exceptions.DanglingReferenceError) as excinfo:
            _dom_design().push(aci, verify_refs=True)
        (failure,) = excinfo.value.failures
        assert failure.apic_code == "104"
        assert failure.detail  # the human half is untouched

    def test_plan_populates_statuses_and_never_raises(
        self, aci: Niwaki, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})  # ref read
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})  # plan read
        # boundary-create probe (absent domain root)
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        result = _dom_design().push(aci, mode="plan", verify_refs=True)

        (check,) = result.external_refs
        assert check.status == "missing"
        assert result.creates  # the plan itself still reports normally

    def test_flag_off_is_byte_identical(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        """No flag → zero verification reads; wire traffic is the pre-M6 shape."""
        httpx_mock.add_response(method="POST", url=f"{HOST}/api/mo/uni.json", json=ok())
        _dom_design().push(aci)
        gets = httpx_mock.get_requests(method="GET")
        assert gets == []


# ── Async mirror ──────────────────────────────────────────────────────────────


class TestVerifiedPushAsync:
    async def test_strict_missing_raises_before_writes(self, httpx_mock: HTTPXMock) -> None:
        from niwaki import AsyncNiwaki
        from tests.conftest import LOGIN_URL, login_payload

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(exceptions.DanglingReferenceError):
                await _dom_design().push(aci, verify_refs=True)
        posts = [r for r in httpx_mock.get_requests(method="POST") if "aaaLogin" not in str(r.url)]
        assert posts == []

    async def test_plan_populates_external_refs(self, httpx_mock: HTTPXMock) -> None:
        from niwaki import AsyncNiwaki
        from tests.conftest import LOGIN_URL, login_payload

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=_envelope("fvnsVlanInstP", POOL_DN))
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        # boundary-create probe (absent domain root)
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            result = await _dom_design().push(aci, mode="plan", verify_refs=True)

        (check,) = result.external_refs
        assert check.status == "ok"


class TestExternalRefsInspection:
    def test_matches_the_verified_enumeration(self) -> None:
        dom = _dom_design()
        refs = dom.external_refs()
        assert refs == _collect(dom)

    def test_empty_for_a_closed_design(self) -> None:
        cfg = tenant("t")
        cfg.bd("web").bind(vrf="prod")
        cfg.vrf("prod")
        assert cfg.external_refs() == []
