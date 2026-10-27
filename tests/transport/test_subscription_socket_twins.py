"""The equivalence net of the two subscription sockets — one scenario, both twins.

The sync :class:`~niwaki.transport._subscription_socket.SubscriptionSocket` and
the async :class:`~niwaki.transport._subscription_socket_async.AsyncSubscriptionSocket`
are never generated from one another: their concurrency differs by design
(threads + ``queue.Queue`` vs tasks + ``asyncio.Queue``, connect under vs
outside the lock, per-flavour finalisers).  What they *do* share is pure
logic — frame parsing and demultiplexing, the backpressure bound, the
escalation ladder, the gap fan-out, the ``SubscriptionInfo`` snapshot — and
that logic must behave identically on both.  Every test below runs once per
twin (``[sync]``/``[async]``) through the real local WebSocket servers of
``tests/conftest.py`` (REST mocked by ``pytest-httpx``, the socket genuinely
dialled), so a refactor that moves the shared logic into one place is proven
to change nothing observable on either side.

The scenarios are written against what a consumer sees — the items its
iterator yields, the errors it raises, ``info``/``list_subscriptions`` — plus
the registration's own counters where the contract is about them
(``dropped_total``, the queue bound).  Nothing here depends on threads vs
tasks: the adapter waits until an item is buffered before pulling it, so a
regression surfaces as a bounded ``AssertionError``, never a hang.

Scenarios (each ``[sync]`` and ``[async]``):

- **Frame parsing and demultiplexing** — array and scalar ``subscriptionId``;
  one frame fanning out to two subscriptions; several items of one frame in
  order; ``status`` defaulting to ``"modified"``; a frame for an unknown id
  ignored; non-envelope items skipped without losing their valid neighbours;
  malformed frames (invalid JSON, ``null``, array, string, non-object
  attributes) skipped by the ``_dispatch`` guard with the reader surviving on
  the same connection; the warning carrying the frame length, never its
  content.
- **Edge frames (today's exact delivery)** — a non-object ``attributes``
  cuts the frame: what came before is delivered to every target, nothing
  after, one warning; non-string hashable ids are carried and an object id
  routes by its keys, silently; a duplicated id delivers twice; an
  unhashable id voids the frame with a warning; a non-list ``imdata``
  delivers nothing (object/string silently, number/``true`` with a warning);
  a frame with no target is dropped silently even when broken; a non-object
  body is an empty event; a non-string ``status`` passes through.
- **Backpressure** — registration queues unbounded, the bound being
  ``max_pending`` (default ``_DEFAULT_MAX_PENDING``); a burst drops the
  newest events and delivers exactly one ``SubscriptionOverflow`` carrying the
  count at the first drop; one marker per overload episode (hysteresis at half
  the bound) with a cumulative ``dropped_total``; control items exempt from the
  bound; a slow subscription never disturbs its sibling.
- **Escalation** — first scheduled refresh failure is a
  ``SubscriptionRefreshFailed`` marker; the second triggers a recovery
  resubscribe delivering a ``SubscriptionGap``; a failed recovery is a
  ``SubscriptionLostError(REFRESH_ESCALATION)`` with the escalation message and
  drops only that registration; an intervening success resets the counter.
- **Gap fan-out on reconnect** — every subscription receives exactly one gap,
  all sharing one ``disconnected_at``/``reconnected_at``, the old wire ids
  retired and the new ones live; a resubscribe the APIC rejects is fatal
  (``RESUBSCRIBE_FAILED``) for that subscription only; an exhausted reconnect
  fails every subscription (``RECONNECT_EXHAUSTED``).
- **SubscriptionInfo** — every field; ``params`` a snapshot, not a view;
  ``list_subscriptions`` agreeing with each ``info``; ``pending`` counting
  buffered markers; ``subscription_id`` following a gap while ``local_id``
  stays; ``info`` after close raising.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, cast

import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.transport._subscription_socket import (
    _DEFAULT_MAX_PENDING,
    _REFRESH_FAILURE_ESCALATION_THRESHOLD,
    RawPushItem,
    RawSubscription,
    RawSubscriptionEvent,
    SubscriptionGap,
    SubscriptionInfo,
    SubscriptionOverflow,
    SubscriptionRefreshFailed,
    SubscriptionSocket,
    _Registration,
)
from niwaki.transport._subscription_socket_async import (
    AsyncRawSubscription,
    AsyncSubscriptionSocket,
    _AsyncRegistration,
)
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import FakeAsyncWsServer, FakeWsServer, _await_until, ok, subscribe_response

PATH = "/api/class/fvBD.json"

_REFRESH_REJECTED = {
    "imdata": [{"error": {"attributes": {"code": "400", "text": "Subscription refresh timeout"}}}]
}
_SUBSCRIBE_REJECTED = {
    "imdata": [{"error": {"attributes": {"code": "405", "text": "resubscribe refused"}}}]
}

type Sub = RawSubscription | AsyncRawSubscription
type Registration = _Registration | _AsyncRegistration


class _Ended(Exception):
    """The iterator ended cleanly (``StopIteration`` / ``StopAsyncIteration``)."""


def _frame(sub_ids: str | list[str], *items: dict[str, Any]) -> dict[str, Any]:
    """One push frame for *sub_ids* carrying *items* as its ``imdata``."""
    return {"subscriptionId": sub_ids, "imdata": list(items)}


def _bd(name: str, status: str | None = "modified") -> dict[str, Any]:
    """One ``fvBD`` push item (``status`` omitted when ``None``)."""
    attrs: dict[str, str] = {"dn": f"uni/tn-t/BD-{name}"}
    if status is not None:
        attrs["status"] = status
    return {"fvBD": {"attributes": attrs}}


def _burst(sub_id: str, count: int, start: int = 0) -> dict[str, Any]:
    """One frame carrying *count* modified events for *sub_id*."""
    return _frame([sub_id], *(_bd(str(start + i)) for i in range(count)))


def _dn(item: RawPushItem) -> str:
    assert isinstance(item, RawSubscriptionEvent), f"expected an event, got {item!r}"
    return item.attributes["dn"]


# ── The twin adapter ──────────────────────────────────────────────────────────


class _Twin(ABC):
    """Drive one socket flavour through one uniform, awaitable surface."""

    flavour: str

    @property
    @abstractmethod
    def socket(self) -> SubscriptionSocket | AsyncSubscriptionSocket: ...

    @property
    @abstractmethod
    def connection_count(self) -> int: ...

    @abstractmethod
    async def subscribe(
        self,
        params: dict[str, str] | None = None,
        *,
        refresh_timeout: int | None = None,
        max_pending: int | None = None,
    ) -> Sub: ...

    @abstractmethod
    async def pull(self, sub: Sub) -> RawPushItem:
        """The next item, as soon as one is buffered (``_Ended`` on a clean stop)."""

    @abstractmethod
    async def close(self, sub: Sub) -> None: ...

    @abstractmethod
    async def send(self, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    async def send_raw(self, frame: str) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def break_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every reconnect attempt fail with ``OSError``."""

    def registration(self, local_id: int) -> Registration:
        return self.socket._registrations[local_id]

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        return self.socket.list_subscriptions()

    async def wait_connections(self, count: int) -> None:
        await _await_until(lambda: self.connection_count == count, timeout=5.0)

    async def wait_buffered(self, sub: Sub) -> None:
        await _await_until(lambda: sub._closed or sub._queue.qsize() > 0, timeout=5.0)


class _SyncTwin(_Twin):
    flavour = "sync"

    def __init__(self, session: ApicSession, server: FakeWsServer) -> None:
        self.session = session
        self.server = server

    @property
    def socket(self) -> SubscriptionSocket:
        socket = self.session._subscription_socket
        assert socket is not None
        return socket

    @property
    def connection_count(self) -> int:
        return self.server.connection_count

    async def subscribe(
        self,
        params: dict[str, str] | None = None,
        *,
        refresh_timeout: int | None = None,
        max_pending: int | None = None,
    ) -> Sub:
        if max_pending is None:
            return self.session.subscribe(PATH, params or {}, refresh_timeout=refresh_timeout)
        return self.session.subscribe(
            PATH, params or {}, refresh_timeout=refresh_timeout, max_pending=max_pending
        )

    async def pull(self, sub: Sub) -> RawPushItem:
        assert isinstance(sub, RawSubscription)
        await self.wait_buffered(sub)
        try:
            return next(sub)
        except StopIteration:
            raise _Ended from None

    async def close(self, sub: Sub) -> None:
        assert isinstance(sub, RawSubscription)
        sub.close()

    async def send(self, payload: dict[str, Any]) -> None:
        self.server.send(payload)

    async def send_raw(self, frame: str) -> None:
        with self.server.lock:
            conn = self.server.connections[-1]
        conn.send(frame)

    async def disconnect(self) -> None:
        self.server.disconnect()

    def break_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse() -> None:
            raise OSError("simulated: cannot reconnect")

        monkeypatch.setattr(self.socket, "_open_socket_locked", refuse)


class _AsyncTwin(_Twin):
    flavour = "async"

    def __init__(self, session: AsyncApicSession, server: FakeAsyncWsServer) -> None:
        self.session = session
        self.server = server

    @property
    def socket(self) -> AsyncSubscriptionSocket:
        socket = self.session._subscription_socket
        assert socket is not None
        return socket

    @property
    def connection_count(self) -> int:
        return self.server.connection_count

    async def subscribe(
        self,
        params: dict[str, str] | None = None,
        *,
        refresh_timeout: int | None = None,
        max_pending: int | None = None,
    ) -> Sub:
        if max_pending is None:
            return await self.session.subscribe(PATH, params or {}, refresh_timeout=refresh_timeout)
        return await self.session.subscribe(
            PATH, params or {}, refresh_timeout=refresh_timeout, max_pending=max_pending
        )

    async def pull(self, sub: Sub) -> RawPushItem:
        assert isinstance(sub, AsyncRawSubscription)
        await self.wait_buffered(sub)
        try:
            return await anext(sub)
        except StopAsyncIteration:
            raise _Ended from None

    async def close(self, sub: Sub) -> None:
        assert isinstance(sub, AsyncRawSubscription)
        await sub.close()

    async def send(self, payload: dict[str, Any]) -> None:
        await self.server.send(payload)

    async def send_raw(self, frame: str) -> None:
        await self.server.connections[-1].send(frame)

    async def disconnect(self) -> None:
        await self.server.disconnect()

    def break_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def refuse() -> None:
            raise OSError("simulated: cannot reconnect")

        monkeypatch.setattr(self.socket, "_connect", refuse)


@pytest.fixture(params=["sync", "async"])
def twin(request: pytest.FixtureRequest) -> _Twin:
    """The socket under test — the sync and the async twin, in turn."""
    if request.param == "sync":
        return _SyncTwin(
            request.getfixturevalue("ws_session"), request.getfixturevalue("fake_ws_server")
        )
    return _AsyncTwin(
        request.getfixturevalue("async_ws_session"),
        request.getfixturevalue("fake_async_ws_server"),
    )


async def _open(twin: _Twin, httpx_mock: HTTPXMock, wire_id: str = "1001", **kwargs: Any) -> Sub:
    """Subscribe under *wire_id* and wait for the shared socket to be up."""
    httpx_mock.add_response(method="GET", json=subscribe_response(wire_id))
    sub = await twin.subscribe(**kwargs)
    await twin.wait_connections(1)
    return sub


def _force_refresh(twin: _Twin, local_id: int = 1) -> None:
    """Make the sweep consider *local_id* due on its next ~1 s tick."""
    twin.registration(local_id).next_refresh_at = time.monotonic() - 1


# ── Frame parsing and demultiplexing ──────────────────────────────────────────


class TestFrameDemux:
    async def test_an_array_id_frame_reaches_its_subscription(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        await twin.send(_frame(["1001"], _bd("web", "created")))

        event = await twin.pull(sub)
        assert event == RawSubscriptionEvent(
            subscription_ids=("1001",),
            class_name="fvBD",
            attributes={"dn": "uni/tn-t/BD-web", "status": "created"},
            status="created",
        )

    async def test_a_scalar_id_frame_reaches_its_subscription(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        """The subscribe response carries a scalar id; a push may too."""
        sub = await _open(twin, httpx_mock)
        await twin.send(_frame("1001", _bd("web", "deleted")))

        event = await twin.pull(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.subscription_ids == ("1001",)
        assert event.status == "deleted"

    async def test_one_frame_naming_two_ids_fans_out_to_both(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})

        await twin.send(_frame(["1001", "2002"], _bd("shared")))

        event_a = await twin.pull(sub_a)
        event_b = await twin.pull(sub_b)
        assert event_a == event_b
        assert isinstance(event_a, RawSubscriptionEvent)
        assert event_a.subscription_ids == ("1001", "2002")
        # Exactly once each — never duplicated into the same queue.
        assert twin.registration(1).queue.qsize() == 0
        assert twin.registration(2).queue.qsize() == 0

    async def test_the_items_of_one_frame_arrive_in_order(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        await twin.send(_frame(["1001"], _bd("a"), _bd("b"), _bd("c")))

        assert [_dn(await twin.pull(sub)) for _ in range(3)] == [
            "uni/tn-t/BD-a",
            "uni/tn-t/BD-b",
            "uni/tn-t/BD-c",
        ]

    async def test_a_missing_status_defaults_to_modified(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        await twin.send(_frame(["1001"], _bd("web", status=None)))

        event = await twin.pull(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.status == "modified"
        assert event.attributes == {"dn": "uni/tn-t/BD-web"}

    async def test_a_frame_for_an_unknown_id_is_ignored(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        await twin.send(_frame(["9999"], _bd("stranger")))
        await twin.send(_frame(["1001"], _bd("ours")))

        assert _dn(await twin.pull(sub)) == "uni/tn-t/BD-ours"
        assert twin.registration(1).queue.qsize() == 0
        assert twin.connection_count == 1

    async def test_non_envelope_items_are_skipped_without_losing_their_neighbours(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        """An ``imdata`` item that is not a one-key object is not an event;
        the valid items around it are still delivered, in order."""
        sub = await _open(twin, httpx_mock)
        await twin.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [
                    _bd("first"),
                    "not-an-envelope",
                    {"fvBD": {"attributes": {}}, "fvCtx": {"attributes": {}}},
                    _bd("last"),
                ],
            }
        )

        assert _dn(await twin.pull(sub)) == "uni/tn-t/BD-first"
        assert _dn(await twin.pull(sub)) == "uni/tn-t/BD-last"
        assert twin.registration(1).queue.qsize() == 0

    @pytest.mark.parametrize(
        "frame",
        [
            '{"subscriptionId": ["1001"], "imdata": [',
            "null",
            "[1, 2, 3]",
            '"not-an-object"',
            '{"subscriptionId": ["1001"],'
            ' "imdata": [{"fvBD": {"attributes": ["not", "a", "dict"]}}]}',
        ],
        ids=["invalid-json", "json-null", "json-array", "json-string", "attributes-not-a-dict"],
    )
    async def test_a_malformed_frame_is_skipped_and_the_reader_survives(
        self,
        twin: _Twin,
        httpx_mock: HTTPXMock,
        frame: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sub = await _open(twin, httpx_mock)

        with caplog.at_level(logging.WARNING):
            await twin.send_raw(frame)
            # The SAME reader, over the SAME connection, keeps delivering.
            await twin.send(_frame(["1001"], _bd("after")))
            event = await twin.pull(sub)

        assert _dn(event) == "uni/tn-t/BD-after"
        assert twin.registration(1).queue.qsize() == 0
        assert twin.connection_count == 1
        assert any("malformed subscription push frame" in r.getMessage() for r in caplog.records)

    async def test_a_malformed_frame_is_logged_by_length_never_by_content(
        self, twin: _Twin, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        sub = await _open(twin, httpx_mock)
        frame = '{"subscriptionId": ["1001"], "imdata": SECRET-PAYLOAD'

        with caplog.at_level(logging.WARNING):
            await twin.send_raw(frame)
            await twin.send(_frame(["1001"], _bd("after")))
            await twin.pull(sub)

        messages = [
            r.getMessage()
            for r in caplog.records
            if "malformed subscription push frame" in r.getMessage()
        ]
        assert len(messages) == 1
        assert str(len(frame)) in messages[0]
        assert all("SECRET" not in r.getMessage() for r in caplog.records)


# ── Edge frames: today's exact delivery, kept by the extraction ──────────────


def _malformed_warnings(caplog: pytest.LogCaptureFixture) -> int:
    return sum("malformed subscription push frame" in r.getMessage() for r in caplog.records)


async def _send_then_sentinel(twin: _Twin, sub: Sub, frame: str) -> list[RawPushItem]:
    """Send *frame*, then a sentinel frame; return what *frame* delivered.

    The reader handles frames in order, so once the sentinel is pulled every
    item *frame* produced has been pulled before it.
    """
    await twin.send_raw(frame)
    await twin.send(_frame(["1001"], _bd("sentinel")))
    delivered: list[RawPushItem] = []
    while True:
        item = await twin.pull(sub)
        if isinstance(item, RawSubscriptionEvent) and item.attributes.get("dn", "").endswith(
            "BD-sentinel"
        ):
            return delivered
        delivered.append(item)


class TestEdgeFrames:
    """Inputs where a stricter, all-or-nothing parse would deliver differently.

    Measured on both twins before the extraction (they agree on every one):
    the extraction must reproduce each delivery and each warning exactly.
    """

    async def test_a_bad_envelope_cuts_the_frame_after_what_came_before(
        self, twin: _Twin, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Partial delivery: the envelopes before the first non-object
        ``attributes`` are delivered (to every target), nothing after it, and
        the frame is logged as malformed."""
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})
        frame = json.dumps(
            _frame(
                ["1001", "2002"],
                _bd("before"),
                {"fvBD": {"attributes": ["not", "an", "object"]}},
                _bd("after"),
            )
        )

        with caplog.at_level(logging.WARNING):
            delivered = await _send_then_sentinel(twin, sub_a, frame)

        assert [_dn(item) for item in delivered] == ["uni/tn-t/BD-before"]
        assert _dn(await twin.pull(sub_b)) == "uni/tn-t/BD-before"
        assert twin.registration(2).queue.qsize() == 0
        assert _malformed_warnings(caplog) == 1
        assert twin.connection_count == 1

    @pytest.mark.parametrize(
        "ids",
        [["1001", 2002], ["1001", None], {"1001": "x"}],
        ids=["list-with-a-number", "list-with-a-null", "object-keys"],
    )
    async def test_ids_beyond_strings_still_route(
        self,
        twin: _Twin,
        httpx_mock: HTTPXMock,
        ids: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-string hashable id is carried as is and matches nothing; an
        object routes by its keys.  Neither is logged."""
        sub = await _open(twin, httpx_mock)
        frame = json.dumps({"subscriptionId": ids, "imdata": [_bd("routed")]})

        with caplog.at_level(logging.WARNING):
            (event,) = await _send_then_sentinel(twin, sub, frame)

        assert isinstance(event, RawSubscriptionEvent)
        assert event.attributes["dn"] == "uni/tn-t/BD-routed"
        expected = tuple(ids) if isinstance(ids, list) else ("1001",)
        assert cast(tuple[object, ...], event.subscription_ids) == expected
        assert _malformed_warnings(caplog) == 0

    async def test_a_duplicated_id_delivers_twice(self, twin: _Twin, httpx_mock: HTTPXMock) -> None:
        sub = await _open(twin, httpx_mock)
        frame = json.dumps(_frame(["1001", "1001"], _bd("twice")))

        delivered = await _send_then_sentinel(twin, sub, frame)

        assert [_dn(item) for item in delivered] == ["uni/tn-t/BD-twice"] * 2

    @pytest.mark.parametrize(
        "ids", [["1001", [1]], [[1], "1001"], ["1001", {"a": 1}]], ids=["list", "first", "object"]
    )
    async def test_an_unhashable_id_voids_the_frame(
        self,
        twin: _Twin,
        httpx_mock: HTTPXMock,
        ids: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sub = await _open(twin, httpx_mock)
        frame = json.dumps({"subscriptionId": ids, "imdata": [_bd("lost")]})

        with caplog.at_level(logging.WARNING):
            delivered = await _send_then_sentinel(twin, sub, frame)

        assert delivered == []
        assert _malformed_warnings(caplog) == 1

    @pytest.mark.parametrize(
        ("imdata", "warnings"),
        [({"fvBD": {"attributes": {}}}, 0), ("fvBD", 0), (5, 1), (True, 1)],
        ids=["object", "string", "number", "true"],
    )
    async def test_a_non_list_imdata_delivers_nothing(
        self,
        twin: _Twin,
        httpx_mock: HTTPXMock,
        imdata: object,
        warnings: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An object or a string iterates into non-envelopes, silently; a
        number or ``true`` cannot be iterated and is logged."""
        sub = await _open(twin, httpx_mock)
        frame = json.dumps({"subscriptionId": ["1001"], "imdata": imdata})

        with caplog.at_level(logging.WARNING):
            delivered = await _send_then_sentinel(twin, sub, frame)

        assert delivered == []
        assert _malformed_warnings(caplog) == warnings
        assert twin.connection_count == 1

    @pytest.mark.parametrize(
        "imdata",
        [5, [{"fvBD": {"attributes": ["x"]}}]],
        ids=["non-iterable-imdata", "bad-attributes"],
    )
    async def test_a_frame_without_target_is_dropped_silently(
        self,
        twin: _Twin,
        httpx_mock: HTTPXMock,
        imdata: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No target: the frame is dropped before its ``imdata`` is read, so
        even a broken one is not logged."""
        sub = await _open(twin, httpx_mock)
        frame = json.dumps({"subscriptionId": ["9999"], "imdata": imdata})

        with caplog.at_level(logging.WARNING):
            delivered = await _send_then_sentinel(twin, sub, frame)

        assert delivered == []
        assert _malformed_warnings(caplog) == 0

    @pytest.mark.parametrize("body", [None, [1], "x"], ids=["null", "list", "string"])
    async def test_a_non_object_body_is_an_empty_event(
        self, twin: _Twin, httpx_mock: HTTPXMock, body: object
    ) -> None:
        sub = await _open(twin, httpx_mock)
        frame = json.dumps({"subscriptionId": ["1001"], "imdata": [{"fvBD": body}]})

        (event,) = await _send_then_sentinel(twin, sub, frame)

        assert isinstance(event, RawSubscriptionEvent)
        assert (event.class_name, event.attributes, event.status) == ("fvBD", {}, "modified")

    async def test_a_non_string_status_passes_through(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        frame = json.dumps(
            {"subscriptionId": ["1001"], "imdata": [{"fvBD": {"attributes": {"status": 5}}}]}
        )

        (event,) = await _send_then_sentinel(twin, sub, frame)

        assert isinstance(event, RawSubscriptionEvent)
        assert cast(object, event.status) == 5


# ── Backpressure and overflow ─────────────────────────────────────────────────


class TestBackpressure:
    async def test_queues_are_unbounded_and_the_bound_is_max_pending(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        """The precondition of ``put_nowait`` ≡ ``put``: the queue object itself
        is unbounded (``maxsize == 0``) — the bound lives in the dispatcher."""
        await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        await twin.subscribe(max_pending=5)

        default, bounded = twin.registration(1), twin.registration(2)
        assert default.queue.maxsize == 0
        assert bounded.queue.maxsize == 0
        assert default.max_pending == _DEFAULT_MAX_PENDING
        assert bounded.max_pending == 5

    async def test_a_burst_drops_the_newest_and_delivers_one_marker(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock, max_pending=3)
        reg = twin.registration(1)

        await twin.send(_burst("1001", 6))
        await _await_until(lambda: reg.dropped_total == 3)

        # Oldest kept, newest dropped; the marker sits past the bound.
        assert reg.queue.qsize() == 4
        assert [_dn(await twin.pull(sub)) for _ in range(3)] == [
            f"uni/tn-t/BD-{i}" for i in range(3)
        ]
        marker = await twin.pull(sub)
        assert marker == SubscriptionOverflow(dropped_total=1)  # counted at the FIRST drop

        info = sub.info
        assert info.dropped == 3
        assert info.pending == 0

    async def test_one_marker_per_overload_episode(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        """Hysteresis: the episode only ends once the consumer drains to half
        the bound; a second overload inside the episode adds no marker, a new
        episode adds one carrying the cumulative count."""
        sub = await _open(twin, httpx_mock, max_pending=4)
        reg = twin.registration(1)

        await twin.send(_burst("1001", 5))  # 0..3 kept, 4 dropped -> marker
        await _await_until(lambda: reg.dropped_total == 1)
        assert reg.overflow_open is True

        await twin.pull(sub)
        await twin.pull(sub)  # 3 buffered (2 events + marker): above half the bound
        await twin.send(_burst("1001", 2, start=10))  # 10 accepted, 11 dropped
        await _await_until(lambda: reg.dropped_total == 2)
        assert reg.overflow_open is True  # still the same episode

        drained = [await twin.pull(sub) for _ in range(4)]
        assert [type(item) for item in drained].count(SubscriptionOverflow) == 1
        assert reg.queue.qsize() == 0

        await twin.send(_burst("1001", 6, start=20))  # 20..23 kept, 24-25 dropped
        await _await_until(lambda: reg.dropped_total == 4)
        assert [_dn(await twin.pull(sub)) for _ in range(4)] == [
            f"uni/tn-t/BD-{i}" for i in range(20, 24)
        ]
        assert await twin.pull(sub) == SubscriptionOverflow(dropped_total=3)
        assert sub.info.dropped == 4

    async def test_control_items_bypass_the_bound(self, twin: _Twin, httpx_mock: HTTPXMock) -> None:
        """A full buffer can never wedge close() — the stop sentinel is exempt."""
        sub = await _open(twin, httpx_mock, max_pending=2)
        reg = twin.registration(1)

        await twin.send(_burst("1001", 5))
        await _await_until(lambda: reg.dropped_total == 3)
        assert reg.queue.qsize() == 3  # bound + the marker

        await twin.close(sub)
        assert reg.queue.qsize() == 4  # + the stop sentinel, past the bound
        with pytest.raises(_Ended):
            await twin.pull(sub)

    async def test_a_slow_subscription_never_disturbs_its_sibling(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        slow = await _open(twin, httpx_mock, "1001", max_pending=1)
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        fast = await twin.subscribe({"query-target": "subtree"})
        slow_reg, fast_reg = twin.registration(1), twin.registration(2)

        await twin.send(_burst("1001", 4))
        await _await_until(lambda: slow_reg.dropped_total == 3)
        await twin.send(_burst("2002", 3, start=50))

        assert [_dn(await twin.pull(fast)) for _ in range(3)] == [
            f"uni/tn-t/BD-{i}" for i in range(50, 53)
        ]
        assert fast_reg.dropped_total == 0
        assert fast.info.dropped == 0
        assert slow.info.dropped == 3


# ── Escalation: threshold -> gap -> fatal ─────────────────────────────────────


class TestEscalation:
    async def test_the_first_failure_is_a_marker_the_second_recovers_with_a_gap(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})
        reg_a, reg_b = twin.registration(1), twin.registration(2)

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        _force_refresh(twin, 1)
        marker = await twin.pull(sub_a)
        assert marker == SubscriptionRefreshFailed(subscription_id="1001", consecutive_failures=1)
        assert sub_a.info.consecutive_refresh_failures == 1
        assert sub_a.info.is_stale is True

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        before = time.time()
        _force_refresh(twin, 1)
        gap = await twin.pull(sub_a)
        assert isinstance(gap, SubscriptionGap)
        assert (gap.old_subscription_id, gap.new_subscription_id) == ("1001", "3003")
        assert before <= gap.disconnected_at <= gap.reconnected_at <= time.time()
        assert reg_a.consecutive_refresh_failures == 0
        assert sub_a.subscription_id == "3003"
        assert sub_a.info.is_stale is False

        # The sibling on the same socket is untouched and still delivered to;
        # the retired wire id no longer routes anywhere.
        assert reg_b.consecutive_refresh_failures == 0
        await twin.send(_frame(["1001"], _bd("stale-id")))
        await twin.send(_frame(["2002", "3003"], _bd("live")))
        assert _dn(await twin.pull(sub_b)) == "uni/tn-t/BD-live"
        assert _dn(await twin.pull(sub_a)) == "uni/tn-t/BD-live"
        assert reg_a.queue.qsize() == 0

    async def test_a_failed_recovery_is_fatal_with_the_escalation_message(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        _force_refresh(twin, 1)
        assert isinstance(await twin.pull(sub_a), SubscriptionRefreshFailed)

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", status_code=405, json=_SUBSCRIBE_REJECTED)
        _force_refresh(twin, 1)
        with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
            await twin.pull(sub_a)

        assert exc_info.value.reason == exceptions.SubscriptionLostReason.REFRESH_ESCALATION
        assert str(exc_info.value).startswith(
            f"subscription refresh failed {_REFRESH_FAILURE_ESCALATION_THRESHOLD} times "
            "consecutively and the recovery resubscribe also failed: "
        )
        # Only the struggling registration is dropped; its sibling lives on.
        assert [info.local_id for info in twin.list_subscriptions()] == [2]
        with pytest.raises(exceptions.SubscriptionError):
            _ = sub_a.info
        await twin.send(_frame(["2002"], _bd("sibling")))
        assert _dn(await twin.pull(sub_b)) == "uni/tn-t/BD-sibling"

    async def test_an_intervening_success_resets_the_counter(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock)
        reg = twin.registration(1)

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        _force_refresh(twin)
        first = await twin.pull(sub)
        assert first == SubscriptionRefreshFailed(subscription_id="1001", consecutive_failures=1)

        httpx_mock.add_response(method="GET", json=ok())
        _force_refresh(twin)
        await _await_until(lambda: reg.consecutive_refresh_failures == 0, timeout=5.0)

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        _force_refresh(twin)
        again = await twin.pull(sub)
        assert again == SubscriptionRefreshFailed(subscription_id="1001", consecutive_failures=1)


# ── Gap fan-out on reconnect ──────────────────────────────────────────────────


class TestReconnectGapFanOut:
    async def test_every_subscription_gets_one_gap_with_shared_timestamps(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})

        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        httpx_mock.add_response(method="GET", json=subscribe_response("4004"))
        before = time.time()
        await twin.disconnect()

        gap_a = await twin.pull(sub_a)
        gap_b = await twin.pull(sub_b)
        assert isinstance(gap_a, SubscriptionGap)
        assert isinstance(gap_b, SubscriptionGap)
        assert (gap_a.old_subscription_id, gap_a.new_subscription_id) == ("1001", "3003")
        assert (gap_b.old_subscription_id, gap_b.new_subscription_id) == ("2002", "4004")
        # One disconnect, one reconnect: the whole fan-out shares both stamps.
        assert gap_a.disconnected_at == gap_b.disconnected_at
        assert gap_a.reconnected_at == gap_b.reconnected_at
        assert before <= gap_a.disconnected_at <= gap_a.reconnected_at <= time.time()
        assert twin.registration(1).queue.qsize() == 0  # exactly one gap each
        assert twin.registration(2).queue.qsize() == 0

        await twin.wait_connections(2)
        await twin.send(_frame(["1001", "2002"], _bd("retired-ids")))
        await twin.send(_frame(["3003", "4004"], _bd("fresh-ids")))
        assert _dn(await twin.pull(sub_a)) == "uni/tn-t/BD-fresh-ids"
        assert _dn(await twin.pull(sub_b)) == "uni/tn-t/BD-fresh-ids"
        assert {info.subscription_id for info in twin.list_subscriptions()} == {"3003", "4004"}

    async def test_a_rejected_resubscribe_is_fatal_for_that_subscription_only(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})

        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        httpx_mock.add_response(method="GET", status_code=405, json=_SUBSCRIBE_REJECTED)
        await twin.disconnect()

        gap = await twin.pull(sub_a)
        assert isinstance(gap, SubscriptionGap)
        assert gap.new_subscription_id == "3003"
        with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
            await twin.pull(sub_b)
        assert exc_info.value.reason == exceptions.SubscriptionLostReason.RESUBSCRIBE_FAILED
        assert str(exc_info.value).startswith("could not resubscribe after reconnect: ")
        assert [info.local_id for info in twin.list_subscriptions()] == [1]

    async def test_an_exhausted_reconnect_fails_every_subscription(
        self, twin: _Twin, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"})

        twin.break_reconnect(monkeypatch)
        await twin.disconnect()

        for sub in (sub_a, sub_b):
            with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
                await twin.pull(sub)
            assert exc_info.value.reason == exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED
            assert str(exc_info.value).startswith(
                "subscription websocket lost, reconnect exhausted: "
            )
            assert "simulated: cannot reconnect" in str(exc_info.value)
        assert twin.list_subscriptions() == []


# ── SubscriptionInfo ──────────────────────────────────────────────────────────


class TestSubscriptionInfo:
    async def test_info_reflects_the_registration(self, twin: _Twin, httpx_mock: HTTPXMock) -> None:
        sub = await _open(
            twin, httpx_mock, params={"query-target": "subtree"}, refresh_timeout=45, max_pending=7
        )

        info = sub.info
        assert info.local_id == 1
        assert info.subscription_id == "1001"
        assert info.path == PATH
        assert info.params == {"query-target": "subtree"}
        assert info.refresh_timeout == 45
        assert info.consecutive_refresh_failures == 0
        assert 0.0 < info.seconds_until_refresh <= 15.0  # 45 / 3
        assert info.pending == 0
        assert info.dropped == 0
        assert info.is_stale is False

    async def test_info_params_are_a_snapshot_not_a_view(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        params = {"query-target": "subtree"}
        sub = await _open(twin, httpx_mock, params=params)

        params["query-target"] = "self"  # the caller's dict, mutated after subscribe
        info = sub.info
        info.params["rsp-subtree"] = "full"  # the snapshot's dict, mutated

        assert sub.info.params == {"query-target": "subtree"}
        assert twin.registration(1).params == {"query-target": "subtree"}

    async def test_list_subscriptions_agrees_with_each_info(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub_a = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_b = await twin.subscribe({"query-target": "subtree"}, refresh_timeout=60)

        def key(info: SubscriptionInfo) -> tuple[object, ...]:
            return (
                info.local_id,
                info.subscription_id,
                info.path,
                info.params,
                info.refresh_timeout,
                info.consecutive_refresh_failures,
                info.pending,
                info.dropped,
            )

        listed = sorted(twin.list_subscriptions(), key=lambda info: info.local_id)
        assert [key(info) for info in listed] == [key(sub_a.info), key(sub_b.info)]

    async def test_pending_counts_buffered_events_and_markers(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock, max_pending=2)
        reg = twin.registration(1)

        await twin.send(_burst("1001", 4))
        await _await_until(lambda: reg.dropped_total == 2)

        assert sub.info.pending == 3  # two events + the overflow marker
        assert sub.info.dropped == 2
        await twin.pull(sub)
        assert sub.info.pending == 2

    async def test_info_follows_the_wire_id_across_a_gap_and_keeps_local_id(
        self, twin: _Twin, httpx_mock: HTTPXMock
    ) -> None:
        sub = await _open(twin, httpx_mock, "1001")
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        await twin.disconnect()

        assert isinstance(await twin.pull(sub), SubscriptionGap)
        assert sub.info.local_id == 1
        assert sub.info.subscription_id == "2002"
        assert sub.subscription_id == "2002"

    async def test_info_after_close_raises(self, twin: _Twin, httpx_mock: HTTPXMock) -> None:
        sub = await _open(twin, httpx_mock)
        await twin.close(sub)

        with pytest.raises(exceptions.SubscriptionError):
            _ = sub.info
        assert twin.list_subscriptions() == []
