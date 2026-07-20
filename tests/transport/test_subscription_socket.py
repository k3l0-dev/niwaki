"""
Unit tests for ``niwaki.transport._subscription_socket``.

REST calls (login, subscribe, refresh) are mocked via ``pytest-httpx``, exactly
like every other session test. The WebSocket half is tested against a real
local ``websockets`` server (``FakeWsServer``/``fake_ws_server``, shared via
``tests/conftest.py`` with the query-layer subscription suite) rather than a
mock of the client.

Covers: query construction, array-vs-scalar subscriptionId demux, one frame
fanning out to two subscriptions, forced disconnect -> reconnect -> exactly
one SubscriptionGap, refresh sweep firing and marking a rejected refresh,
reconnect exhausted -> SubscriptionLostError, subscribe rejected ->
SubscribeRejectedError, close() unblocking a consumer, and the
weakref.finalize resource safety net for a session that is never closed.
"""

from __future__ import annotations

import gc
import logging
import threading
import time

import pytest
import stamina
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.transport._config import RetryConfig
from niwaki.transport._subscription_socket import (
    _RECONNECT_ATTEMPTS,
    RawSubscriptionEvent,
    SubscriptionGap,
    SubscriptionRefreshFailed,
    _finalize_socket,
    _refresh_interval,
    _SocketHandle,
)
from niwaki.transport.session import ApicSession
from tests.conftest import FakeWsServer, _wait_until, login_payload, ok, subscribe_response

_REFRESH_REJECTED = {
    "imdata": [{"error": {"attributes": {"code": "400", "text": "Subscription refresh timeout"}}}]
}

# ── subscribe(): the REST half ─────────────────────────────────────────────────


class TestSubscribe:
    def test_returns_initial_snapshot_and_wire_id(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        snapshot = [{"fvBD": {"attributes": {"name": "web"}}}]
        httpx_mock.add_response(method="GET", json=subscribe_response("1001", snapshot))

        sub = ws_session.subscribe("/api/class/fvBD.json", {})

        assert sub.initial == snapshot
        assert sub.subscription_id == "1001"

    def test_appends_subscription_and_refresh_timeout_params(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response())

        ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}, refresh_timeout=45
        )

        request = httpx_mock.get_requests()[-1]
        query = dict(pair.split("=") for pair in request.url.query.decode().split("&"))
        assert query["subscription"] == "yes"
        assert query["refresh-timeout"] == "45"
        assert query["query-target"] == "subtree"

    def test_rejected_subscribe_raises_typed_error(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            status_code=405,
            json={
                "imdata": [
                    {
                        "error": {
                            "attributes": {
                                "code": "405",
                                "text": "unable to locate opened web socket",
                            }
                        }
                    }
                ]
            },
        )

        with pytest.raises(exceptions.SubscribeRejectedError) as exc_info:
            ws_session.subscribe("/api/class/fvBD.json", {})

        assert exc_info.value.status_code == 405
        assert isinstance(exc_info.value, exceptions.SubscriptionError)


# ── Live push demux over the fake WebSocket ────────────────────────────────────


class TestPushDemux:
    def test_event_is_delivered_to_the_subscription(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        fake_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [{"fvBD": {"attributes": {"dn": "uni/tn-x/BD-y", "status": "modified"}}}],
            }
        )

        event = next(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.class_name == "fvBD"
        assert event.status == "modified"
        assert event.attributes["dn"] == "uni/tn-x/BD-y"
        assert event.subscription_ids == ("1001",)

    def test_one_frame_naming_two_ids_fans_out_to_both(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = ws_session.subscribe("/api/class/fvBD.json", {"query-target": "subtree"})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        fake_ws_server.send(
            {
                "subscriptionId": ["1001", "2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "created"}}}],
            }
        )

        event_a = next(sub_a)
        event_b = next(sub_b)
        assert isinstance(event_a, RawSubscriptionEvent)
        assert isinstance(event_b, RawSubscriptionEvent)
        assert event_a.subscription_ids == ("1001", "2002")
        assert event_b.subscription_ids == ("1001", "2002")

    def test_created_modified_deleted_payload_shapes_pass_through_unchanged(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        """The transport layer forwards whatever attributes the wire sent —
        confirmed live: create=full object, modify=delta+dn, delete=dn only."""
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        fake_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [{"fvBD": {"attributes": {"dn": "uni/tn-x/BD-y", "status": "deleted"}}}],
            }
        )
        event = next(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.attributes == {"dn": "uni/tn-x/BD-y", "status": "deleted"}
        assert event.status == "deleted"


# ── Reconnect / gap ────────────────────────────────────────────────────────────


class TestReconnect:
    def test_forced_disconnect_yields_exactly_one_gap_then_resumes(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        # The reconnect will resubscribe from scratch — queue its response.
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        fake_ws_server.disconnect()

        gap = next(sub)
        assert isinstance(gap, SubscriptionGap)
        assert gap.old_subscription_id == "1001"
        assert gap.new_subscription_id == "2002"
        _wait_until(lambda: fake_ws_server.connection_count == 2)

        # The new connection is live — a push over it must still reach us.
        fake_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event = next(sub)
        assert isinstance(event, RawSubscriptionEvent)

    def test_reconnect_exhausted_raises_subscription_lost(
        self, fake_ws_server: FakeWsServer, httpx_mock: HTTPXMock
    ) -> None:
        host = f"http://127.0.0.1:{fake_ws_server.port}"
        httpx_mock.add_response(method="POST", json=login_payload())
        s = ApicSession(
            host=host, username="admin", password="secret", retry=RetryConfig(attempts=1)
        )
        s.login()
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = s.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        # Shut the whole server down — a reconnect attempt will find nothing
        # to connect to (unlike .disconnect(), which just drops one client).
        fake_ws_server.server.shutdown()
        fake_ws_server.disconnect()

        with pytest.raises(exceptions.SubscriptionLostError):
            next(sub)
        s.close()


# ── Refresh sweep ──────────────────────────────────────────────────────────────


class TestRefreshSweep:
    def test_rejected_refresh_yields_marker_and_keeps_going(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        # Force the registration due for refresh right now instead of waiting
        # out the real (30s default / 5s floor) cadence.
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.next_refresh_at = time.monotonic() - 1

        httpx_mock.add_response(
            method="GET",
            status_code=400,
            json={
                "imdata": [
                    {
                        "error": {
                            "attributes": {"code": "400", "text": "Subscription refresh timeout"}
                        }
                    }
                ]
            },
        )

        marker = next(sub)
        assert isinstance(marker, SubscriptionRefreshFailed)
        assert marker.subscription_id == "1001"


# ── close() ────────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_unblocks_a_blocked_consumer(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        result: list[str] = []

        def consume() -> None:
            try:
                next(sub)
            except StopIteration:
                result.append("stopped")

        t = threading.Thread(target=consume)
        t.start()
        time.sleep(0.1)  # let the thread block on queue.get()
        sub.close()
        t.join(timeout=2)

        assert result == ["stopped"]

    def test_session_close_tears_down_the_socket(
        self, fake_ws_server: FakeWsServer, httpx_mock: HTTPXMock
    ) -> None:
        host = f"http://127.0.0.1:{fake_ws_server.port}"
        httpx_mock.add_response(method="POST", json=login_payload())
        s = ApicSession(host=host, username="admin", password="secret")
        s.login()
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = s.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        s.close()

        with pytest.raises(StopIteration):
            next(sub)


# ── Resource safety net (weakref.finalize) ─────────────────────────────────────


class FakeWs:
    """A minimal stand-in for a websockets client connection."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestFinalizeSocketCallback:
    def test_closes_an_open_socket_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        ws = FakeWs()
        handle = _SocketHandle(ws=ws, closed=False)

        with caplog.at_level(logging.WARNING):
            _finalize_socket(handle)

        assert ws.closed is True
        assert any(
            "garbage-collected without an explicit close" in r.message for r in caplog.records
        )

    def test_is_a_noop_after_an_explicit_close(self, caplog: pytest.LogCaptureFixture) -> None:
        ws = FakeWs()
        handle = _SocketHandle(ws=ws, closed=True)  # close() already ran

        with caplog.at_level(logging.WARNING):
            _finalize_socket(handle)

        assert ws.closed is False  # never touched
        assert not caplog.records

    def test_is_a_noop_when_no_socket_was_ever_opened(self) -> None:
        _finalize_socket(_SocketHandle())  # ws=None, closed=False — must not raise


class TestFinalizeSocketWiring:
    def test_dropping_the_session_without_close_triggers_the_finalizer(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        """End-to-end: the real weakref.finalize registration, no explicit close()."""
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        conn = fake_ws_server.connections[-1]
        assert conn.close_code is None

        # Drop the session's only reference to the socket (mirrors an app that
        # forgets to call close()) and force collection.
        ws_session._subscription_socket = None  # type: ignore[reportPrivateUsage]
        gc.collect()

        _wait_until(lambda: conn.close_code is not None)


# ── _refresh_interval() ─────────────────────────────────────────────────────────


class TestRefreshInterval:
    def test_default_cadence_is_20_seconds(self) -> None:
        assert _refresh_interval(None) == 20.0

    def test_explicit_timeout_divides_by_three(self) -> None:
        # A third, not a half — two consecutive misses land with a full
        # interval of margin before the caller's own refresh_timeout expires.
        assert _refresh_interval(90) == 30.0

    def test_explicit_timeout_floors_at_5_seconds(self) -> None:
        assert _refresh_interval(9) == 5.0


# ── Refresh escalation: recover-first, fatal only if recovery also fails ───────


class TestRefreshEscalation:
    def test_escalation_is_isolated_to_the_struggling_registration(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = ws_session.subscribe("/api/class/fvBD.json", {"query-target": "subtree"})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg_a = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg_b = socket._registrations[2]  # type: ignore[reportPrivateUsage]

        # First consecutive failure: below the escalation threshold.
        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker = next(sub_a)
        assert isinstance(marker, SubscriptionRefreshFailed)
        assert marker.consecutive_failures == 1

        # Second consecutive failure: escalates. The recovery resubscribe
        # succeeds -> a SubscriptionGap under a fresh wire id, not a fatal.
        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        gap = next(sub_a)
        assert isinstance(gap, SubscriptionGap)
        assert gap.old_subscription_id == "1001"
        assert gap.new_subscription_id == "3003"
        assert reg_a.consecutive_refresh_failures == 0
        assert sub_a.subscription_id == "3003"

        # The sibling on the same socket was never touched.
        assert reg_b.consecutive_refresh_failures == 0
        fake_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event_b = next(sub_b)
        assert isinstance(event_b, RawSubscriptionEvent)

    def test_escalation_raises_lost_only_if_the_recovery_resubscribe_also_fails(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = ws_session.subscribe("/api/class/fvBD.json", {"query-target": "subtree"})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg_a = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg_b = socket._registrations[2]  # type: ignore[reportPrivateUsage]

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        next(sub_a)  # first REFRESH_FAILED marker, consumed and discarded

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", status_code=405, json=_REFRESH_REJECTED)

        with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
            next(sub_a)
        assert exc_info.value.reason == exceptions.SubscriptionLostReason.REFRESH_ESCALATION

        # The sibling is untouched — no escalation, still receives pushes.
        assert reg_b.consecutive_refresh_failures == 0
        fake_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event_b = next(sub_b)
        assert isinstance(event_b, RawSubscriptionEvent)

    def test_consecutive_counter_resets_on_an_intervening_success(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker1 = next(sub)
        assert isinstance(marker1, SubscriptionRefreshFailed)
        assert marker1.consecutive_failures == 1

        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", json=ok())
        _wait_until(lambda: reg.consecutive_refresh_failures == 0)

        # A single failure after the reset must not have "carried over" —
        # still just a marker, not an escalation.
        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker2 = next(sub)
        assert isinstance(marker2, SubscriptionRefreshFailed)
        assert marker2.consecutive_failures == 1

    def test_reconnect_uses_the_dedicated_backoff_not_the_session_retry(
        self,
        ws_session: ApicSession,
        httpx_mock: HTTPXMock,
        fake_ws_server: FakeWsServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ws_session`` is built with ``RetryConfig(attempts=2)`` (see the
        fixture) — proves reconnect no longer reads the session's retry policy
        at all, by asserting the connect-attempt count instead matches this
        module's own dedicated constant.

        The global ``disable_stamina_delays`` autouse fixture forces stamina's
        testing mode to exactly 1 attempt everywhere by default (see
        ``tests/conftest.py``) — this test locally switches to ``cap=True`` so
        the *code's own* requested attempt count (8, not the session's 2)
        actually plays out, while keeping the zero-delay behaviour.
        """
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        attempts = 0

        def always_fail() -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("simulated: cannot reconnect")

        monkeypatch.setattr(socket, "_open_socket_locked", always_fail)

        with stamina.set_testing(True, attempts=100, cap=True):
            fake_ws_server.disconnect()
            with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
                next(sub)
        assert attempts == _RECONNECT_ATTEMPTS
        assert exc_info.value.reason == exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED


# ── Bulk tools: list / refresh_all / close_all ─────────────────────────────────


class TestListSubscriptions:
    def test_shape_and_staleness(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}, refresh_timeout=45
        )
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None

        infos = socket.list_subscriptions()
        assert len(infos) == 1
        info = infos[0]
        assert info.local_id == 1
        assert info.subscription_id == "1001"
        assert info.path == "/api/class/fvBD.json"
        assert info.params == {"query-target": "subtree"}
        assert info.refresh_timeout == 45
        assert info.consecutive_refresh_failures == 0
        assert info.is_stale is False
        assert info.seconds_until_refresh > 0

        socket._registrations[1].consecutive_refresh_failures = 1  # type: ignore[reportPrivateUsage]
        assert socket.list_subscriptions()[0].is_stale is True

    def test_empty_before_any_subscribe(self, ws_session: ApicSession) -> None:
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is None  # the socket itself is lazy — nothing to list yet


class TestRefreshAllSubscriptions:
    def test_does_not_feed_escalation_on_failure(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.consecutive_refresh_failures = 1  # already showing signs of trouble

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        infos = socket.refresh_all_subscriptions()

        assert infos[0].consecutive_refresh_failures == 1  # untouched, not incremented
        assert infos[0].is_stale is True

    def test_success_resets_counter_and_reschedules(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.consecutive_refresh_failures = 1
        stale_schedule = reg.next_refresh_at

        httpx_mock.add_response(method="GET", json=ok())
        infos = socket.refresh_all_subscriptions()

        assert infos[0].consecutive_refresh_failures == 0
        assert reg.next_refresh_at > stale_schedule


class TestCloseAllSubscriptions:
    def test_stops_every_subscription_but_keeps_the_socket_alive(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = ws_session.subscribe("/api/class/fvBD.json", {"query-target": "subtree"})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        socket.close_all_subscriptions()

        with pytest.raises(StopIteration):
            next(sub_a)
        with pytest.raises(StopIteration):
            next(sub_b)
        assert socket.list_subscriptions() == []
        assert socket._ws is not None  # type: ignore[reportPrivateUsage]
        assert fake_ws_server.connection_count == 1  # no reconnect happened

        # A later subscribe reuses the same still-open socket — no new
        # connection, no reconnect dance.
        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        sub_c = ws_session.subscribe("/api/class/fvBD.json", {})
        assert fake_ws_server.connection_count == 1

        fake_ws_server.send(
            {
                "subscriptionId": ["3003"],
                "imdata": [{"fvBD": {"attributes": {"status": "created"}}}],
            }
        )
        event = next(sub_c)
        assert isinstance(event, RawSubscriptionEvent)

    def test_is_idempotent(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None

        socket.close_all_subscriptions()
        socket.close_all_subscriptions()  # must not raise

        assert socket.list_subscriptions() == []

    def test_does_not_resurrect_a_registration_the_sweep_had_marked_due(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.next_refresh_at = time.monotonic() - 1  # overdue

        # Beat the ~1s sweep tick to this registration.
        socket.close_all_subscriptions()

        with pytest.raises(StopIteration):
            next(sub)
        # No unmocked refresh request escapes — give the sweep a window to
        # run; if it incorrectly refreshed/escalated the removed
        # registration it would either raise here (unmocked httpx request)
        # or leave a non-empty registry.
        time.sleep(1.5)
        assert socket.list_subscriptions() == []


# ── Single-subscription primitives: .info / .refresh_now() ────────────────────


class TestSingleSubscriptionPrimitives:
    def test_info_reflects_current_state(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        assert sub.info.subscription_id == "1001"
        assert sub.info.is_stale is False

    def test_info_after_close_raises(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            _ = sub.info

    def test_refresh_now_resets_the_counter_without_escalating(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        socket._registrations[1].consecutive_refresh_failures = 1  # type: ignore[reportPrivateUsage]

        httpx_mock.add_response(method="GET", json=ok())
        info = sub.refresh_now()

        assert info.consecutive_refresh_failures == 0
        assert info.is_stale is False

    def test_refresh_now_after_close_raises(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            sub.refresh_now()
