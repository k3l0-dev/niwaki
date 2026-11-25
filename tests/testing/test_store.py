"""The fake's store — writes, deletes, seeding, RN computation and the read-only views.

Contract pinned here (decisions recorded for the implementer):

- **Views.** ``fake.get(dn) -> StoredObject | None`` and
  ``fake.objects(aci_class: str | None = None) -> tuple[StoredObject, ...]``
  (sorted by DN).  :class:`StoredObject` is a frozen dataclass
  ``(dn: str, aci_class: str, attributes: Mapping[str, str])``: wire class
  name, wire attributes, ``dn`` **not** repeated inside ``attributes``.
  A view is a value: read-only (item assignment raises ``TypeError``) and
  detached (a later write never changes a view already handed out).
- **POST** ``/api/mo/<dn>.json`` ingests the envelope at any depth; the
  object's parent is the URL DN minus the object's own RN (staged pushes
  post an object at its own DN), or the URL DN itself (``uni`` + polUni).
  An explicit ``dn`` attribute in a payload places the object there.
  Upserts merge attributes.  ``status`` is an instruction, never stored:
  ``status="deleted"`` removes the object's subtree; any other status upserts.
- **DELETE** removes the subtree.
- **RN** is computed by the generated model for a generated class, by the
  catalogue's RN format for a catalogue-only class.
- **seed(payload)** ingests an envelope exactly like a POST would, without
  being a request: a top-level object lands at its explicit ``dn`` attribute,
  else at its own RN from the root (``polUni`` → ``uni``,
  ``fabricTopology`` → ``topology``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import MutableMapping
from typing import cast

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.testing import FakeApic, StoredObject

from .conftest import FABRIC, FABRIC_DNS, request


def _attrs(fake: FakeApic, dn: str) -> dict[str, str]:
    stored = fake.get(dn)
    assert stored is not None, f"nothing stored at {dn}"
    return dict(stored.attributes)


class TestViews:
    def test_get_returns_the_class_and_wire_attributes(self, seeded: FakeApic) -> None:
        stored = seeded.get("uni/tn-prod/BD-web")
        assert stored is not None
        assert stored.dn == "uni/tn-prod/BD-web"
        assert stored.aci_class == "fvBD"
        assert stored.attributes["name"] == "web"
        assert stored.attributes["arpFlood"] == "yes"
        assert "dn" not in stored.attributes

    def test_a_view_compares_by_value_against_a_plain_mapping(self, seeded: FakeApic) -> None:
        assert seeded.get("uni/tn-dev/BD-web") == StoredObject(
            dn="uni/tn-dev/BD-web",
            aci_class="fvBD",
            attributes={"name": "web", "arpFlood": "no"},
        )

    def test_get_of_an_unknown_dn_is_none(self, seeded: FakeApic) -> None:
        assert seeded.get("uni/tn-ghost") is None

    def test_objects_lists_everything_sorted_by_dn(self, seeded: FakeApic) -> None:
        listed = seeded.objects()
        assert isinstance(listed, tuple)
        assert [o.dn for o in listed] == sorted(FABRIC_DNS)

    def test_objects_filters_by_class(self, seeded: FakeApic) -> None:
        assert [o.dn for o in seeded.objects("fvBD")] == [
            "uni/tn-dev/BD-web",
            "uni/tn-prod/BD-db",
            "uni/tn-prod/BD-web",
        ]
        assert seeded.objects("vzBrCP") == ()

    def test_a_view_is_read_only(self, seeded: FakeApic) -> None:
        stored = seeded.get("uni/tn-prod/BD-web")
        assert stored is not None
        with pytest.raises(TypeError):
            cast(MutableMapping[str, str], stored.attributes)["arpFlood"] = "no"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(stored, "dn", "uni/tn-other")  # noqa: B010
        assert _attrs(seeded, "uni/tn-prod/BD-web")["arpFlood"] == "yes"

    def test_a_view_is_detached_from_later_writes(self, seeded: FakeApic) -> None:
        before = seeded.get("uni/tn-prod/BD-web")
        assert before is not None
        seeded.handle(
            request(
                "POST",
                "/api/mo/uni/tn-prod/BD-web.json",
                {"fvBD": {"attributes": {"name": "web", "arpFlood": "no"}}},
            )
        )
        assert before.attributes["arpFlood"] == "yes"
        assert _attrs(seeded, "uni/tn-prod/BD-web")["arpFlood"] == "no"


class TestPost:
    def test_a_poluni_envelope_is_ingested_at_any_depth(self, fake: FakeApic) -> None:
        response = fake.handle(request("POST", "/api/mo/uni.json", FABRIC))
        assert response.status_code == 200
        assert {o.dn for o in fake.objects()} == FABRIC_DNS

    def test_an_object_posted_at_its_own_dn_finds_its_parent(self, fake: FakeApic) -> None:
        body = {"fvBD": {"attributes": {"name": "web"}}}
        fake.handle(request("POST", "/api/mo/uni/tn-prod/BD-web.json", body))
        assert fake.get("uni/tn-prod/BD-web") is not None
        assert fake.get("uni/tn-prod/BD-web/BD-web") is None

    def test_an_explicit_dn_attribute_places_the_object(self, fake: FakeApic) -> None:
        body = {"fvTenant": {"attributes": {"dn": "uni/tn-prod", "name": "prod"}}}
        fake.handle(request("POST", "/api/mo/uni.json", body))
        stored = fake.get("uni/tn-prod")
        assert stored is not None
        assert stored.aci_class == "fvTenant"
        assert "dn" not in stored.attributes

    def test_repeated_posts_merge_attributes(self, fake: FakeApic) -> None:
        fake.handle(request("POST", "/api/mo/uni.json", FABRIC))
        patch = {"fvBD": {"attributes": {"name": "web", "unicastRoute": "no"}}}
        fake.handle(request("POST", "/api/mo/uni/tn-prod/BD-web.json", patch))
        attrs = _attrs(fake, "uni/tn-prod/BD-web")
        assert attrs["unicastRoute"] == "no"
        assert attrs["arpFlood"] == "yes"
        assert attrs["descr"] == 'say "hi"'

    def test_a_later_value_overwrites_an_earlier_one(self, fake: FakeApic) -> None:
        fake.handle(request("POST", "/api/mo/uni.json", FABRIC))
        patch = {"fvBD": {"attributes": {"name": "web", "arpFlood": "no"}}}
        fake.handle(request("POST", "/api/mo/uni/tn-prod/BD-web.json", patch))
        assert _attrs(fake, "uni/tn-prod/BD-web")["arpFlood"] == "no"

    def test_status_deleted_removes_the_subtree(self, seeded: FakeApic) -> None:
        body = {"fvBD": {"attributes": {"name": "web", "status": "deleted"}}}
        seeded.handle(request("POST", "/api/mo/uni/tn-prod/BD-web.json", body))
        assert not [o.dn for o in seeded.objects() if o.dn.startswith("uni/tn-prod/BD-web")]
        assert seeded.get("uni/tn-prod/BD-db") is not None

    def test_status_deleted_works_nested_in_a_parent_envelope(self, seeded: FakeApic) -> None:
        body = {
            "fvTenant": {
                "attributes": {"name": "prod"},
                "children": [{"fvBD": {"attributes": {"name": "db", "status": "deleted"}}}],
            }
        }
        seeded.handle(request("POST", "/api/mo/uni/tn-prod.json", body))
        assert seeded.get("uni/tn-prod/BD-db") is None
        assert seeded.get("uni/tn-prod/BD-db/subnet-[10.0.3.1/24]") is None
        assert seeded.get("uni/tn-prod") is not None

    def test_status_is_never_stored(self, fake: FakeApic) -> None:
        body = {"fvTenant": {"attributes": {"name": "prod", "status": "created,modified"}}}
        fake.handle(request("POST", "/api/mo/uni/tn-prod.json", body))
        assert _attrs(fake, "uni/tn-prod") == {"name": "prod"}


class TestDelete:
    def test_delete_removes_the_subtree_only(self, seeded: FakeApic) -> None:
        response = seeded.handle(request("DELETE", "/api/mo/uni/tn-prod/BD-web.json"))
        assert response.status_code == 200
        assert not [o.dn for o in seeded.objects() if o.dn.startswith("uni/tn-prod/BD-web")]
        assert seeded.get("uni/tn-prod") is not None
        assert seeded.get("uni/tn-prod/BD-db") is not None
        assert seeded.get("uni/tn-dev/BD-web") is not None

    def test_a_dn_prefix_is_not_a_subtree(self, fake: FakeApic) -> None:
        """Deleting ``BD-web`` must not take ``BD-web2`` with it."""
        fake.seed(
            {
                "fvTenant": {
                    "attributes": {"dn": "uni/tn-prod", "name": "prod"},
                    "children": [
                        {"fvBD": {"attributes": {"name": "web"}}},
                        {"fvBD": {"attributes": {"name": "web2"}}},
                    ],
                }
            }
        )
        fake.handle(request("DELETE", "/api/mo/uni/tn-prod/BD-web.json"))
        assert fake.get("uni/tn-prod/BD-web2") is not None

    def test_the_facade_delete_reaches_the_store(self, seeded: FakeApic, aci: Niwaki) -> None:
        aci.tenant("prod").bd("db").delete()
        assert seeded.get("uni/tn-prod/BD-db") is None
        assert seeded.get("uni/tn-prod/BD-db/subnet-[10.0.3.1/24]") is None


class TestRelativeNames:
    def test_a_generated_class_rn_comes_from_its_model(self, fake: FakeApic) -> None:
        fake.handle(request("POST", "/api/mo/uni.json", FABRIC))
        assert fake.get("uni/tn-prod/BD-web/subnet-[10.0.1.1/24]") is not None
        assert fake.get("uni/tn-prod/BD-web/rsctx") is not None

    def test_a_nested_bracket_rn_is_kept_whole(self, seeded: FakeApic) -> None:
        dn = "uni/tn-prod/ap-shop/epg-web/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
        stored = seeded.get(dn)
        assert stored is not None
        assert stored.aci_class == "fvRsPathAtt"

    def test_a_catalogue_only_class_rn_comes_from_the_catalogue(self, fake: FakeApic) -> None:
        # fabricPod is not a generated model: only the catalogue knows "pod-{id}".
        body = {"fabricPod": {"attributes": {"id": "1"}}}
        response = fake.handle(request("POST", "/api/mo/topology.json", body))
        assert response.status_code == 200
        stored = fake.get("topology/pod-1")
        assert stored is not None
        assert stored.aci_class == "fabricPod"


class TestSeed:
    def test_a_poluni_envelope_roots_at_uni(self, fake: FakeApic) -> None:
        fake.seed(FABRIC)
        assert {o.dn for o in fake.objects()} == FABRIC_DNS
        uni = fake.get("uni")
        assert uni is not None
        assert uni.aci_class == "polUni"

    def test_a_top_level_object_lands_at_its_own_rn(self, fake: FakeApic) -> None:
        fake.seed(
            {
                "fabricTopology": {
                    "attributes": {},
                    "children": [{"fabricPod": {"attributes": {"id": "1"}}}],
                }
            }
        )
        assert [o.dn for o in fake.objects()] == ["topology", "topology/pod-1", "uni"]

    def test_an_explicit_dn_places_a_top_level_object(self, fake: FakeApic) -> None:
        fake.seed({"fvBD": {"attributes": {"dn": "uni/tn-prod/BD-web", "name": "web"}}})
        assert [o.dn for o in fake.objects()] == ["uni", "uni/tn-prod/BD-web"]

    def test_seed_then_push_merge(self, fake: FakeApic, aci: Niwaki) -> None:
        fake.seed(FABRIC)
        tenant("prod").bd("web", unicast_routing=False).push(aci)
        attrs = _attrs(fake, "uni/tn-prod/BD-web")
        assert attrs["arpFlood"] == "yes"
        assert "unicastRoute" in attrs
