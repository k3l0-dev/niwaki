"""
Unit tests for ``niwaki.transport._subscription_socket_async`` — the async mirror.

Same philosophy as the sync suite (``test_subscription_socket.py``): REST
calls mocked via ``pytest-httpx``, the WebSocket half tested against a real
local ``websockets.asyncio.server`` (``FakeAsyncWsServer``/``fake_async_ws_server``,
shared via ``tests/conftest.py``), never a mock of the client.

Covers the same matrix as the sync suite, plus the async-specific safety-net
mechanics: ``_finalize_async_socket`` uses ``socket.shutdown(SHUT_RDWR)``
(verified live to be the one loop-independent primitive that both releases
the APIC-side resource and wakes a suspended ``recv()``), and the
GC-while-a-task-is-alive wiring test that locks in why the reader/refresh
task entry points must be free functions holding only a weak reference.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import socket as socket_module
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
)
from niwaki.transport._subscription_socket_async import (
    _AsyncSocketHandle,
    _finalize_async_socket,
)
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import FakeAsyncWsServer, _await_until, login_payload, ok, subscribe_response

_REFRESH_REJECTED = {
    "imdata": [{"error": {"attributes": {"code": "400", "text": "Subscription refresh timeout"}}}]
}

# ── subscribe(): the REST half ─────────────────────────────────────────────────


class TestSubscribe:
    async def test_returns_initial_snapshot_and_wire_id(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
    ) -> None:
        snapshot = [{"fvBD": {"attributes": {"name": "web"}}}]
        httpx_mock.add_response(method="GET", json=subscribe_response("1001", snapshot))

        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})

        assert sub.initial == snapshot
        assert sub.subscription_id == "1001"

    async def test_appends_subscription_and_refresh_timeout_params(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response())

        await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}, refresh_timeout=45
        )

        request = httpx_mock.get_requests()[-1]
        query = dict(pair.split("=") for pair in request.url.query.decode().split("&"))
        assert query["subscription"] == "yes"
        assert query["refresh-timeout"] == "45"
        assert query["query-target"] == "subtree"

    async def test_rejected_subscribe_raises_typed_error(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
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
            await async_ws_session.subscribe("/api/class/fvBD.json", {})

        assert exc_info.value.status_code == 405
        assert isinstance(exc_info.value, exceptions.SubscriptionError)


# ── Live push demux over the fake WebSocket ────────────────────────────────────


class TestPushDemux:
    async def test_event_is_delivered_to_the_subscription(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [{"fvBD": {"attributes": {"dn": "uni/tn-x/BD-y", "status": "modified"}}}],
            }
        )

        event = await anext(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.class_name == "fvBD"
        assert event.status == "modified"
        assert event.attributes["dn"] == "uni/tn-x/BD-y"
        assert event.subscription_ids == ("1001",)

    async def test_one_frame_naming_two_ids_fans_out_to_both(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}
        )
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001", "2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "created"}}}],
            }
        )

        event_a = await anext(sub_a)
        event_b = await anext(sub_b)
        assert isinstance(event_a, RawSubscriptionEvent)
        assert isinstance(event_b, RawSubscriptionEvent)
        assert event_a.subscription_ids == ("1001", "2002")
        assert event_b.subscription_ids == ("1001", "2002")

    async def test_created_modified_deleted_payload_shapes_pass_through_unchanged(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [{"fvBD": {"attributes": {"dn": "uni/tn-x/BD-y", "status": "deleted"}}}],
            }
        )
        event = await anext(sub)
        assert isinstance(event, RawSubscriptionEvent)
        assert event.attributes == {"dn": "uni/tn-x/BD-y", "status": "deleted"}
        assert event.status == "deleted"


# ── Reconnect / gap ────────────────────────────────────────────────────────────


class TestReconnect:
    async def test_forced_disconnect_yields_exactly_one_gap_then_resumes(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        await fake_async_ws_server.disconnect()

        gap = await anext(sub)
        assert isinstance(gap, SubscriptionGap)
        assert gap.old_subscription_id == "1001"
        assert gap.new_subscription_id == "2002"
        await _await_until(lambda: fake_async_ws_server.connection_count == 2)

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event = await anext(sub)
        assert isinstance(event, RawSubscriptionEvent)

    async def test_reconnect_exhausted_raises_subscription_lost(
        self, fake_async_ws_server: FakeAsyncWsServer, httpx_mock: HTTPXMock
    ) -> None:
        host = f"http://127.0.0.1:{fake_async_ws_server.port}"
        httpx_mock.add_response(method="POST", json=login_payload())
        s = AsyncApicSession(
            host=host, username="admin", password="secret", retry=RetryConfig(attempts=1)
        )
        await s.login()
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await s.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        fake_async_ws_server.server.close()
        await fake_async_ws_server.server.wait_closed()
        await fake_async_ws_server.disconnect()

        with pytest.raises(exceptions.SubscriptionLostError):
            await anext(sub)
        await s.close()


# ── Refresh sweep ──────────────────────────────────────────────────────────────


class TestRefreshSweep:
    async def test_rejected_refresh_yields_marker_and_keeps_going(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
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

        marker = await anext(sub)
        assert isinstance(marker, SubscriptionRefreshFailed)
        assert marker.subscription_id == "1001"


# ── close() ────────────────────────────────────────────────────────────────────


class TestClose:
    async def test_close_unblocks_a_blocked_consumer(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        task = asyncio.create_task(anext(sub))
        await asyncio.sleep(0.05)  # let it park on queue.get()
        await sub.close()

        with pytest.raises(StopAsyncIteration):
            await task

    async def test_session_close_tears_down_the_socket(
        self, fake_async_ws_server: FakeAsyncWsServer, httpx_mock: HTTPXMock
    ) -> None:
        host = f"http://127.0.0.1:{fake_async_ws_server.port}"
        httpx_mock.add_response(method="POST", json=login_payload())
        s = AsyncApicSession(host=host, username="admin", password="secret")
        await s.login()
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await s.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await s.close()

        with pytest.raises(StopAsyncIteration):
            await anext(sub)


# ── Resource safety net (weakref.finalize) ─────────────────────────────────────


class FakeSock:
    """A minimal stand-in for the raw socket a TransportSocket wraps."""

    def __init__(self) -> None:
        self.shutdown_called_with: int | None = None

    def shutdown(self, how: int) -> None:
        self.shutdown_called_with = how


class TestFinalizeAsyncSocketCallback:
    def test_closes_an_open_socket_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        sock = FakeSock()
        handle = _AsyncSocketHandle(sock=sock, closed=False)

        with caplog.at_level(logging.WARNING):
            _finalize_async_socket(handle)

        assert sock.shutdown_called_with == socket_module.SHUT_RDWR
        assert any(
            "garbage-collected without an explicit close" in r.message for r in caplog.records
        )

    def test_is_a_noop_after_an_explicit_close(self, caplog: pytest.LogCaptureFixture) -> None:
        sock = FakeSock()
        handle = _AsyncSocketHandle(sock=sock, closed=True)  # close() already ran

        with caplog.at_level(logging.WARNING):
            _finalize_async_socket(handle)

        assert sock.shutdown_called_with is None
        assert not caplog.records

    def test_is_a_noop_when_no_socket_was_ever_opened(self) -> None:
        _finalize_async_socket(_AsyncSocketHandle())  # sock=None, closed=False — must not raise


class TestFinalizeAsyncSocketWiring:
    async def test_dropping_the_session_without_close_triggers_the_finalizer(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        """End-to-end: the real weakref.finalize registration, no explicit close()."""
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        conn = fake_async_ws_server.connections[-1]
        assert conn.close_code is None

        # Drop the session's only reference to the socket (mirrors an app that
        # forgets to call close()) and force collection.
        async_ws_session._subscription_socket = None  # type: ignore[reportPrivateUsage]
        gc.collect()

        await _await_until(lambda: conn.close_code is not None)


# ── Refresh escalation: recover-first, fatal only if recovery also fails ───────


class TestRefreshEscalation:
    async def test_escalation_is_isolated_to_the_struggling_registration(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}
        )
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg_a = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg_b = socket._registrations[2]  # type: ignore[reportPrivateUsage]

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker = await anext(sub_a)
        assert isinstance(marker, SubscriptionRefreshFailed)
        assert marker.consecutive_failures == 1

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        gap = await anext(sub_a)
        assert isinstance(gap, SubscriptionGap)
        assert gap.old_subscription_id == "1001"
        assert gap.new_subscription_id == "3003"
        assert reg_a.consecutive_refresh_failures == 0
        assert sub_a.subscription_id == "3003"

        assert reg_b.consecutive_refresh_failures == 0
        await fake_async_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event_b = await anext(sub_b)
        assert isinstance(event_b, RawSubscriptionEvent)

    async def test_escalation_raises_lost_only_if_the_recovery_resubscribe_also_fails(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}
        )
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg_a = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg_b = socket._registrations[2]  # type: ignore[reportPrivateUsage]

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        await anext(sub_a)  # first REFRESH_FAILED marker, consumed and discarded

        reg_a.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        httpx_mock.add_response(method="GET", status_code=405, json=_REFRESH_REJECTED)

        with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
            await anext(sub_a)
        assert exc_info.value.reason == exceptions.SubscriptionLostReason.REFRESH_ESCALATION

        assert reg_b.consecutive_refresh_failures == 0
        await fake_async_ws_server.send(
            {
                "subscriptionId": ["2002"],
                "imdata": [{"fvBD": {"attributes": {"status": "modified"}}}],
            }
        )
        event_b = await anext(sub_b)
        assert isinstance(event_b, RawSubscriptionEvent)

    async def test_consecutive_counter_resets_on_an_intervening_success(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker1 = await anext(sub)
        assert isinstance(marker1, SubscriptionRefreshFailed)
        assert marker1.consecutive_failures == 1

        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", json=ok())
        await _await_until(lambda: reg.consecutive_refresh_failures == 0)

        reg.next_refresh_at = time.monotonic() - 1
        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        marker2 = await anext(sub)
        assert isinstance(marker2, SubscriptionRefreshFailed)
        assert marker2.consecutive_failures == 1

    async def test_reconnect_uses_the_dedicated_backoff_not_the_session_retry(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``async_ws_session`` is built with ``RetryConfig(attempts=2)`` (see
        the fixture) — proves reconnect no longer reads the session's retry
        policy, by asserting the connect-attempt count instead matches this
        module's own dedicated constant (see the sync twin for why
        ``stamina.set_testing(True, attempts=100, cap=True)`` is needed here).
        """
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        attempts = 0

        async def always_fail() -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("simulated: cannot reconnect")

        monkeypatch.setattr(socket, "_connect", always_fail)

        with stamina.set_testing(True, attempts=100, cap=True):
            await fake_async_ws_server.disconnect()
            with pytest.raises(exceptions.SubscriptionLostError) as exc_info:
                await anext(sub)
        assert attempts == _RECONNECT_ATTEMPTS
        assert exc_info.value.reason == exceptions.SubscriptionLostReason.RECONNECT_EXHAUSTED


# ── Bulk tools: list / refresh_all / close_all ─────────────────────────────────


class TestListSubscriptions:
    async def test_shape_and_staleness(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}, refresh_timeout=45
        )
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
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

    async def test_empty_before_any_subscribe(self, async_ws_session: AsyncApicSession) -> None:
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is None  # the socket itself is lazy — nothing to list yet


class TestRefreshAllSubscriptions:
    async def test_does_not_feed_escalation_on_failure(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.consecutive_refresh_failures = 1

        httpx_mock.add_response(method="GET", status_code=400, json=_REFRESH_REJECTED)
        infos = await socket.refresh_all_subscriptions()

        assert infos[0].consecutive_refresh_failures == 1
        assert infos[0].is_stale is True

    async def test_success_resets_counter_and_reschedules(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.consecutive_refresh_failures = 1
        stale_schedule = reg.next_refresh_at

        httpx_mock.add_response(method="GET", json=ok())
        infos = await socket.refresh_all_subscriptions()

        assert infos[0].consecutive_refresh_failures == 0
        assert reg.next_refresh_at > stale_schedule


class TestCloseAllSubscriptions:
    async def test_stops_every_subscription_but_keeps_the_socket_alive(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        sub_a = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        sub_b = await async_ws_session.subscribe(
            "/api/class/fvBD.json", {"query-target": "subtree"}
        )
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        await socket.close_all_subscriptions()

        with pytest.raises(StopAsyncIteration):
            await anext(sub_a)
        with pytest.raises(StopAsyncIteration):
            await anext(sub_b)
        assert socket.list_subscriptions() == []
        assert socket._ws is not None  # type: ignore[reportPrivateUsage]
        assert fake_async_ws_server.connection_count == 1

        httpx_mock.add_response(method="GET", json=subscribe_response("3003"))
        sub_c = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        assert fake_async_ws_server.connection_count == 1

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["3003"],
                "imdata": [{"fvBD": {"attributes": {"status": "created"}}}],
            }
        )
        event = await anext(sub_c)
        assert isinstance(event, RawSubscriptionEvent)

    async def test_is_idempotent(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None

        await socket.close_all_subscriptions()
        await socket.close_all_subscriptions()  # must not raise

        assert socket.list_subscriptions() == []

    async def test_does_not_resurrect_a_registration_the_sweep_had_marked_due(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]
        reg.next_refresh_at = time.monotonic() - 1

        await socket.close_all_subscriptions()

        with pytest.raises(StopAsyncIteration):
            await anext(sub)
        await asyncio.sleep(1.5)
        assert socket.list_subscriptions() == []


# ── Single-subscription primitives: .info / .refresh_now() ────────────────────


class TestSingleSubscriptionPrimitives:
    async def test_info_reflects_current_state(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        assert sub.info.subscription_id == "1001"
        assert sub.info.is_stale is False

    async def test_info_after_close_raises(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            _ = sub.info

    async def test_refresh_now_resets_the_counter_without_escalating(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        socket._registrations[1].consecutive_refresh_failures = 1  # type: ignore[reportPrivateUsage]

        httpx_mock.add_response(method="GET", json=ok())
        info = await sub.refresh_now()

        assert info.consecutive_refresh_failures == 0
        assert info.is_stale is False

    async def test_refresh_now_after_close_raises(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            await sub.refresh_now()
