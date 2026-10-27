"""The ``_dispatch`` guard of both subscription sockets — one odd frame is never fatal.

``_parse_frame`` cannot raise and ``_offer`` only calls ``put_nowait`` on an
unbounded queue, so nothing in a socket's ``_dispatch`` is expected to fail.
The guard around it is defence in depth: should anything unexpected raise
while ONE frame is handled, that frame is skipped with the usual
malformed-frame warning (its length, never its content), and the reader keeps
going. Without the guard the exception would reach the reader's crash net,
which fails EVERY subscription with ``SubscriptionLostError(INTERNAL_ERROR)``.

Each test runs once per socket (``[sync]``/``[async]``) against the real
local WebSocket servers of ``tests/conftest.py``, with the shared helper made
to raise on one marked frame through ``monkeypatch`` on the socket's own
module (where ``_dispatch`` looks the helper up).

The file also pins where the buffer-full warning is logged: on the logger of
the socket's own module, for both flavours.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from niwaki.transport import _subscription_socket as sync_module
from niwaki.transport import _subscription_socket_async as async_module
from niwaki.transport._subscription_socket import (
    RawPushItem,
    RawSubscription,
    RawSubscriptionEvent,
    SubscriptionInfo,
    SubscriptionOverflow,
    SubscriptionSocket,
)
from niwaki.transport._subscription_socket_async import (
    AsyncRawSubscription,
    AsyncSubscriptionSocket,
)
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import FakeAsyncWsServer, FakeWsServer, _await_until, subscribe_response

PATH = "/api/class/fvBD.json"
MALFORMED = "malformed subscription push frame"

type Sub = RawSubscription | AsyncRawSubscription


def _frame(sub_ids: list[str], *names: str) -> dict[str, Any]:
    return {
        "subscriptionId": sub_ids,
        "imdata": [{"fvBD": {"attributes": {"dn": f"uni/tn-t/BD-{name}"}}} for name in names],
    }


def _dn(item: RawPushItem) -> str:
    assert isinstance(item, RawSubscriptionEvent), f"expected an event, got {item!r}"
    return item.attributes["dn"]


class _Twin(ABC):
    """One socket flavour behind a uniform, awaitable surface."""

    module: ModuleType

    @property
    @abstractmethod
    def socket(self) -> SubscriptionSocket | AsyncSubscriptionSocket: ...

    @abstractmethod
    async def subscribe(self, wire_id: str, *, max_pending: int | None = None) -> Sub: ...

    @abstractmethod
    async def send(self, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    async def pull(self, sub: Sub) -> RawPushItem: ...

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        return self.socket.list_subscriptions()


class _SyncTwin(_Twin):
    module = sync_module

    def __init__(self, session: ApicSession, server: FakeWsServer, mock: HTTPXMock) -> None:
        self.session, self.server, self.mock = session, server, mock

    @property
    def socket(self) -> SubscriptionSocket:
        socket = self.session._subscription_socket
        assert socket is not None
        return socket

    async def subscribe(self, wire_id: str, *, max_pending: int | None = None) -> Sub:
        self.mock.add_response(method="GET", json=subscribe_response(wire_id))
        if max_pending is None:
            sub = self.session.subscribe(PATH, {})
        else:
            sub = self.session.subscribe(PATH, {}, max_pending=max_pending)
        await _await_until(lambda: self.server.connection_count == 1, timeout=5.0)
        return sub

    async def send(self, payload: dict[str, Any]) -> None:
        self.server.send(payload)

    async def pull(self, sub: Sub) -> RawPushItem:
        assert isinstance(sub, RawSubscription)
        await _await_until(lambda: sub._queue.qsize() > 0, timeout=5.0)
        return next(sub)


class _AsyncTwin(_Twin):
    module = async_module

    def __init__(
        self, session: AsyncApicSession, server: FakeAsyncWsServer, mock: HTTPXMock
    ) -> None:
        self.session, self.server, self.mock = session, server, mock

    @property
    def socket(self) -> AsyncSubscriptionSocket:
        socket = self.session._subscription_socket
        assert socket is not None
        return socket

    async def subscribe(self, wire_id: str, *, max_pending: int | None = None) -> Sub:
        self.mock.add_response(method="GET", json=subscribe_response(wire_id))
        if max_pending is None:
            sub = await self.session.subscribe(PATH, {})
        else:
            sub = await self.session.subscribe(PATH, {}, max_pending=max_pending)
        await _await_until(lambda: self.server.connection_count == 1, timeout=5.0)
        return sub

    async def send(self, payload: dict[str, Any]) -> None:
        await self.server.send(payload)

    async def pull(self, sub: Sub) -> RawPushItem:
        assert isinstance(sub, AsyncRawSubscription)
        await _await_until(lambda: sub._queue.qsize() > 0, timeout=5.0)
        return await anext(sub)


@pytest.fixture(params=["sync", "async"])
def twin(request: pytest.FixtureRequest, httpx_mock: HTTPXMock) -> _Twin:
    """The socket under test — the sync and the async twin, in turn."""
    if request.param == "sync":
        return _SyncTwin(
            request.getfixturevalue("ws_session"),
            request.getfixturevalue("fake_ws_server"),
            httpx_mock,
        )
    return _AsyncTwin(
        request.getfixturevalue("async_ws_session"),
        request.getfixturevalue("fake_async_ws_server"),
        httpx_mock,
    )


def _raise_on_boom_frame(real: Callable[[str | bytes], object]) -> Callable[[str | bytes], object]:
    def parse(raw: str | bytes) -> object:
        text = raw if isinstance(raw, str) else raw.decode()
        if "BD-boom" in text:
            raise RuntimeError("simulated: unexpected failure while parsing")
        return real(raw)

    return parse


def _raise_on_boom_event(real: Callable[..., None]) -> Callable[..., None]:
    def offer(reg: object, event: RawSubscriptionEvent, *args: object) -> None:
        if event.attributes.get("dn", "").endswith("BD-boom"):
            raise RuntimeError("simulated: unexpected failure while enqueueing")
        real(reg, event, *args)

    return offer


def _malformed_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if MALFORMED in r.getMessage()]


@pytest.mark.parametrize(
    ("helper", "wrap"),
    [("_parse_frame", _raise_on_boom_frame), ("_offer", _raise_on_boom_event)],
    ids=["parse-raises", "offer-raises"],
)
async def test_an_unexpected_failure_skips_only_that_frame(
    twin: _Twin,
    helper: str,
    wrap: Callable[[Any], Any],  # Any: the two wrapped helpers have different signatures
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sub_a = await twin.subscribe("1001")
    sub_b = await twin.subscribe("2002")
    monkeypatch.setattr(twin.module, helper, wrap(getattr(twin.module, helper)))
    boom = _frame(["1001", "2002"], "boom")

    with caplog.at_level(logging.WARNING):
        await twin.send(boom)
        await twin.send(_frame(["1001", "2002"], "after"))
        # The boom frame delivered nothing; the next frame reaches both.
        assert _dn(await twin.pull(sub_a)) == "uni/tn-t/BD-after"
        assert _dn(await twin.pull(sub_b)) == "uni/tn-t/BD-after"

    warnings = _malformed_warnings(caplog)
    assert len(warnings) == 1
    assert str(len(json.dumps(boom))) in warnings[0]
    assert all("boom" not in r.getMessage() for r in caplog.records)  # length, never content
    # No subscription was failed: both still tracked, both still answer.
    assert sorted(info.local_id for info in twin.list_subscriptions()) == [1, 2]
    assert sub_a.info.subscription_id == "1001"
    assert sub_b.info.subscription_id == "2002"
    assert twin.socket._dead is False
    assert sub_a._queue.qsize() == 0
    assert sub_b._queue.qsize() == 0


async def test_the_buffer_full_warning_names_the_sockets_own_module(
    twin: _Twin, caplog: pytest.LogCaptureFixture
) -> None:
    sub = await twin.subscribe("1001", max_pending=1)

    with caplog.at_level(logging.WARNING):
        await twin.send(_frame(["1001"], "kept", "dropped"))
        assert _dn(await twin.pull(sub)) == "uni/tn-t/BD-kept"
        assert await twin.pull(sub) == SubscriptionOverflow(dropped_total=1)

    (record,) = [r for r in caplog.records if "subscription buffer full" in r.getMessage()]
    assert record.name == twin.module.__name__
