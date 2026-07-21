"""Object-subscription (WebSocket push) — live validation against a real fabric.

Run:
    uv run pytest tests/integration/test_subscribe_live.py -m integration -s

Confirms what the offline suite cannot: the real wire contract behind the
object-subscription primitive — genuine push payload shapes flowing through
``RawSubscriptionEvent``/``SubscriptionEvent``, a real ``subscriptionId``, the
``subscriptionRefresh`` endpoint actually accepting a real id, and the bulk
introspection/refresh/stop tools against a live socket. The offline suite
(``tests/transport/test_subscription_socket*.py``) already proves the
escalation/reconnect/backoff *logic* deterministically against a local
``FakeWsServer``/``FakeAsyncWsServer`` double — this file does not repeat that,
it only proves the live wire contract those doubles stand in for.

Every blocking wait below has a timeout — a live fabric behaving even
slightly differently than expected must fail loud, never hang the suite.

This file owns tenant ``niwaki-it-subscribe``; ``wipe`` (operator-only)
deletes it.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.query import EventKind, Subscription, SubscriptionEvent
from niwaki.transport._subscription_socket import SubscriptionGap, SubscriptionRefreshFailed

pytestmark = pytest.mark.integration

TN = "niwaki-it-subscribe"
TN_DESC = "Object-subscription live validation, owns nothing else."

# Live discovery: subscribing to a subtree immediately after the parent tenant
# itself was just created can silently miss the very next child CREATE push —
# the APIC's eventmgr needs a brief settle window after a brand-new parent DN
# materializes before subtree subscriptions on it reliably fire. An
# already-existing tenant is unaffected, so this only costs the wait once per
# session, the one time it actually has to create the tenant fresh.
_SETTLE_SECONDS = 5.0


@pytest.fixture(scope="session")
def _subscribe_tenant_ready(live_aci: Niwaki) -> None:
    """Ensure tenant ``TN`` exists, settled enough for subtree subscriptions."""
    try:
        live_aci.node(f"uni/tn-{TN}").read()
        return  # already exists from an earlier run/test -- no settle needed
    except NotFoundError:
        pass
    tenant(TN, description=TN_DESC).push(live_aci)
    time.sleep(_SETTLE_SECONDS)


def _with_timeout[T](fn: Callable[[], T], *, timeout: float = 15.0, what: str) -> T:
    """Run a blocking call on a helper thread and fail loud if it outlives *timeout*.

    A live fabric behaving even slightly differently than the assumptions
    baked into this test (an extra housekeeping event, a slow simulator)
    must not hang the suite indefinitely — surface a clear failure instead.
    """
    box: queue.Queue[tuple[str, object]] = queue.Queue()

    def _run() -> None:
        try:
            box.put(("ok", fn()))
        except Exception as exc:  # re-raised on the test thread below
            box.put(("err", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        kind, value = box.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"{what}: no result within {timeout}s — live fabric too slow or unresponsive")
    if kind == "err":
        raise value  # type: ignore[misc]
    return value  # type: ignore[return-value]


def _next_of_kind(
    sub: Subscription[fvBD], kind: EventKind, *, max_events: int = 6, timeout: float = 15.0
) -> SubscriptionEvent[fvBD]:
    """Consume events until *kind* is seen, skipping the fabric's own async churn.

    Live discovery: the APIC emits its own housekeeping ``modified`` pushes on
    a freshly created object (observed: a VNI/segment-id assignment landing as
    a follow-up ``modified`` event a moment after the CREATED push, with no
    caller-initiated write behind it) — a real, uncorrelated event this test
    must tolerate rather than assume strict one-push-in, one-event-out
    ordering. Bounded so a genuinely missing event still fails loud.
    """
    for _ in range(max_events):
        event = _with_timeout(lambda: next(sub), timeout=timeout, what=f"waiting for a {kind} push")
        if event.kind is kind:
            return event
    pytest.fail(f"did not observe a {kind} event within {max_events} pushes")


def _assert_no_event(sub: Subscription[fvBD], *, timeout: float = 8.0) -> None:
    """Assert nothing arrives on *sub* within *timeout* seconds.

    The inverse of :func:`_next_of_kind` — used to prove a filtered
    subscription genuinely excludes a non-matching object rather than just
    not breaking on one.

    Polls the raw queue directly with a real timeout rather than wrapping a
    blocking ``next(sub)`` in a throwaway thread. Live discovery: that thread
    keeps blocking on the queue's single-consumer ``get()`` past this
    function's own timeout — once it "correctly" finds nothing within
    *timeout* and returns, the thread is still alive and still waiting, so it
    silently steals whatever the *next* real event turns out to be (racing
    the very next ``_next_of_kind`` call for it), rather than that event
    reaching the caller who actually expects it.
    """
    try:
        item = sub._raw._queue.get(timeout=timeout)  # type: ignore[reportPrivateUsage]
    except queue.Empty:
        return  # correct: nothing arrived
    pytest.fail(f"expected no event within {timeout}s on a filtered-out object, but got: {item!r}")


def test_created_modified_deleted_events_are_typed_live(
    live_aci: Niwaki, _subscribe_tenant_ready: None
) -> None:
    """A real create/modify/delete sequence produces the expected typed events."""
    tn_dn = f"uni/tn-{TN}"
    bd_name = f"niwaki-it-sub-{int(time.time())}"  # unique every run — guarantees a real CREATE
    bd_dn = f"{tn_dn}/BD-{bd_name}"

    with live_aci.query(fvBD).under(tn_dn).subscribe() as sub:
        tenant(TN).bd(bd_name, description="Live subscription smoke: created.").push(live_aci)
        created = _next_of_kind(sub, EventKind.CREATED)
        assert created.mo is not None
        assert created.mo.name == bd_name

        tenant(TN).bd(bd_name, arp_flooding=True).push(live_aci)
        modified = _next_of_kind(sub, EventKind.MODIFIED)
        assert modified.mo is not None
        assert modified.dn == bd_dn

        live_aci.node(bd_dn).delete()
        deleted = _next_of_kind(sub, EventKind.DELETED)
        assert deleted.dn == bd_dn


def test_filtered_subscription_only_pushes_matching_objects_live(
    live_aci: Niwaki, _subscribe_tenant_ready: None
) -> None:
    """``where()`` carries onto ``subscription=yes`` — the APIC only pushes
    for the object the filter matches, not every object under the scope."""
    tn_dn = f"uni/tn-{TN}"
    suffix = int(time.time())
    match_name = f"niwaki-it-sub-filter-match-{suffix}"
    other_name = f"niwaki-it-sub-filter-other-{suffix}"

    with live_aci.query(fvBD).under(tn_dn).where(name=match_name).subscribe() as sub:
        # A non-matching object under the same scope must produce nothing.
        tenant(TN).bd(other_name).push(live_aci)
        _assert_no_event(sub)

        # The matching object still gets through.
        tenant(TN).bd(match_name).push(live_aci)
        created = _next_of_kind(sub, EventKind.CREATED)
        assert created.mo is not None
        assert created.mo.name == match_name

    live_aci.node(f"{tn_dn}/BD-{other_name}").delete()
    live_aci.node(f"{tn_dn}/BD-{match_name}").delete()


def test_forced_reconnect_recovers_live(live_aci: Niwaki, _subscribe_tenant_ready: None) -> None:
    """Directly closing the shared WebSocket under a live subscription proves
    the reconnect-and-resubscribe path really works against the real fabric,
    not just the offline ``FakeWsServer`` double."""
    tn_dn = f"uni/tn-{TN}"
    sub = live_aci.query(fvBD).under(tn_dn).subscribe()
    try:
        socket = sub._raw._socket  # type: ignore[reportPrivateUsage]
        old_wire_id = sub.subscription_id
        with socket._state_lock:  # type: ignore[reportPrivateUsage]
            ws = socket._ws  # type: ignore[reportPrivateUsage]
        ws.close()  # force-drop the connection, exactly like a real network blip

        gap = _next_of_kind(sub, EventKind.GAP, timeout=20.0)
        assert isinstance(gap.raw, SubscriptionGap)
        assert gap.raw.old_subscription_id == old_wire_id
        assert sub.subscription_id != old_wire_id  # resubscribed under a fresh id, live

        # The new wire id is genuinely live: a push still reaches it.
        bd_name = f"niwaki-it-sub-reconnect-{int(time.time())}"
        tenant(TN).bd(bd_name).push(live_aci)
        created = _next_of_kind(sub, EventKind.CREATED)
        assert created.mo is not None
        assert created.mo.name == bd_name
        live_aci.node(f"{tn_dn}/BD-{bd_name}").delete()
    finally:
        sub.close()


def test_refresh_escalation_recovers_live(live_aci: Niwaki, _subscribe_tenant_ready: None) -> None:
    """Corrupting the wire id forces two real ``subscriptionRefresh``
    rejections from the live APIC, proving escalation's recovery resubscribe
    against the real fabric rather than the offline double."""
    tn_dn = f"uni/tn-{TN}"
    sub = live_aci.query(fvBD).under(tn_dn).subscribe()
    try:
        socket = sub._raw._socket  # type: ignore[reportPrivateUsage]
        local_id = sub._raw._local_id  # type: ignore[reportPrivateUsage]
        old_wire_id = sub.subscription_id
        # Live discovery: the APIC's subscriptionRefresh endpoint parses ``id``
        # as a numeric prefix and ignores trailing garbage — appending a
        # suffix to a real, still-active id (e.g. f"{old_wire_id}-corrupted")
        # is silently accepted as if it were the original id, never rejected.
        # A fixed, plainly non-existent numeric id is what actually forces a
        # real rejection.
        corrupted_id = "99999999999999999"

        with socket._state_lock:  # type: ignore[reportPrivateUsage]
            reg = socket._registrations[local_id]  # type: ignore[reportPrivateUsage]
            reg.wire_id = (
                corrupted_id  # a real subscriptionRefresh call against this id must be rejected
            )
            reg.next_refresh_at = time.monotonic() - 1

        marker = _next_of_kind(sub, EventKind.REFRESH_FAILED, timeout=20.0)
        assert isinstance(marker.raw, SubscriptionRefreshFailed)
        assert marker.raw.consecutive_failures == 1

        with socket._state_lock:  # type: ignore[reportPrivateUsage]
            reg.next_refresh_at = (
                time.monotonic() - 1
            )  # force the 2nd consecutive failure -> escalation

        gap = _next_of_kind(sub, EventKind.GAP, timeout=20.0)
        assert isinstance(gap.raw, SubscriptionGap)
        assert gap.raw.old_subscription_id == corrupted_id
        assert sub.subscription_id not in (old_wire_id, corrupted_id)  # fresh, real resubscribe
    finally:
        sub.close()


def test_bulk_tools_against_a_live_socket(live_aci: Niwaki, _subscribe_tenant_ready: None) -> None:
    """``list()``/``refresh_now()``/``refresh_all()``/``close_all()`` against a
    genuine subscription — proves the ``subscriptionRefresh`` endpoint accepts
    a real id and the facade manager reaches the same live socket state."""
    tn_dn = f"uni/tn-{TN}"
    sub = live_aci.query(fvBD).under(tn_dn).subscribe()
    try:
        infos = live_aci.subscriptions.list()
        assert any(info.subscription_id == sub.subscription_id for info in infos)

        info = sub.refresh_now()
        assert info.consecutive_refresh_failures == 0
        assert info.is_stale is False

        refreshed = live_aci.subscriptions.refresh_all()
        assert any(info.subscription_id == sub.subscription_id for info in refreshed)
    finally:
        sub.close()

    live_aci.subscriptions.close_all()  # idempotent even with nothing left tracked
    assert live_aci.subscriptions.list() == []


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    with contextlib.suppress(NotFoundError):
        aci.node(f"uni/tn-{TN}").delete()
