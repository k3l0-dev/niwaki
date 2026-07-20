"""Tests for ``Niwaki.subscriptions`` (:class:`~niwaki.facade.SubscriptionManager`).

The manager is a thin wrapper over the session's own delegation methods
(already covered end-to-end in ``tests/transport/test_session.py`` and
``tests/transport/test_subscription_socket.py``) — these tests only prove the
facade wiring: the property reaches the right session, and the no-op case
before any subscription was ever opened.
"""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from niwaki.facade import Niwaki, SubscriptionManager
from niwaki.transport.session import ApicSession
from tests.conftest import subscribe_response


class TestSubscriptionsProperty:
    def test_returns_a_manager_over_the_active_session(self, aci: Niwaki) -> None:
        manager = aci.subscriptions
        assert isinstance(manager, SubscriptionManager)

    def test_no_op_before_any_subscribe(self, aci: Niwaki) -> None:
        assert aci.subscriptions.list() == []
        assert aci.subscriptions.refresh_all() == []
        aci.subscriptions.close_all()  # must not raise


class TestSubscriptionsDelegation:
    def test_list_refresh_all_close_all_delegate_to_the_session(
        self, ws_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        aci = Niwaki()
        aci._session = ws_session  # type: ignore[reportPrivateUsage]

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        ws_session.subscribe("/api/class/fvBD.json", {})

        infos = aci.subscriptions.list()
        assert len(infos) == 1
        assert infos[0].subscription_id == "1001"

        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        refreshed = aci.subscriptions.refresh_all()
        assert len(refreshed) == 1

        aci.subscriptions.close_all()
        assert aci.subscriptions.list() == []
