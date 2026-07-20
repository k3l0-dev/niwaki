"""
End-to-end tests for ``aci.query(...).subscribe()`` (async) — the full typed stack.

Async mirror of ``test_subscription.py``. The transport primitive is proven
against a real local ``websockets.asyncio.server`` in
``tests/transport/test_subscription_socket_async.py``; these tests prove the
layer built on top of it: query-accumulator-to-wire-param mapping, and that a
live push comes back as a typed :class:`~niwaki.query._events.SubscriptionEvent`,
not a raw transport item. Reuses the same async fake-server fixtures
(``tests/conftest.py``).
"""

from __future__ import annotations

import time
from urllib.parse import parse_qsl

import pytest
from pytest_httpx import HTTPXMock

from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.query import AsyncQuery, EventKind
from niwaki.query._events import SubscriptionEvent
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import FakeAsyncWsServer, _await_until, ok, subscribe_response


class TestAsyncSubscribeEndToEnd:
    async def test_initial_snapshot_is_typed(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
    ) -> None:
        snapshot = [{"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}}]
        httpx_mock.add_response(method="GET", json=subscribe_response("1001", snapshot))

        async with await AsyncQuery(fvBD, async_ws_session).subscribe() as sub:
            assert len(sub.initial) == 1
            bd = sub.initial[0]
            assert isinstance(bd, fvBD)
            assert bd.name == "web"
            assert bd.arp_flooding is True

    async def test_build_to_wire_param_mapping(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))

        query = AsyncQuery(fvBD, async_ws_session).under("uni/tn-prod").where(name="web")
        async with await query.subscribe(refresh_timeout=45):
            pass

        request = httpx_mock.get_requests()[-1]
        assert request.url.path == "/api/mo/uni/tn-prod.json"
        params = dict(parse_qsl(request.url.query.decode()))
        assert params["query-target"] == "subtree"
        assert params["target-subtree-class"] == "fvBD"
        assert params["query-target-filter"] == 'eq(fvBD.name,"web")'
        assert params["subscription"] == "yes"
        assert params["refresh-timeout"] == "45"

    async def test_created_modified_deleted_events_are_typed_through_the_stack(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [
                    {
                        "fvBD": {
                            "attributes": {
                                "name": "web",
                                "arpFlood": "no",
                                "status": "created",
                            }
                        }
                    }
                ],
            }
        )
        created = await anext(sub)
        assert isinstance(created, SubscriptionEvent)
        assert created.kind is EventKind.CREATED
        assert isinstance(created.mo, fvBD)
        assert created.mo.model_fields_set == {"name", "arp_flooding"}
        assert created.mo.arp_flooding is False

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [
                    {
                        "fvBD": {
                            "attributes": {
                                "dn": "uni/tn-prod/BD-web",
                                "arpFlood": "yes",
                                "status": "modified",
                            }
                        }
                    }
                ],
            }
        )
        modified = await anext(sub)
        assert modified.kind is EventKind.MODIFIED
        assert modified.mo is not None
        assert modified.mo.model_fields_set == {"arp_flooding"}
        assert modified.mo.arp_flooding is True
        assert modified.dn == "uni/tn-prod/BD-web"

        await fake_async_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [
                    {"fvBD": {"attributes": {"dn": "uni/tn-prod/BD-web", "status": "deleted"}}}
                ],
            }
        )
        deleted = await anext(sub)
        assert deleted.kind is EventKind.DELETED
        assert deleted.mo is not None
        assert deleted.mo.model_fields_set == set()
        assert deleted.dn == "uni/tn-prod/BD-web"

        await sub.close()

    async def test_forced_disconnect_yields_a_typed_gap_event(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        await fake_async_ws_server.disconnect()

        event = await anext(sub)
        assert event.kind is EventKind.GAP
        assert event.mo is None
        assert event.subscription_ids == ()
        await sub.close()

    async def test_forced_refresh_rejection_yields_a_typed_refresh_failed_event(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
        assert socket is not None
        socket._registrations[1].next_refresh_at = time.monotonic() - 1  # type: ignore[reportPrivateUsage]

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
        event = await anext(sub)
        assert event.kind is EventKind.REFRESH_FAILED
        assert event.mo is None
        await sub.close()

    async def test_close_stops_iteration(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await sub.close()

        with pytest.raises(StopAsyncIteration):
            await anext(sub)

    async def test_context_manager_closes_on_exit(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        async with await AsyncQuery(fvBD, async_ws_session).subscribe() as sub:
            await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        with pytest.raises(StopAsyncIteration):
            await anext(sub)

    async def test_server_down_raises_subscription_lost(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        from niwaki import exceptions

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        fake_async_ws_server.server.close()
        await fake_async_ws_server.server.wait_closed()
        await fake_async_ws_server.disconnect()

        with pytest.raises(exceptions.SubscriptionLostError):
            await anext(sub)


class TestSubscriptionInfoAndRefreshNow:
    async def test_info_reflects_current_state(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        async with await AsyncQuery(fvBD, async_ws_session).subscribe() as sub:
            await _await_until(lambda: fake_async_ws_server.connection_count == 1)

            assert sub.info.subscription_id == "1001"
            assert sub.info.is_stale is False

    async def test_refresh_now_resets_the_counter_without_escalating(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        async with await AsyncQuery(fvBD, async_ws_session).subscribe() as sub:
            await _await_until(lambda: fake_async_ws_server.connection_count == 1)
            socket = async_ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
            assert socket is not None
            socket._registrations[1].consecutive_refresh_failures = 1  # type: ignore[reportPrivateUsage]

            httpx_mock.add_response(method="GET", json=ok())
            info = await sub.refresh_now()

            assert info.consecutive_refresh_failures == 0
            assert info.is_stale is False

    async def test_info_and_refresh_now_after_close_raise(
        self,
        async_ws_session: AsyncApicSession,
        httpx_mock: HTTPXMock,
        fake_async_ws_server: FakeAsyncWsServer,
    ) -> None:
        from niwaki import exceptions

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = await AsyncQuery(fvBD, async_ws_session).subscribe()
        await _await_until(lambda: fake_async_ws_server.connection_count == 1)

        await sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            _ = sub.info
        with pytest.raises(exceptions.SubscriptionError):
            await sub.refresh_now()
