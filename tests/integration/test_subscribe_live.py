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

pytestmark = pytest.mark.integration

TN = "niwaki-it-subscribe"
TN_DESC = "Object-subscription live validation, owns nothing else."


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


def test_created_modified_deleted_events_are_typed_live(live_aci: Niwaki) -> None:
    """A real create/modify/delete sequence produces the expected typed events."""
    tenant(TN, description=TN_DESC).push(live_aci)
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


def test_bulk_tools_against_a_live_socket(live_aci: Niwaki) -> None:
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
