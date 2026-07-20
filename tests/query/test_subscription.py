"""
End-to-end tests for ``aci.query(...).subscribe()`` — the full typed stack.

Lot 1 (``tests/transport/test_subscription_socket.py``) already proves the
transport primitive against a real local ``websockets`` server; these tests
prove the layer built on top of it: query-accumulator-to-wire-param mapping,
and that a live push comes back as a typed
:class:`~niwaki.query._events.SubscriptionEvent`, not a raw transport item.
Reuses the same fake-server fixtures (``tests/conftest.py``).
"""

from __future__ import annotations

import time
from urllib.parse import parse_qsl

import pytest
from pytest_httpx import HTTPXMock

from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.query import EventKind, Query
from niwaki.query._events import SubscriptionEvent
from niwaki.transport.session import ApicSession
from tests.conftest import FakeWsServer, _wait_until, ok, subscribe_response


class TestSubscribeEndToEnd:
    def test_initial_snapshot_is_typed(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        snapshot = [{"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}}]
        httpx_mock.add_response(method="GET", json=subscribe_response("1001", snapshot))

        with Query(fvBD, ws_session).subscribe() as sub:
            assert len(sub.initial) == 1
            bd = sub.initial[0]
            assert isinstance(bd, fvBD)
            assert bd.name == "web"
            assert bd.arp_flooding is True

    def test_build_to_wire_param_mapping(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))

        with (
            Query(fvBD, ws_session)
            .under("uni/tn-prod")
            .where(name="web")
            .subscribe(refresh_timeout=45) as _sub
        ):
            pass

        request = httpx_mock.get_requests()[-1]
        assert request.url.path == "/api/mo/uni/tn-prod.json"
        query = dict(parse_qsl(request.url.query.decode()))
        assert query["query-target"] == "subtree"
        assert query["target-subtree-class"] == "fvBD"
        assert query["query-target-filter"] == 'eq(fvBD.name,"web")'
        assert query["subscription"] == "yes"
        assert query["refresh-timeout"] == "45"

    def test_created_modified_deleted_events_are_typed_through_the_stack(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        fake_ws_server.send(
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
        created = next(sub)
        assert isinstance(created, SubscriptionEvent)
        assert created.kind is EventKind.CREATED
        assert isinstance(created.mo, fvBD)
        assert created.mo.model_fields_set == {"name", "arp_flooding"}
        assert created.mo.arp_flooding is False

        fake_ws_server.send(
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
        modified = next(sub)
        assert modified.kind is EventKind.MODIFIED
        assert modified.mo is not None
        assert modified.mo.model_fields_set == {"arp_flooding"}
        assert modified.mo.arp_flooding is True
        assert modified.dn == "uni/tn-prod/BD-web"

        fake_ws_server.send(
            {
                "subscriptionId": ["1001"],
                "imdata": [
                    {"fvBD": {"attributes": {"dn": "uni/tn-prod/BD-web", "status": "deleted"}}}
                ],
            }
        )
        deleted = next(sub)
        assert deleted.kind is EventKind.DELETED
        assert deleted.mo is not None
        assert deleted.mo.model_fields_set == set()
        assert deleted.dn == "uni/tn-prod/BD-web"

        sub.close()

    def test_forced_disconnect_yields_a_typed_gap_event(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        httpx_mock.add_response(method="GET", json=subscribe_response("2002"))
        fake_ws_server.disconnect()

        event = next(sub)
        assert event.kind is EventKind.GAP
        assert event.mo is None
        assert event.subscription_ids == ()
        sub.close()

    def test_forced_refresh_rejection_yields_a_typed_refresh_failed_event(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
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
        event = next(sub)
        assert event.kind is EventKind.REFRESH_FAILED
        assert event.mo is None
        sub.close()

    def test_close_stops_iteration(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        sub.close()

        with pytest.raises(StopIteration):
            next(sub)

    def test_context_manager_closes_on_exit(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        with Query(fvBD, ws_session).subscribe() as sub:
            _wait_until(lambda: fake_ws_server.connection_count == 1)

        with pytest.raises(StopIteration):
            next(sub)

    def test_server_down_raises_subscription_lost(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        from niwaki import exceptions

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        fake_ws_server.server.shutdown()
        fake_ws_server.disconnect()

        with pytest.raises(exceptions.SubscriptionLostError):
            next(sub)


class TestSubscriptionInfoAndRefreshNow:
    def test_info_reflects_current_state(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        with Query(fvBD, ws_session).subscribe() as sub:
            _wait_until(lambda: fake_ws_server.connection_count == 1)

            assert sub.info.subscription_id == "1001"
            assert sub.info.is_stale is False

    def test_refresh_now_resets_the_counter_without_escalating(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        with Query(fvBD, ws_session).subscribe() as sub:
            _wait_until(lambda: fake_ws_server.connection_count == 1)
            socket = ws_session._subscription_socket  # type: ignore[reportPrivateUsage]
            assert socket is not None
            socket._registrations[1].consecutive_refresh_failures = 1  # type: ignore[reportPrivateUsage]

            httpx_mock.add_response(method="GET", json=ok())
            info = sub.refresh_now()

            assert info.consecutive_refresh_failures == 0
            assert info.is_stale is False

    def test_info_and_refresh_now_after_close_raise(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock, fake_ws_server: FakeWsServer
    ) -> None:
        from niwaki import exceptions

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        sub = Query(fvBD, ws_session).subscribe()
        _wait_until(lambda: fake_ws_server.connection_count == 1)

        sub.close()
        with pytest.raises(exceptions.SubscriptionError):
            _ = sub.info
        with pytest.raises(exceptions.SubscriptionError):
            sub.refresh_now()
