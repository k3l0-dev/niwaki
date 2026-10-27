"""The pure logic shared by the two subscription sockets — contracts and post-state.

The sync and async subscription sockets keep their own concurrency (threads +
``queue.Queue`` vs tasks + ``asyncio.Queue``, connect under vs outside the
lock, per-flavour finalisers, loops, locks) — they are never generated from
one another.  Only the logic that has no concurrency in it is defined once, in
the sync module next to the wire types it produces
(``niwaki.transport._subscription_socket``), and imported by the async module.
The equivalence of the two sockets end to end is pinned by
``tests/transport/test_subscription_socket_twins.py``; this file pins each
shared function on its own, then the parity post-state of the extraction.

Helpers are reached through :func:`getattr` so this file type-checks before
they exist; a missing helper fails with a pointer to its contract below.

Contracts
---------

``_parse_frame(raw: str | bytes) -> ParsedFrame | None``
    ``ParsedFrame = tuple[tuple[object, ...], tuple[RawSubscriptionEvent, ...], bool]``
    — ``(wire_ids, events, truncated)``.  Pure: no I/O, no logging, never
    raises.  It reproduces, input for input, what both sockets' ``_dispatch``
    delivers and logs today (measured on both twins, which agree everywhere);
    the socket keeps the target lookup and the logging:

    - ``None`` -> the socket logs the malformed-frame warning and delivers
      nothing, whatever its registrations.  Exactly when: ``raw`` is not valid
      JSON (including empty text and undecodable bytes); the top-level value
      is not an object; ``subscriptionId`` is truthy, not a string and not
      iterable (a number, ``true``); or one of the ids is unhashable (a nested
      list or object — today's target lookup raises on it).
    - ``wire_ids``: a falsy ``subscriptionId`` (missing, ``null``, ``""``,
      ``0``, ``[]``, ``{}``) gives ``()``; a string gives ``(s,)``; anything
      else gives ``tuple(value)`` as is — a list keeps its order, its
      duplicates and its non-string hashable members (``["1001", 2002]`` ->
      ``("1001", 2002)``), an object gives its keys.
    - ``events``: a falsy ``imdata`` gives none.  A truthy ``imdata`` is
      iterated: an object yields its keys and a string its characters, none
      of which is an envelope, so no event and no truncation; a non-iterable
      one (a number, ``true``) gives no event and ``truncated``.  Each item
      that is a one-key object ``{class_name: body}`` yields one
      :class:`RawSubscriptionEvent` in frame order; any other item is
      skipped.  ``attributes`` is ``body.get("attributes", {})`` for an
      object body and ``{}`` for any other body (``null``, a list, a
      string); ``status`` is ``attributes.get("status", "modified")``,
      passed through whatever its type; ``subscription_ids`` is
      ``wire_ids``.
    - ``truncated``: the first envelope whose ``attributes`` is not an object
      (a list, a string, a number, ``null``) stops the parse there —
      ``events`` keeps every event before it, nothing after it.  The socket
      delivers ``events`` to its targets in order, then logs the
      malformed-frame warning **only if the frame had at least one target**:
      today a frame with no target returns before ``imdata`` is read, so it
      is dropped silently.

``_offer(reg, event) -> None``
    Enqueue one data event into a registration (sync ``_Registration`` or
    async ``_AsyncRegistration``) under its backpressure bound, through the
    queue's ``put_nowait`` — the one enqueue both ``queue.Queue`` and
    ``asyncio.Queue`` share, and the same delivery as ``put`` on the
    registrations' unbounded queues (proven below).  Never blocks, never
    raises.

    - ``qsize() < max_pending``: the event is enqueued.
    - ``qsize() >= max_pending``: the event is dropped (newest-dropped) and
      ``dropped_total`` incremented; on the first drop of an overload episode
      (``overflow_open`` false) one ``SubscriptionOverflow(dropped_total)`` is
      enqueued past the bound, ``overflow_open`` set and one WARNING logged
      (naming ``max_pending``, never the event).  Later drops of the episode
      add nothing but the count.
    - Hysteresis: the episode ends when an event is accepted while
      ``qsize() <= max_pending // 2`` (``overflow_open`` cleared).

``_subscription_info(local_id, reg) -> SubscriptionInfo``
    The diagnostic snapshot of one registration (either flavour): every
    field copied, ``params`` as a fresh ``dict``, ``seconds_until_refresh =
    max(0.0, reg.next_refresh_at - time.monotonic())``, ``pending`` the
    queue's ``qsize()`` (markers and sentinels included), ``dropped`` the
    registration's ``dropped_total``.

The fatal messages — ``_refresh_escalation_lost``, ``_resubscribe_failed_lost``,
``_reconnect_exhausted_lost``, each ``(cause: Exception) -> SubscriptionLostError``
    One builder per recovery path, each returning a fresh, unraised
    :class:`~niwaki.exceptions.SubscriptionLostError` with its reason and the
    exact message both sockets emit today (the cause rendered with ``str``)::

        REFRESH_ESCALATION   "subscription refresh failed {_REFRESH_FAILURE_ESCALATION_THRESHOLD}
                              times consecutively and the recovery resubscribe also failed: {cause}"
        RESUBSCRIBE_FAILED   "could not resubscribe after reconnect: {cause}"
        RECONNECT_EXHAUSTED  "subscription websocket lost, reconnect exhausted: {cause}"

    The queue item stays ``_Fatal(builder(cause))``; ``SubscriptionGap`` and
    ``SubscriptionRefreshFailed`` are already single, shared dataclass
    constructors and get no wrapper.  ``INTERNAL_ERROR`` (the crash net) keeps
    its own message: it names the worker kind (thread vs task) on purpose.

Parity post-state
-----------------

Both sockets enqueue with ``put_nowait`` only (the sync socket drops the
blocking ``put``), call the shared helpers above, and define none of them
themselves.  The twin-parity entries whose only divergence was ``put`` vs
``put_nowait`` are therefore dead and must be deleted from
``tests/twin_parity_allowlist.json``: ``SubscriptionSocket._offer``,
``._escalate``, ``._refresh_one``, ``.close_all_subscriptions`` and
``.unsubscribe``.  The remaining entries of the pair that mentioned the
``put`` divergence keep their other, intended divergence, with the ``put``
clause dropped from their reason and their fingerprint re-recorded.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from niwaki import exceptions
from niwaki.transport import _subscription_socket as socket_module
from niwaki.transport._subscription_socket import (
    _REFRESH_FAILURE_ESCALATION_THRESHOLD,
    _STOP,
    RawSubscriptionEvent,
    SubscriptionInfo,
    SubscriptionOverflow,
    _Registration,
)
from niwaki.transport._subscription_socket_async import _AsyncRegistration

SRC = Path(__file__).resolve().parents[2] / "src" / "niwaki" / "transport"
SYNC_FILE = SRC / "_subscription_socket.py"
ASYNC_FILE = SRC / "_subscription_socket_async.py"
ALLOWLIST = Path(__file__).resolve().parents[1] / "twin_parity_allowlist.json"
SYNC_MODULE = "niwaki.transport._subscription_socket"

type Registration = _Registration | _AsyncRegistration
type ParsedFrame = tuple[tuple[object, ...], tuple[RawSubscriptionEvent, ...], bool]

PARSE_FRAME = "_parse_frame"
OFFER = "_offer"
SUBSCRIPTION_INFO = "_subscription_info"
LOST_BUILDERS = (
    "_refresh_escalation_lost",
    "_resubscribe_failed_lost",
    "_reconnect_exhausted_lost",
)
SHARED_HELPERS = (PARSE_FRAME, OFFER, SUBSCRIPTION_INFO, *LOST_BUILDERS)


def _shared(name: str) -> object:
    """The shared helper *name* from the sync socket module, or a clear failure."""
    helper = getattr(socket_module, name, None)
    if helper is None:
        pytest.fail(
            f"{SYNC_MODULE}.{name} does not exist yet — the shared pure helper "
            "of L8.4 (contract in this file's docstring)"
        )
    return cast(object, helper)


def parse_frame(raw: str | bytes) -> ParsedFrame | None:
    fn = cast(Callable[[str | bytes], ParsedFrame | None], _shared(PARSE_FRAME))
    return fn(raw)


def offer(reg: Registration, event: RawSubscriptionEvent) -> None:
    fn = cast(Callable[[Registration, RawSubscriptionEvent], None], _shared(OFFER))
    fn(reg, event)


def subscription_info(local_id: int, reg: Registration) -> SubscriptionInfo:
    fn = cast(Callable[[int, Registration], SubscriptionInfo], _shared(SUBSCRIPTION_INFO))
    return fn(local_id, reg)


def lost_builder(name: str) -> Callable[[Exception], exceptions.SubscriptionLostError]:
    return cast(Callable[[Exception], exceptions.SubscriptionLostError], _shared(name))


# ── Fixtures and builders ─────────────────────────────────────────────────────


def _event(name: str = "web", status: str = "modified") -> RawSubscriptionEvent:
    return RawSubscriptionEvent(
        subscription_ids=("1001",),
        class_name="fvBD",
        attributes={"dn": f"uni/tn-t/BD-{name}", "status": status},
        status=status,
    )


def _registration(kind: str, **fields: Any) -> Registration:
    base: dict[str, Any] = {
        "path": "/api/class/fvBD.json",
        "params": {},
        "refresh_timeout": None,
        "wire_id": "1001",
    }
    base.update(fields)
    if kind == "sync":
        return _Registration(**base)
    return _AsyncRegistration(**base)


@pytest.fixture(params=["sync", "async"])
def kind(request: pytest.FixtureRequest) -> str:
    """Which registration flavour the shared helper is fed."""
    value: str = request.param
    return value


def _drain(reg: Registration) -> list[object]:
    items: list[object] = []
    while reg.queue.qsize():
        items.append(reg.queue.get_nowait())
    return items


def _frame(**payload: object) -> str:
    return json.dumps(payload)


def _bd(name: str, **extra: str) -> dict[str, object]:
    return {"fvBD": {"attributes": {"dn": f"uni/tn-t/BD-{name}", **extra}}}


# ── _parse_frame ──────────────────────────────────────────────────────────────


class TestParseFrame:
    def test_an_array_id_frame(self) -> None:
        raw = _frame(subscriptionId=["1001", "2002"], imdata=[_bd("web", status="created")])

        assert parse_frame(raw) == (
            ("1001", "2002"),
            (
                RawSubscriptionEvent(
                    subscription_ids=("1001", "2002"),
                    class_name="fvBD",
                    attributes={"dn": "uni/tn-t/BD-web", "status": "created"},
                    status="created",
                ),
            ),
            False,
        )

    def test_a_scalar_id_is_one_id(self) -> None:
        parsed = parse_frame(_frame(subscriptionId="1001", imdata=[_bd("web")]))

        assert parsed is not None
        wire_ids, (event,), truncated = parsed
        assert wire_ids == ("1001",)
        assert event.subscription_ids == ("1001",)
        assert truncated is False

    @pytest.mark.parametrize(
        "ids",
        [None, "", 0, False, [], {}],
        ids=["null", "empty-string", "zero", "false", "empty-list", "empty-object"],
    )
    def test_a_falsy_id_means_no_id(self, ids: object) -> None:
        parsed = parse_frame(_frame(subscriptionId=ids, imdata=[_bd("web")]))

        assert parsed is not None
        assert parsed[0] == ()
        assert len(parsed[1]) == 1  # the events are still parsed

    def test_a_missing_id_means_no_id(self) -> None:
        parsed = parse_frame(_frame(imdata=[_bd("web")]))

        assert parsed is not None
        assert parsed[0] == ()

    def test_non_string_ids_in_the_list_are_carried_as_is(self) -> None:
        parsed = parse_frame(_frame(subscriptionId=["1001", 2002, None], imdata=[_bd("web")]))

        assert parsed is not None
        wire_ids, (event,), truncated = parsed
        assert wire_ids == ("1001", 2002, None)
        assert cast(tuple[object, ...], event.subscription_ids) == ("1001", 2002, None)
        assert truncated is False

    def test_duplicate_ids_are_kept(self) -> None:
        parsed = parse_frame(_frame(subscriptionId=["1001", "1001"], imdata=[_bd("web")]))

        assert parsed is not None
        assert parsed[0] == ("1001", "1001")

    def test_an_object_id_gives_its_keys(self) -> None:
        parsed = parse_frame(_frame(subscriptionId={"1001": "x", "2002": "y"}, imdata=[]))

        assert parsed is not None
        assert parsed[0] == ("1001", "2002")

    @pytest.mark.parametrize(
        "imdata", [None, [], 0, False, {}], ids=["null", "empty-list", "zero", "false", "empty"]
    )
    def test_a_falsy_imdata_means_no_event(self, imdata: object) -> None:
        assert parse_frame(_frame(subscriptionId=["1001"], imdata=imdata)) == (("1001",), (), False)

    def test_a_missing_imdata_means_no_event(self) -> None:
        assert parse_frame(_frame(subscriptionId=["1001"])) == (("1001",), (), False)

    @pytest.mark.parametrize(
        ("imdata", "truncated"),
        [({"fvBD": {"attributes": {}}}, False), ("fvBD", False), (5, True), (True, True)],
        ids=["object", "string", "number", "true"],
    )
    def test_a_non_list_imdata_gives_no_event(self, imdata: object, truncated: bool) -> None:
        """An object or a string iterates into non-envelopes (skipped,
        silent); a number or ``true`` cannot be iterated (truncated)."""
        assert parse_frame(_frame(subscriptionId=["1001"], imdata=imdata)) == (
            ("1001",),
            (),
            truncated,
        )

    def test_items_keep_their_frame_order(self) -> None:
        raw = _frame(subscriptionId=["1001"], imdata=[_bd("a"), _bd("b"), _bd("c")])

        parsed = parse_frame(raw)
        assert parsed is not None
        assert [event.attributes["dn"] for event in parsed[1]] == [
            "uni/tn-t/BD-a",
            "uni/tn-t/BD-b",
            "uni/tn-t/BD-c",
        ]

    def test_status_defaults_to_modified(self) -> None:
        parsed = parse_frame(_frame(subscriptionId=["1001"], imdata=[_bd("web")]))

        assert parsed is not None
        (event,) = parsed[1]
        assert event.status == "modified"
        assert event.attributes == {"dn": "uni/tn-t/BD-web"}  # no status injected

    @pytest.mark.parametrize("status", [5, None], ids=["number", "null"])
    def test_a_non_string_status_passes_through(self, status: object) -> None:
        raw = _frame(subscriptionId=["1001"], imdata=[{"fvBD": {"attributes": {"status": status}}}])

        parsed = parse_frame(raw)
        assert parsed is not None
        (event,) = parsed[1]
        assert cast(object, event.status) == status
        assert cast(dict[str, object], event.attributes) == {"status": status}

    @pytest.mark.parametrize(
        "body",
        [{}, {"attributes": {}}, None, [1], "x"],
        ids=["no-attributes", "empty-attributes", "null-body", "list-body", "string-body"],
    )
    def test_a_body_without_attributes_is_an_empty_event(self, body: object) -> None:
        parsed = parse_frame(_frame(subscriptionId=["1001"], imdata=[{"fvBD": body}]))

        assert parsed is not None
        (event,) = parsed[1]
        assert (event.class_name, event.attributes, event.status) == ("fvBD", {}, "modified")
        assert parsed[2] is False

    def test_non_envelope_items_are_skipped_not_fatal(self) -> None:
        raw = _frame(
            subscriptionId=["1001"],
            imdata=[
                _bd("first"),
                "a string",
                42,
                None,
                {},
                {"fvBD": {"attributes": {}}, "fvCtx": {"attributes": {}}},
                _bd("last"),
            ],
        )

        parsed = parse_frame(raw)
        assert parsed is not None
        assert [event.attributes["dn"] for event in parsed[1]] == [
            "uni/tn-t/BD-first",
            "uni/tn-t/BD-last",
        ]
        assert parsed[2] is False

    @pytest.mark.parametrize(
        "attributes",
        [["x", "y"], ["ab"], [], "dn=x", "", 3, None],
        ids=["list", "pair-list", "empty-list", "string", "empty-string", "number", "null"],
    )
    def test_non_object_attributes_cut_the_frame_short(self, attributes: object) -> None:
        """Partial delivery: everything before the bad envelope, nothing after."""
        raw = _frame(
            subscriptionId=["1001"],
            imdata=[_bd("before"), {"fvBD": {"attributes": attributes}}, _bd("after")],
        )

        parsed = parse_frame(raw)
        assert parsed is not None
        wire_ids, events, truncated = parsed
        assert wire_ids == ("1001",)
        assert [event.attributes["dn"] for event in events] == ["uni/tn-t/BD-before"]
        assert truncated is True

    def test_a_bad_first_envelope_leaves_no_event(self) -> None:
        raw = _frame(subscriptionId=["1001"], imdata=[{"fvBD": {"attributes": []}}, _bd("after")])

        assert parse_frame(raw) == (("1001",), (), True)

    def test_bytes_parse_like_text(self) -> None:
        raw = _frame(subscriptionId=["1001"], imdata=[_bd("café")])

        assert parse_frame(raw.encode()) == parse_frame(raw)
        assert parse_frame(raw.encode()) is not None

    def test_the_result_is_immutable(self) -> None:
        parsed = parse_frame(_frame(subscriptionId=["1001"], imdata=[_bd("web")]))

        assert parsed is not None
        assert isinstance(parsed, tuple)
        assert isinstance(parsed[0], tuple)
        assert isinstance(parsed[1], tuple)

    @pytest.mark.parametrize(
        "raw",
        [
            '{"subscriptionId": ["1001"], "imdata": [',
            "",
            b"\xff\xfe not utf-8",
            "null",
            "true",
            "42",
            '"a string"',
            "[1, 2, 3]",
            _frame(subscriptionId=1001, imdata=[]),
            _frame(subscriptionId=True, imdata=[]),
            _frame(subscriptionId=1.5, imdata=[]),
            _frame(subscriptionId=["1001", [1]], imdata=[]),
            _frame(subscriptionId=[[1], "1001"], imdata=[]),
            _frame(subscriptionId=["1001", {"a": 1}], imdata=[]),
        ],
        ids=[
            "invalid-json",
            "empty-text",
            "undecodable-bytes",
            "json-null",
            "json-bool",
            "json-number",
            "json-string",
            "json-array",
            "id-number",
            "id-true",
            "id-float",
            "id-list-with-a-list",
            "id-list-starting-with-a-list",
            "id-list-with-an-object",
        ],
    )
    def test_a_frame_the_socket_cannot_route_is_none(
        self, raw: str | bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            assert parse_frame(raw) is None
        assert not caplog.records  # pure: the socket logs the skip, not the parser


# ── _offer ────────────────────────────────────────────────────────────────────


class TestOffer:
    def test_below_the_bound_the_event_is_enqueued(self, kind: str) -> None:
        reg = _registration(kind, max_pending=2)
        first, second = _event("a"), _event("b")

        offer(reg, first)
        offer(reg, second)

        assert _drain(reg) == [first, second]
        assert (reg.dropped_total, reg.overflow_open) == (0, False)

    def test_at_the_bound_the_newest_is_dropped_and_one_marker_goes_past_it(
        self, kind: str
    ) -> None:
        reg = _registration(kind, max_pending=2)
        kept = [_event("a"), _event("b")]
        for event in kept:
            offer(reg, event)

        offer(reg, _event("dropped"))

        assert reg.queue.qsize() == 3  # the bound + the marker
        assert reg.dropped_total == 1
        assert reg.overflow_open is True
        assert _drain(reg) == [*kept, SubscriptionOverflow(dropped_total=1)]

    def test_further_drops_of_the_episode_only_count(self, kind: str) -> None:
        reg = _registration(kind, max_pending=2)
        for name in ("a", "b", "c", "d", "e"):
            offer(reg, _event(name))

        assert reg.dropped_total == 3
        items = _drain(reg)
        assert items.count(SubscriptionOverflow(dropped_total=1)) == 1
        assert sum(isinstance(item, SubscriptionOverflow) for item in items) == 1

    def test_the_episode_ends_only_at_half_the_bound(self, kind: str) -> None:
        reg = _registration(kind, max_pending=4)
        for i in range(5):  # 4 kept, 1 dropped -> marker; qsize 5
            offer(reg, _event(str(i)))
        reg.queue.get_nowait()
        reg.queue.get_nowait()  # qsize 3 > 4 // 2

        offer(reg, _event("accepted-still-open"))
        assert reg.overflow_open is True
        assert reg.queue.qsize() == 4

        offer(reg, _event("dropped-same-episode"))
        assert reg.dropped_total == 2
        assert reg.queue.qsize() == 4  # no second marker

        reg.queue.get_nowait()
        reg.queue.get_nowait()  # qsize 2 == 4 // 2
        offer(reg, _event("accepted-closes"))
        assert reg.overflow_open is False

        offer(reg, _event("fills"))  # qsize 4
        offer(reg, _event("new-episode"))  # dropped -> a fresh marker
        assert reg.dropped_total == 3
        assert _drain(reg)[-1] == SubscriptionOverflow(dropped_total=3)

    def test_a_bound_of_one_reopens_only_when_drained(self, kind: str) -> None:
        reg = _registration(kind, max_pending=1)
        offer(reg, _event("a"))
        offer(reg, _event("dropped"))  # marker; qsize 2
        reg.queue.get_nowait()
        reg.queue.get_nowait()  # empty: 0 <= 1 // 2

        offer(reg, _event("b"))

        assert reg.overflow_open is False
        assert _drain(reg) == [_event("b")]

    def test_one_warning_per_episode_never_the_event(
        self, kind: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        reg = _registration(kind, max_pending=1)

        with caplog.at_level(logging.WARNING):
            for i in range(5):
                offer(reg, _event(f"secret-{i}"))

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "max_pending=1" in warnings[0]
        assert all("secret" not in message for message in warnings)


# ── put_nowait is put, on an unbounded queue ─────────────────────────────────


class _RecordingQueue(queue.Queue[object]):
    """A ``queue.Queue`` that records how ``put`` is reached."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[object, bool, float | None]] = []

    def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
        self.calls.append((item, block, timeout))
        super().put(item, block, timeout)


class TestPutNowaitIsPutOnAnUnboundedQueue:
    """Why the sync socket may switch every ``put`` to ``put_nowait``.

    ``queue.Queue.put_nowait(item)`` *is* ``put(item, block=False)``, and
    ``block`` only matters when ``maxsize > 0`` (the wait for a free slot).
    Every registration queue is unbounded — the bound is enforced by
    ``_offer`` on data events, never by the queue object — so the two
    spellings enqueue the same item, wake the same waiting consumer, and
    neither can block nor raise ``Full``.
    """

    def test_put_nowait_is_put_without_blocking(self) -> None:
        q = _RecordingQueue()

        q.put_nowait("item")

        assert q.calls == [("item", False, None)]

    @pytest.mark.parametrize("kind", ["sync", "async"])
    def test_registration_queues_are_unbounded(self, kind: str) -> None:
        assert _registration(kind).queue.maxsize == 0

    def test_an_unbounded_queue_never_reports_full(self) -> None:
        q: queue.Queue[int] = queue.Queue()

        for i in range(20_000):
            q.put_nowait(i)

        assert q.qsize() == 20_000
        assert q.full() is False

    def test_both_spellings_build_the_same_fifo(self) -> None:
        blocking: queue.Queue[object] = queue.Queue()
        nowait: queue.Queue[object] = queue.Queue()
        items: list[object] = [_event("a"), SubscriptionOverflow(dropped_total=1), _STOP]

        for item in items:
            blocking.put(item)
            nowait.put_nowait(item)

        assert blocking.unfinished_tasks == nowait.unfinished_tasks == len(items)
        assert [blocking.get_nowait() for _ in items] == [nowait.get_nowait() for _ in items]

    def test_put_nowait_wakes_a_consumer_blocked_in_another_thread(self) -> None:
        q: queue.Queue[str] = queue.Queue()
        received: list[str] = []
        consumer = threading.Thread(target=lambda: received.append(q.get(timeout=5)))
        consumer.start()
        time.sleep(0.05)  # let it block on get()

        q.put_nowait("woken")
        consumer.join(timeout=5)

        assert received == ["woken"]

    def test_the_equivalence_needs_the_unbounded_precondition(self) -> None:
        """Counter-example: on a full *bounded* queue the spellings part ways."""
        q: queue.Queue[str] = queue.Queue(maxsize=1)
        q.put("occupant")

        with pytest.raises(queue.Full):
            q.put_nowait("refused")

    def test_put_nowait_wakes_an_awaiting_consumer(self) -> None:
        async def scenario() -> str:
            q: asyncio.Queue[str] = asyncio.Queue()
            waiter = asyncio.create_task(q.get())
            await asyncio.sleep(0)
            q.put_nowait("woken")
            return await asyncio.wait_for(waiter, timeout=5)

        assert asyncio.run(scenario()) == "woken"


# ── _subscription_info ────────────────────────────────────────────────────────


class TestSubscriptionInfo:
    def test_every_field_is_copied(self, kind: str) -> None:
        reg = _registration(
            kind,
            params={"query-target": "subtree"},
            refresh_timeout=45,
            wire_id="7007",
            consecutive_refresh_failures=1,
            dropped_total=4,
            max_pending=9,
            next_refresh_at=time.monotonic() + 10.0,
        )
        reg.queue.put_nowait(_event())

        info = subscription_info(3, reg)

        assert (
            info.local_id,
            info.subscription_id,
            info.path,
            info.params,
            info.refresh_timeout,
            info.consecutive_refresh_failures,
            info.pending,
            info.dropped,
        ) == (3, "7007", "/api/class/fvBD.json", {"query-target": "subtree"}, 45, 1, 1, 4)
        assert 0.0 < info.seconds_until_refresh <= 10.0
        assert info.is_stale is True

    def test_params_are_a_copy_both_ways(self, kind: str) -> None:
        reg = _registration(kind, params={"query-target": "subtree"})

        info = subscription_info(1, reg)
        info.params["rsp-subtree"] = "full"
        reg.params["query-target"] = "self"

        assert reg.params == {"query-target": "self"}
        assert info.params == {"query-target": "subtree", "rsp-subtree": "full"}
        assert info.params is not reg.params

    def test_an_overdue_refresh_clamps_to_zero(self, kind: str) -> None:
        reg = _registration(kind, next_refresh_at=time.monotonic() - 100.0)

        assert subscription_info(1, reg).seconds_until_refresh == 0.0

    def test_pending_counts_markers_and_sentinels(self, kind: str) -> None:
        reg = _registration(kind)
        for item in (_event(), SubscriptionOverflow(dropped_total=1), _STOP):
            reg.queue.put_nowait(item)

        assert subscription_info(1, reg).pending == 3

    def test_a_healthy_registration_is_not_stale(self, kind: str) -> None:
        info = subscription_info(1, _registration(kind))

        assert info.consecutive_refresh_failures == 0
        assert info.is_stale is False
        assert (info.pending, info.dropped) == (0, 0)


# ── The fatal-message builders ───────────────────────────────────────────────


_LOST_CASES = [
    pytest.param(
        "_refresh_escalation_lost",
        exceptions.SubscriptionLostReason.REFRESH_ESCALATION,
        f"subscription refresh failed {_REFRESH_FAILURE_ESCALATION_THRESHOLD} times "
        "consecutively and the recovery resubscribe also failed: {cause}",
        id="refresh-escalation",
    ),
    pytest.param(
        "_resubscribe_failed_lost",
        exceptions.SubscriptionLostReason.RESUBSCRIBE_FAILED,
        "could not resubscribe after reconnect: {cause}",
        id="resubscribe-failed",
    ),
    pytest.param(
        "_reconnect_exhausted_lost",
        exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED,
        "subscription websocket lost, reconnect exhausted: {cause}",
        id="reconnect-exhausted",
    ),
]


class TestLostBuilders:
    @pytest.mark.parametrize(("name", "reason", "template"), _LOST_CASES)
    @pytest.mark.parametrize(
        "cause",
        [
            OSError("simulated: cannot reconnect"),
            exceptions.SubscribeRejectedError(405, "resubscribe refused"),
            exceptions.ConnectionError("simulated: APIC unreachable"),
        ],
        ids=["os-error", "subscribe-rejected", "niwaki-connection-error"],
    )
    def test_reason_and_exact_message(
        self,
        name: str,
        reason: exceptions.SubscriptionLostReason,
        template: str,
        cause: Exception,
    ) -> None:
        error = lost_builder(name)(cause)

        assert type(error) is exceptions.SubscriptionLostError
        assert error.reason == reason
        assert str(error) == template.format(cause=cause)

    @pytest.mark.parametrize(("name", "reason", "template"), _LOST_CASES)
    def test_a_fresh_unraised_error_per_call(
        self, name: str, reason: exceptions.SubscriptionLostReason, template: str
    ) -> None:
        cause = OSError("boom")

        first, second = lost_builder(name)(cause), lost_builder(name)(cause)

        assert first is not second
        assert first.__traceback__ is None


# ── Parity post-state of the extraction ──────────────────────────────────────

_RETIRED_ENTRIES = frozenset(
    {
        "SubscriptionSocket._offer",
        "SubscriptionSocket._escalate",
        "SubscriptionSocket._refresh_one",
        "SubscriptionSocket.close_all_subscriptions",
        "SubscriptionSocket.unsubscribe",
    }
)


def _socket_entries() -> dict[str, dict[str, str]]:
    data = json.loads(ALLOWLIST.read_text())
    entries: dict[str, dict[str, str]] = data["parity"]["subscription_socket"]
    return entries


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _string_literals(source: str) -> Iterator[str]:
    """Every string constant of *source* that is not a docstring."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def _class_methods(tree: ast.Module, class_name: str) -> dict[str, ast.AST]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name: item
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    raise AssertionError(f"class {class_name} not found")


def _top_level_defs(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


class TestParityPostState:
    def test_the_put_only_entries_are_retired(self) -> None:
        """Their only divergence was ``put`` vs ``put_nowait`` — gone with
        ``_offer`` shared and the sync socket on ``put_nowait``.  The parity
        guard itself fails on a dead entry; this names which ones die."""
        still_listed = sorted(_RETIRED_ENTRIES & _socket_entries().keys())
        assert not still_listed, f"dead after L8.4, delete from the allowlist: {still_listed}"

    def test_no_entry_still_cites_the_put_divergence(self) -> None:
        stale = sorted(
            name
            for name, entry in _socket_entries().items()
            if "put_nowait" in entry["reason"] or "Queue.put" in entry["reason"]
        )
        assert not stale, f"reason still cites put vs put_nowait: {stale}"

    def test_the_sync_socket_never_calls_a_blocking_put(self) -> None:
        tree = ast.parse(SYNC_FILE.read_text())
        puts = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "put"
        ]
        assert not puts, f"blocking .put() at lines {puts} — use put_nowait"

    def test_the_helpers_are_defined_once_in_the_sync_module(self) -> None:
        sync_tree = ast.parse(SYNC_FILE.read_text())
        async_tree = ast.parse(ASYNC_FILE.read_text())

        missing = sorted(set(SHARED_HELPERS) - _top_level_defs(sync_tree))
        assert not missing, f"not defined at the top of the sync module: {missing}"

        imported = {
            alias.asname or alias.name
            for node in ast.walk(async_tree)
            if isinstance(node, ast.ImportFrom) and node.module == SYNC_MODULE
            for alias in node.names
        }
        not_imported = sorted(set(SHARED_HELPERS) - imported)
        assert not not_imported, f"the async socket must import, not copy: {not_imported}"

        redefined = sorted(set(SHARED_HELPERS) & _top_level_defs(async_tree))
        assert not redefined, f"the async module redefines: {redefined}"

    @pytest.mark.parametrize(
        ("source", "class_name"),
        [(SYNC_FILE, "SubscriptionSocket"), (ASYNC_FILE, "AsyncSubscriptionSocket")],
        ids=["sync", "async"],
    )
    def test_no_socket_keeps_a_private_copy(self, source: Path, class_name: str) -> None:
        methods = _class_methods(ast.parse(source.read_text()), class_name)

        assert not {OFFER, SUBSCRIPTION_INFO} & methods.keys()
        json_calls = [
            node
            for node in ast.walk(methods["_dispatch"])
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "json"
        ]
        assert not json_calls, f"{class_name}._dispatch parses JSON itself"

    def test_the_async_socket_no_longer_imports_json(self) -> None:
        tree = ast.parse(ASYNC_FILE.read_text())
        imported = {
            alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
        }
        assert "json" not in imported

    @pytest.mark.parametrize(
        "fragment",
        [
            "recovery resubscribe also failed",
            "could not resubscribe after reconnect",
            "reconnect exhausted",
        ],
    )
    def test_each_fatal_message_has_one_source(self, fragment: str) -> None:
        in_sync = sum(fragment in s for s in _string_literals(SYNC_FILE.read_text()))
        in_async = sum(fragment in s for s in _string_literals(ASYNC_FILE.read_text()))

        assert (in_sync, in_async) == (1, 0)
