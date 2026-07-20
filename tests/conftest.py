"""
Global pytest configuration for niwaki.

- Loads credentials from ``.env`` (if present) before any test session starts.
- Disables stamina delays (immediate retries) to speed up unit tests.
- Provides real local WebSocket server test-doubles
  (:class:`FakeWsServer`/``fake_ws_server`` sync,
  :class:`FakeAsyncWsServer`/``fake_async_ws_server`` async) shared by the
  transport-layer and query-layer subscription test suites.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import stamina
import websockets.asyncio.server as ws_async_server
import websockets.sync.server as ws_server
from dotenv import load_dotenv
from pytest_httpx import HTTPXMock

from niwaki.facade import Niwaki
from niwaki.transport._config import RetryConfig
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession

# Load .env from the repository root so APIC_* variables are available to
# both unit tests (env fallback) and integration tests.
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Fixture loader ────────────────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """
    Load a JSON fixture file from ``tests/fixtures/``.

    Args:
        name: Fixture filename without extension (e.g. ``"auth_login"``).

    Returns:
        Parsed fixture as a Python dict (deep-copied so callers can mutate it).

    Raises:
        FileNotFoundError: If the fixture file does not exist.
    """
    path = _FIXTURES_DIR / f"{name}.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return copy.deepcopy(data)


# ── Stamina ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def disable_stamina_delays() -> None:
    """
    Enable stamina testing mode for all unit tests.

    In testing mode, stamina removes backoff delays between retry attempts
    (wait_initial, wait_max, wait_jitter are set to 0). Retries happen
    immediately, which avoids slowing down the test suite.

    Note:
        Integration tests marked ``integration`` may disable this fixture
        if they want to test real backoff behaviour.
    """
    stamina.set_testing(True)


# ── Shared APIC mock helpers ──────────────────────────────────────────────────

HOST = "https://apic.test"
LOGIN_URL = f"{HOST}/api/aaaLogin.json"


def login_payload(token: str = "tok", ttl: int = 600) -> dict[str, Any]:
    """aaaLogin response from the fixture with a controllable token and TTL."""
    data = load_fixture("auth_login")
    attrs: dict[str, Any] = data["imdata"][0]["aaaLogin"]["attributes"]
    attrs["token"] = token
    attrs["refreshTimeoutSeconds"] = str(ttl)
    return data


def ok() -> dict[str, Any]:
    """Empty successful APIC response."""
    return {"totalCount": "0", "imdata": []}


@pytest.fixture
def aci(httpx_mock: HTTPXMock) -> Niwaki:
    """Authenticated sync facade backed by pytest-httpx."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    return Niwaki.connect(HOST, "admin", "secret")


# ── Object-subscription test double ────────────────────────────────────────────
#
# REST calls (login, subscribe, refresh) are mocked via pytest-httpx like
# everything else; the WebSocket half needs a genuine test-double instead — a
# real local ``websockets`` server bound to an ephemeral port on 127.0.0.1, run
# in a background thread for the test's duration. A session's ``host`` points
# at that same address, so the mocked REST calls and the real WebSocket
# connection resolve to the same place — pytest-httpx intercepts the httpx
# traffic regardless of host; the WebSocket connection is a different library
# entirely and genuinely dials 127.0.0.1. Shared by the transport-layer
# (tests/transport/test_subscription_socket.py) and query-layer
# (tests/query/test_subscription.py) subscription suites.


@dataclass
class FakeWsServer:
    server: ws_server.Server
    port: int
    connections: list[ws_server.ServerConnection] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON frame over the most recently accepted connection."""
        with self.lock:
            conn = self.connections[-1]
        conn.send(json.dumps(payload))

    def disconnect(self) -> None:
        """Forcibly close the most recently accepted connection (simulates a drop)."""
        with self.lock:
            conn = self.connections[-1]
        conn.close()

    @property
    def connection_count(self) -> int:
        with self.lock:
            return len(self.connections)


@pytest.fixture
def fake_ws_server() -> Iterator[FakeWsServer]:
    connections: list[ws_server.ServerConnection] = []
    lock = threading.Lock()

    def handler(conn: ws_server.ServerConnection) -> None:
        with lock:
            connections.append(conn)
        with contextlib.suppress(Exception):
            for _ in conn:  # server never expects client->server messages
                pass

    server = ws_server.serve(handler, host="127.0.0.1", port=0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FakeWsServer(server=server, port=port, connections=connections, lock=lock)
    finally:
        server.shutdown()


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    """Poll ``predicate()`` until true or *timeout* elapses (thread-timing tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def ws_session(fake_ws_server: FakeWsServer, httpx_mock: HTTPXMock) -> Iterator[ApicSession]:
    """Authenticated session whose host resolves to the fake WebSocket server."""
    host = f"http://127.0.0.1:{fake_ws_server.port}"
    httpx_mock.add_response(method="POST", json=login_payload())
    s = ApicSession(host=host, username="admin", password="secret", retry=RetryConfig(attempts=2))
    s.login()
    yield s
    s.close()


def subscribe_response(
    sub_id: str = "1001", imdata: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A realistic ``subscription=yes`` GET response: subscriptionId + one page."""
    return {"subscriptionId": sub_id, "totalCount": "0", "imdata": imdata or []}


# ── Async object-subscription test double ──────────────────────────────────────
#
# Async mirror of the fixtures above — a real local
# ``websockets.asyncio.server``, run on the test's own event loop (no thread
# needed: unlike the sync server, everything here is cooperative). Shared by
# ``tests/transport/test_subscription_socket_async.py`` and
# ``tests/query/test_async_subscription.py``.


@dataclass
class FakeAsyncWsServer:
    server: ws_async_server.Server
    port: int
    connections: list[ws_async_server.ServerConnection] = field(default_factory=list)

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON frame over the most recently accepted connection."""
        await self.connections[-1].send(json.dumps(payload))

    async def disconnect(self) -> None:
        """Forcibly close the most recently accepted connection (simulates a drop)."""
        await self.connections[-1].close()

    @property
    def connection_count(self) -> int:
        return len(self.connections)


@pytest.fixture
async def fake_async_ws_server() -> AsyncIterator[FakeAsyncWsServer]:
    connections: list[ws_async_server.ServerConnection] = []

    async def handler(conn: ws_async_server.ServerConnection) -> None:
        connections.append(conn)
        with contextlib.suppress(Exception):
            async for _ in conn:  # server never expects client->server messages
                pass

    server = await ws_async_server.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield FakeAsyncWsServer(server=server, port=port, connections=connections)
    finally:
        server.close()
        await server.wait_closed()


async def _await_until(predicate: Any, *, timeout: float = 2.0) -> None:
    """Poll ``predicate()`` until true or *timeout* elapses (async analogue of ``_wait_until``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
async def async_ws_session(
    fake_async_ws_server: FakeAsyncWsServer, httpx_mock: HTTPXMock
) -> AsyncIterator[AsyncApicSession]:
    """Authenticated async session whose host resolves to the fake WebSocket server."""
    host = f"http://127.0.0.1:{fake_async_ws_server.port}"
    httpx_mock.add_response(method="POST", json=login_payload())
    s = AsyncApicSession(
        host=host, username="admin", password="secret", retry=RetryConfig(attempts=2)
    )
    await s.login()
    yield s
    await s.close()
