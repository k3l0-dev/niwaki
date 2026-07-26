"""Backpressure on the subscription queues (C8).

A subscription's buffer is bounded by ``max_pending``: past it, incoming
events are dropped — never blocking the shared reader or touching other
subscriptions — and exactly one ``SubscriptionOverflow`` marker per burst is
delivered in-stream. Control items (stop/gap/refresh markers) are exempt
from the bound. A consumer that vanishes without ``close()`` is reaped by a
finalizer so its registration cannot grow forever.
"""

from __future__ import annotations

import gc
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from niwaki.transport._subscription_socket import (
    RawSubscriptionEvent,
    SubscriptionOverflow,
)
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import (
    FakeAsyncWsServer,
    FakeWsServer,
    _await_until,
    _wait_until,
    subscribe_response,
)


def _frame(sub_id: str, count: int, start: int = 0) -> dict[str, Any]:
    """One push frame carrying *count* modified-events for *sub_id*."""
    imdata = [
        {"fvBD": {"attributes": {"dn": f"uni/tn-t/BD-{start + i}", "status": "modified"}}}
        for i in range(count)
    ]
    return {"subscriptionId": [sub_id], "imdata": imdata}


class TestOverflowSync:
    def test_burst_drops_newest_and_delivers_one_marker(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {}, max_pending=3)
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        fake_ws_server.send(_frame("1001", 6))
        _wait_until(lambda: reg.dropped_total == 3)

        # The buffer holds the three oldest events, then the single marker.
        for i in range(3):
            item = next(sub)
            assert isinstance(item, RawSubscriptionEvent)
            assert item.attributes["dn"].endswith(f"BD-{i}")
        marker = next(sub)
        assert isinstance(marker, SubscriptionOverflow)
        assert marker.dropped_total == 1  # enqueued on the burst's FIRST drop

        # Below the bound again: the stream resumes, no second marker.
        fake_ws_server.send(_frame("1001", 1, start=100))
        item = next(sub)
        assert isinstance(item, RawSubscriptionEvent)
        assert item.attributes["dn"].endswith("BD-100")

        info = sub.info
        assert info.dropped == 3
        assert info.pending == 0
        sub.close()

    def test_close_works_while_the_buffer_is_full(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        """Control items bypass the bound — a full buffer can never wedge close()."""
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {}, max_pending=2)
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        fake_ws_server.send(_frame("1001", 5))
        _wait_until(lambda: reg.dropped_total == 3)

        sub.close()  # must not block; the _STOP sentinel is exempt from the bound
        with pytest.raises(StopIteration):
            next(sub)

    def test_a_slow_subscription_does_not_disturb_its_sibling(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        slow = ws_session.subscribe("/api/class/fvBD.json", {}, max_pending=1)
        fast = ws_session.subscribe("/api/class/fvTenant.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        slow_reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        fake_ws_server.send(_frame("1001", 4))
        _wait_until(lambda: slow_reg.dropped_total == 3)
        fake_ws_server.send(_frame("2002", 1, start=50))

        item = next(fast)  # the sibling stream is untouched by the overflow
        assert isinstance(item, RawSubscriptionEvent)
        assert item.attributes["dn"].endswith("BD-50")
        slow.close()
        fast.close()


class TestVanishedConsumerSync:
    def test_dropped_subscription_is_reaped(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        assert 1 in socket._registrations  # type: ignore[reportPrivateUsage]

        del sub
        gc.collect()
        _wait_until(lambda: 1 not in socket._registrations)  # type: ignore[reportPrivateUsage]

    def test_clean_close_detaches_the_finalizer(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = ws_session.subscribe("/api/class/fvBD.json", {})
        _wait_until(lambda: fake_ws_server.connection_count == 1)
        finalizer = sub._finalizer  # type: ignore[reportPrivateUsage]
        sub.close()
        assert not finalizer.alive


class TestOverflowAsync:
    async def test_burst_drops_newest_and_delivers_one_marker(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {}, max_pending=3)
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        await fake_async_ws_server.send(_frame("1001", 6))
        await _await_until(lambda: reg.dropped_total == 3)

        for i in range(3):
            item = await anext(sub)
            assert isinstance(item, RawSubscriptionEvent)
            assert item.attributes["dn"].endswith(f"BD-{i}")
        marker = await anext(sub)
        assert isinstance(marker, SubscriptionOverflow)
        assert marker.dropped_total == 1

        info = sub.info
        assert info.dropped == 3
        assert info.pending == 0
        await sub.close()

    async def test_close_works_while_the_buffer_is_full(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {}, max_pending=2)
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        reg = socket._registrations[1]  # type: ignore[reportPrivateUsage]

        await fake_async_ws_server.send(_frame("1001", 5))
        await _await_until(lambda: reg.dropped_total == 3)

        await sub.close()
        with pytest.raises(StopAsyncIteration):
            await anext(sub)


class TestVanishedConsumerAsync:
    async def test_dropped_subscription_is_reaped(
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
        assert 1 in socket._registrations  # type: ignore[reportPrivateUsage]

        del sub
        gc.collect()
        # The reap hops onto the loop via call_soon_threadsafe — yield to it.
        await _await_until(lambda: 1 not in socket._registrations)  # type: ignore[reportPrivateUsage]

    async def test_clean_close_detaches_the_finalizer(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await async_ws_session.subscribe("/api/class/fvBD.json", {})
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)
        finalizer = sub._finalizer  # type: ignore[reportPrivateUsage]
        await sub.close()
        assert not finalizer.alive
