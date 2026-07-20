"""
Unit tests for ``niwaki.query._events``.

Covers: event_from_raw for each RawPushItem variant, EventKind(status) mapping
and its fallback for an unrecognised status, .dn/.class_name None-safety.
"""

from __future__ import annotations

from typing import ClassVar

from niwaki.models.base import ManagedObject
from niwaki.query._events import EventKind, event_from_raw
from niwaki.transport._subscription_socket import (
    RawSubscriptionEvent,
    SubscriptionGap,
    SubscriptionRefreshFailed,
)


class EventSimpleMO(ManagedObject):
    """Minimal registered class so event_from_raw can dispatch to it."""

    _aci_class: ClassVar[str] = "eventSimpleMO"
    _rn_format: ClassVar[str] = "mo-{name}"
    _naming_props: ClassVar[list[str]] = ["name"]

    name: str
    active: bool = True


class TestEventFromRawCreatedModifiedDeleted:
    def test_created_maps_to_created_kind_and_full_mo(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="eventSimpleMO",
            attributes={"name": "x", "active": "yes", "status": "created"},
            status="created",
        )
        event = event_from_raw(raw)
        assert event.kind is EventKind.CREATED
        assert isinstance(event.mo, EventSimpleMO)
        assert event.mo.model_fields_set == {"name", "active"}
        assert event.mo.active is True
        assert event.subscription_ids == ("1001",)
        assert event.raw is raw

    def test_modified_maps_to_modified_kind_and_delta_only(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="eventSimpleMO",
            attributes={"dn": "uni/mo-x", "active": "no", "status": "modified"},
            status="modified",
        )
        event = event_from_raw(raw)
        assert event.kind is EventKind.MODIFIED
        assert event.mo is not None
        assert event.mo.model_fields_set == {"active"}
        assert event.mo.active is False
        assert event.dn == "uni/mo-x"

    def test_deleted_maps_to_deleted_kind_and_empty_fields_set(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="eventSimpleMO",
            attributes={"dn": "uni/mo-x", "status": "deleted"},
            status="deleted",
        )
        event = event_from_raw(raw)
        assert event.kind is EventKind.DELETED
        assert event.mo is not None
        assert event.mo.model_fields_set == set()
        assert event.dn == "uni/mo-x"

    def test_unrecognised_status_falls_back_to_modified(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="eventSimpleMO",
            attributes={"dn": "uni/mo-x", "status": "something-new"},
            status="something-new",
        )
        event = event_from_raw(raw)
        assert event.kind is EventKind.MODIFIED

    def test_class_name_reflects_the_wire_class(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="eventSimpleMO",
            attributes={"name": "x", "status": "created"},
            status="created",
        )
        event = event_from_raw(raw)
        assert event.class_name == "eventSimpleMO"

    def test_unregistered_class_falls_back_to_base_managedobject(self) -> None:
        raw = RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="totallyUnknownClass",
            attributes={"dn": "topology/x", "status": "modified"},
            status="modified",
        )
        event = event_from_raw(raw)
        assert type(event.mo) is ManagedObject
        assert event.class_name == "totallyUnknownClass"


class TestEventFromRawGapAndRefreshFailed:
    def test_gap_has_no_object_and_no_subscription_ids(self) -> None:
        gap = SubscriptionGap(
            disconnected_at=1.0,
            reconnected_at=2.0,
            old_subscription_id="1001",
            new_subscription_id="2002",
        )
        event = event_from_raw(gap)
        assert event.kind is EventKind.GAP
        assert event.mo is None
        assert event.subscription_ids == ()
        assert event.dn is None
        assert event.class_name is None
        assert event.raw is gap

    def test_refresh_failed_has_no_object_and_no_subscription_ids(self) -> None:
        marker = SubscriptionRefreshFailed(subscription_id="1001")
        event = event_from_raw(marker)
        assert event.kind is EventKind.REFRESH_FAILED
        assert event.mo is None
        assert event.subscription_ids == ()
        assert event.dn is None
        assert event.class_name is None
        assert event.raw is marker
