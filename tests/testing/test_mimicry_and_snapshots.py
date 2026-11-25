"""APIC mimicry, snapshot seeding, and the acceptance properties of a seeded fake.

Contract pinned here (decisions recorded for the implementer):

- **Bitmask order.**  The fake never echoes a bitmask the way it was
  written: what it stores is the same *set* of flags in an order that differs
  from the SDK's canonical one, and storing the stored value again is a
  fixed point (re-posting what was read back changes nothing — so a snapshot
  replays byte-for-byte).
- **Named numbers.**  A number the schema names is stored by its name
  (``dFromPort="80"`` → ``"http"``); a number without a name stays a number.
- **``FakeApic.from_snapshot(snapshot)``** (a classmethod) rebuilds the
  captured objects at their DNs (scope DN + the RN chain of the tree).  The
  acceptance property: ``snapshot.take`` against that fake, at the same scope,
  serialises to the same bytes.  An empty capture gives an empty fake.
- A design pushed, captured, and replayed into a fresh fake plans **no
  change** there; a real change still plans as one.
"""

from __future__ import annotations

import json

from niwaki import Niwaki, snapshot
from niwaki.design import Cursor, tenant
from niwaki.models._wire import split_flags
from niwaki.models.enums.FvRouteScp import FvRouteScp
from niwaki.snapshot import Snapshot
from niwaki.testing import FakeApic
from niwaki.types import JsonObject

from .._jsonpath import as_object, as_objects
from .conftest import FABRIC, request

SUBNET = "uni/tn-prod/BD-web/subnet-[10.0.1.1/24]"


def _design(*, db_flooding: bool = False, cache: bool = False) -> Cursor:
    config = tenant("prod")
    web = config.bd("web", unicast_routing=True, arp_flooding=True).bind(vrf="main")
    web.subnet("10.0.1.1/24", scope={FvRouteScp.PUBLIC, FvRouteScp.SHARED})
    config.bd("db", arp_flooding=db_flooding).bind(vrf="main")
    if cache:
        config.bd("cache")
    config.vrf("main")
    config.filter("f-http").entry("http", tcp=80)
    config.app("shop").epg("web").bind(bd="web")
    return config


def _written_scope(fake: FakeApic) -> str:
    """The subnet scope exactly as the SDK wrote it on the wire."""
    for sent in fake.requests:
        if sent.method != "POST" or sent.url.path == "/api/aaaLogin.json":
            continue
        found = _find(as_object(json.loads(sent.content)), "fvSubnet")
        if found is not None:
            scope = as_object(found["attributes"])["scope"]
            assert isinstance(scope, str)
            return scope
    raise AssertionError("no fvSubnet was posted")


def _find(node: JsonObject, aci_class: str) -> JsonObject | None:
    for cls, body in node.items():
        content = as_object(body)
        if cls == aci_class:
            return content
        for child in as_objects(content.get("children", [])):
            if (hit := _find(child, aci_class)) is not None:
                return hit
    return None


def _stored(fake: FakeApic, dn: str, prop: str) -> str:
    stored = fake.get(dn)
    assert stored is not None, f"nothing stored at {dn}"
    return stored.attributes[prop]


class TestBitmaskOrder:
    def test_a_design_bitmask_is_stored_in_another_order(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        written = _written_scope(fake)
        stored = _stored(fake, SUBNET, "scope")
        assert set(split_flags(stored)) == {"public", "shared"}
        assert stored != written, "the fake must not parrot the SDK's canonical order"

    def test_reposting_the_stored_value_is_a_fixed_point(self, fake: FakeApic) -> None:
        body = {"fvSubnet": {"attributes": {"ip": "10.0.1.1/24", "scope": "public,shared"}}}
        fake.handle(request("POST", f"/api/mo/{SUBNET}.json", body))
        first = _stored(fake, SUBNET, "scope")
        again = {"fvSubnet": {"attributes": {"ip": "10.0.1.1/24", "scope": first}}}
        fake.handle(request("POST", f"/api/mo/{SUBNET}.json", again))
        assert _stored(fake, SUBNET, "scope") == first

    def test_a_single_flag_is_stored_as_is(self, fake: FakeApic) -> None:
        body = {"fvSubnet": {"attributes": {"ip": "10.0.1.1/24", "scope": "private"}}}
        fake.handle(request("POST", f"/api/mo/{SUBNET}.json", body))
        assert _stored(fake, SUBNET, "scope") == "private"


class TestNamedNumbers:
    def test_a_named_number_is_stored_by_its_name(self, fake: FakeApic) -> None:
        dn = "uni/tn-prod/flt-f/e-web"
        body = {
            "vzEntry": {
                "attributes": {
                    "name": "web",
                    "dFromPort": "80",
                    "dToPort": "443",
                    "sFromPort": "8080",
                }
            }
        }
        fake.handle(request("POST", f"/api/mo/{dn}.json", body))
        assert _stored(fake, dn, "dFromPort") == "http"
        assert _stored(fake, dn, "dToPort") == "https"
        assert _stored(fake, dn, "sFromPort") == "8080"

    def test_a_name_is_stored_unchanged(self, fake: FakeApic) -> None:
        dn = "uni/tn-prod/flt-f/e-web"
        body = {"vzEntry": {"attributes": {"name": "web", "dFromPort": "http"}}}
        fake.handle(request("POST", f"/api/mo/{dn}.json", body))
        assert _stored(fake, dn, "dFromPort") == "http"


class TestFromSnapshot:
    def test_a_tenant_capture_replays_byte_for_byte(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        captured = snapshot.take(aci, "uni/tn-prod")
        assert captured.tree is not None
        assert captured.coverage.get("fvBD") == 2

        replayed = FakeApic.from_snapshot(captured)
        with replayed.client() as other:
            again = snapshot.take(other, "uni/tn-prod")
        assert again.to_json() == captured.to_json()

    def test_a_whole_universe_capture_replays_byte_for_byte(
        self, fake: FakeApic, aci: Niwaki
    ) -> None:
        fake.seed(FABRIC)
        captured = snapshot.take(aci)
        assert captured.scope == "uni"

        replayed = FakeApic.from_snapshot(captured)
        with replayed.client() as other:
            again = snapshot.take(other)
        assert again.to_json() == captured.to_json()

    def test_a_hand_written_capture_lands_at_its_dns(self) -> None:
        text = Snapshot(
            scope="uni/tn-x",
            tree={
                "class": "fvTenant",
                "rn": "tn-x",
                "attributes": {"name": "x"},
                "children": [
                    {
                        "class": "fvBD",
                        "rn": "BD-b",
                        "attributes": {"name": "b"},
                        "children": [
                            {
                                "class": "fvSubnet",
                                "rn": "subnet-[10.9.0.1/16]",
                                "attributes": {"ip": "10.9.0.1/16"},
                                "children": [],
                            }
                        ],
                    }
                ],
            },
            coverage={"fvBD": 1, "fvSubnet": 1, "fvTenant": 1},
        ).to_json()
        captured = Snapshot.from_json(text)

        replayed = FakeApic.from_snapshot(captured)
        assert [o.dn for o in replayed.objects()] == [
            "uni",
            "uni/tn-x",
            "uni/tn-x/BD-b",
            "uni/tn-x/BD-b/subnet-[10.9.0.1/16]",
        ]
        subnet = replayed.get("uni/tn-x/BD-b/subnet-[10.9.0.1/16]")
        assert subnet is not None
        assert subnet.aci_class == "fvSubnet"
        assert dict(subnet.attributes) == {"ip": "10.9.0.1/16"}
        with replayed.client() as aci:
            assert snapshot.take(aci, "uni/tn-x").to_json() == text

    def test_an_empty_capture_gives_an_empty_fake(self) -> None:
        replayed = FakeApic.from_snapshot(Snapshot(scope="uni/tn-ghost", tree=None))
        assert [o.dn for o in replayed.objects()] == ["uni"]
        assert list(replayed.requests) == []

    def test_a_replayed_fake_is_a_fresh_independent_fake(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        replayed = FakeApic.from_snapshot(snapshot.take(aci, "uni/tn-prod"))
        assert replayed is not fake
        assert list(replayed.requests) == []
        fake.handle(request("DELETE", "/api/mo/uni/tn-prod.json"))
        assert replayed.get("uni/tn-prod") is not None


class TestPlanAgainstASeededFake:
    def test_what_is_already_there_plans_no_change(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        replayed = FakeApic.from_snapshot(snapshot.take(aci, "uni/tn-prod"))
        with replayed.client() as other:
            plan = _design().push(other, mode="plan")
        assert not plan.has_changes, (plan.creates, plan.updates)
        assert "uni/tn-prod/BD-web" in plan.unchanged
        assert SUBNET in plan.unchanged

    def test_a_real_change_still_plans_as_one(self, fake: FakeApic, aci: Niwaki) -> None:
        _design().push(aci)
        replayed = FakeApic.from_snapshot(snapshot.take(aci, "uni/tn-prod"))
        changed = _design(db_flooding=True, cache=True)
        with replayed.client() as other:
            plan = changed.push(other, mode="plan")
        assert set(plan.updates) == {"uni/tn-prod/BD-db"}
        assert "uni/tn-prod/BD-cache" in plan.creates

    def test_plan_against_the_pushed_fake_itself(self, fake: FakeApic, aci: Niwaki) -> None:
        """The mimicry (reordered bitmask, named port) must not read as drift."""
        _design().push(aci)
        plan = _design().push(aci, mode="plan")
        assert not plan.has_changes, (plan.creates, plan.updates)
