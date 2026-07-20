"""Tests for ``AsyncNiwaki.subscriptions`` (:class:`~niwaki.facade.AsyncSubscriptionManager`).

Async mirror of ``test_subscription_manager.py`` — see that file's docstring.
"""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from niwaki.facade import AsyncNiwaki, AsyncSubscriptionManager
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload, subscribe_response


class TestSubscriptionsProperty:
    async def test_returns_a_manager_over_the_active_session(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            manager = aci.subscriptions
            assert isinstance(manager, AsyncSubscriptionManager)

    async def test_no_op_before_any_subscribe(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.subscriptions.list() == []
            assert await aci.subscriptions.refresh_all() == []
            await aci.subscriptions.close_all()  # must not raise


class TestSubscriptionsDelegation:
    async def test_list_refresh_all_close_all_delegate_to_the_session(
        self, async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock
    ) -> None:
        aci = AsyncNiwaki()
        aci._session = async_ws_session  # type: ignore[reportPrivateUsage]

        httpx_mock.add_response(method="GET", json=subscribe_response("1001"))
        await async_ws_session.subscribe("/api/class/fvBD.json", {})

        infos = aci.subscriptions.list()
        assert len(infos) == 1
        assert infos[0].subscription_id == "1001"

        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        refreshed = await aci.subscriptions.refresh_all()
        assert len(refreshed) == 1

        await aci.subscriptions.close_all()
        assert aci.subscriptions.list() == []
