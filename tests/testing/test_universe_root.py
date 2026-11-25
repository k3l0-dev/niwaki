"""The universe root — ``uni`` always exists, as a ``polUni``, like on every APIC.

A real controller never answers "nothing at ``uni``": the policy universe is
there before anything is written, whatever was pushed and however (one
strict POST at ``uni``, staged per-object POSTs, a seed with explicit DNs).
A fake that only knew ``uni`` once a polUni envelope was posted at it made a
whole-fabric capture come back empty after a staged push, and a
reconciliation report "clean" while a rogue object sat in the tenant.

Contract pinned here (decisions recorded for the implementer):

- Every fake — new, seeded, replayed from a snapshot — holds a ``polUni`` at
  ``uni`` with no attributes.  It is a stored object like any other:
  ``fake.get("uni")`` returns it and ``fake.objects()`` lists it (so a fresh
  fake's ``objects()`` is exactly that one view), and a GET of
  ``/api/mo/uni.json`` answers it.
- It cannot be removed: a DELETE of ``uni`` and a ``status="deleted"`` polUni
  are refused (``"FakeApic:"``) and change nothing.
- Strict and staged pushes of the same design leave the same store, so a
  whole-fabric capture, its reverse import and its emitted code are
  identical whatever the push mode.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki, snapshot
from niwaki.design import Cursor, design, reconcile, tenant, to_code, to_design
from niwaki.exceptions import APIError
from niwaki.testing import FakeApic, StoredObject
from niwaki.transport import ApicSession

from .conftest import dns_of, error_text, request

UNIVERSE = StoredObject(dn="uni", aci_class="polUni", attributes={})


def _multi_domain() -> Cursor:
    """A design spanning the tenant, access-policy and fabric domains."""
    cfg = design()
    acme = cfg.tenant("acme")
    acme.vrf("prod")
    acme.bd("web", unicast_routing=True).bind(vrf="prod").subnet("10.0.1.1/24")
    acme.filter("f-http").entry("http", tcp=80)
    acme.app("shop").epg("web").bind(bd="web")
    access = cfg.infra()
    access.cdp_policy("cdp-off", admin_state="disabled")
    access.vlan_pool("vp", allocation_mode="static")
    cfg.fabric().datetime_policy("prod-time", admin_state="enabled")
    return cfg


def _rogue(fake: FakeApic) -> None:
    fake.seed({"fvBD": {"attributes": {"dn": "uni/tn-T/BD-rogue", "name": "rogue"}}})


class TestAlwaysThere:
    def test_a_new_fake_holds_the_universe_only(self) -> None:
        fake = FakeApic()
        assert fake.get("uni") == UNIVERSE
        assert fake.objects() == (UNIVERSE,)

    def test_the_universe_answers_a_read(self, session: ApicSession) -> None:
        items = session.get("/api/mo/uni.json")
        assert dns_of(items) == ["uni"]
        assert list(items[0]) == ["polUni"]

    def test_a_staged_push_keeps_the_universe(self, fake: FakeApic, aci: Niwaki) -> None:
        config = tenant("T")
        config.bd("b")
        config.push(aci, mode="staged")
        assert fake.get("uni") == UNIVERSE
        assert [o.dn for o in fake.objects()] == ["uni", "uni/tn-T", "uni/tn-T/BD-b"]

    def test_a_seed_with_explicit_dns_keeps_the_universe(self) -> None:
        fake = FakeApic()
        fake.seed({"fvTenant": {"attributes": {"dn": "uni/tn-acme"}}})
        assert fake.get("uni") == UNIVERSE

    def test_a_replayed_capture_has_the_universe(self, fake: FakeApic, aci: Niwaki) -> None:
        tenant("T").bd("b").push(aci)
        replayed = FakeApic.from_snapshot(snapshot.take(aci, "uni/tn-T"))
        assert replayed.get("uni") == UNIVERSE
        empty = FakeApic.from_snapshot(snapshot.Snapshot(scope="uni/tn-ghost", tree=None))
        assert empty.objects() == (UNIVERSE,)

    def test_a_poluni_post_merges_into_the_universe(self, fake: FakeApic) -> None:
        body = {"polUni": {"attributes": {}, "children": []}}
        assert fake.handle(request("POST", "/api/mo/uni.json", body)).status_code == 200
        assert fake.objects() == (UNIVERSE,)


class TestCannotBeRemoved:
    def test_deleting_uni_is_refused(self, seeded: FakeApic) -> None:
        before = seeded.objects()
        response = seeded.handle(request("DELETE", "/api/mo/uni.json"))
        assert response.status_code == 400
        assert error_text(response).startswith("FakeApic:")
        assert seeded.objects() == before

    def test_a_deleted_status_on_the_universe_is_refused(self, seeded: FakeApic) -> None:
        before = seeded.objects()
        body = {"polUni": {"attributes": {"status": "deleted"}}}
        response = seeded.handle(request("POST", "/api/mo/uni.json", body))
        assert response.status_code == 400
        assert error_text(response).startswith("FakeApic:")
        assert seeded.objects() == before

    def test_the_facade_cannot_delete_it_either(self, seeded: FakeApic, aci: Niwaki) -> None:
        with pytest.raises(APIError) as caught:
            aci.node("uni").delete()
        assert caught.value.apic_message.startswith("FakeApic:")
        assert seeded.get("uni/tn-prod") is not None


class TestWholeFabricCaptures:
    def test_a_capture_after_a_staged_push_holds_the_tenant(
        self, fake: FakeApic, aci: Niwaki
    ) -> None:
        config = tenant("T")
        config.bd("b")
        config.push(aci, mode="staged")
        captured = snapshot.take(aci, "uni")
        assert captured.tree is not None
        assert captured.coverage.get("fvTenant") == 1
        assert captured.coverage.get("fvBD") == 1

    def test_a_capture_after_a_seed_holds_the_seeded_tenant(self) -> None:
        fake = FakeApic()
        fake.seed({"fvTenant": {"attributes": {"dn": "uni/tn-acme"}}})
        with fake.client() as aci:
            captured = snapshot.take(aci, "uni")
        assert captured.tree is not None
        assert captured.coverage.get("fvTenant") == 1
        assert "tn-acme" in captured.to_json()

    @pytest.mark.parametrize("mode", ["strict", "staged"])
    def test_reconcile_sees_a_rogue_object_whatever_the_push_mode(
        self, mode: str, fake: FakeApic, aci: Niwaki
    ) -> None:
        config = tenant("T")
        config.bd("b")
        if mode == "staged":
            config.push(aci, mode="staged")
        else:
            config.push(aci)
        _rogue(fake)
        report = reconcile(config, aci)
        assert report.extra == [("uni/tn-T/BD-rogue", "fvBD")]
        assert not report.clean

    def test_strict_and_staged_leave_the_same_store(self) -> None:
        strict, staged = FakeApic(), FakeApic()
        with strict.client() as aci:
            _multi_domain().push(aci)
        with staged.client() as aci:
            _multi_domain().push(aci, mode="staged")
        assert staged.objects() == strict.objects()

    def test_strict_and_staged_captures_round_trip_byte_identical(self) -> None:
        outcomes: list[tuple[str, str]] = []
        for mode in ("strict", "staged"):
            fake = FakeApic()
            with fake.client() as aci:
                if mode == "staged":
                    _multi_domain().push(aci, mode="staged")
                else:
                    _multi_domain().push(aci)
                captured = snapshot.take(aci, "uni")
            assert captured.tree is not None
            outcomes.append((captured.to_json(), to_code(to_design(captured))))
        (strict_json, strict_code), (staged_json, staged_code) = outcomes
        assert staged_json == strict_json
        assert staged_code == strict_code

    def test_a_staged_push_plans_no_change_afterwards(self, fake: FakeApic, aci: Niwaki) -> None:
        _multi_domain().push(aci, mode="staged")
        plan = _multi_domain().push(aci, mode="plan")
        assert not plan.has_changes, (plan.creates, plan.updates)
