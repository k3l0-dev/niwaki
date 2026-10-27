"""Behavioural equivalence matrix for the two APIC sessions.

``ApicSession`` and ``AsyncApicSession`` are twins: the same transport contract,
written twice.  Their own test files grew apart — fewer than one test name in
four is shared — so a fix landing on one twin only stayed invisible.  Every
divergence found so far had exactly that shape.

Each scenario here is written **once** and runs against **both** twins, through
:func:`maybe`: a sync call returns its value, an async call returns an
awaitable, and the scenario never knows which.  Both clients are mocked by
``pytest-httpx``, which also fails a test on any unexpected or unconsumed
request — so every request sequence asserted below is exact.

What is deliberately *not* shared is listed where it happens: the async
semaphore (``max_concurrent``) exists on one side by design, and concurrency
itself is driven with threads on one side and coroutines on the other.

Sleeps are observed, never slept: ``time.sleep`` and ``asyncio.sleep`` are
replaced by recorders, and stamina runs with its real backoff, so the full
sleep sequence — controller-requested delays and stamina's own backoff, and
the channel each went through — is part of the contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import ssl
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

import httpx
import pytest
import stamina
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.transport import session as sync_session_module
from niwaki.transport import session_async as async_session_module
from niwaki.transport._config import RetryConfig
from niwaki.transport._token import TokenState
from niwaki.transport._token_cache import TokenCache
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, load_fixture, login_payload, ok

# ── The two twins ─────────────────────────────────────────────────────────────

type Session = ApicSession | AsyncApicSession
type Kind = Literal["sync", "async"]
type Op = Literal["GET", "POST", "DELETE"]
type Sleeps = list[tuple[str, float]]

USER = "admin"
PASSWORD = "secret"  # pragma: allowlist secret — documentation placeholder
CERT_DN = "uni/userext/user-admin/usercert-twin"

REFRESH_URL = f"{HOST}/api/aaaRefresh.json"
TENANTS = "/api/class/fvTenant.json"
TENANTS_URL = f"{HOST}{TENANTS}"
BDS = "/api/class/fvBD.json"
MO_DN = "uni/tn-twin"
MO_URL = f"{HOST}/api/mo/{MO_DN}.json"
PAYLOAD: dict[str, Any] = {"fvTenant": {"attributes": {"name": "twin"}}}

# Real stamina backoff, no jitter: attempt 1 waits 0.5 s, attempt 2 waits 1.0 s.
RETRY = RetryConfig(attempts=3, wait_initial=0.5, wait_max=5.0, wait_jitter=0.0)
BACKOFF_1, BACKOFF_2 = 0.5, 1.0


@dataclass(frozen=True)
class Twin:
    """One side of the matrix: which session class, module and sleep channel."""

    kind: Kind

    @property
    def module(self) -> ModuleType:
        """The session's own module — where its module-level constants live."""
        return sync_session_module if self.kind == "sync" else async_session_module

    @property
    def sleep_channel(self) -> str:
        """The only sleep this twin may use: blocking for sync, awaited for async.

        An async session calling ``time.sleep`` would freeze its event loop, so
        the channel is part of the contract, not a detail.
        """
        return "time" if self.kind == "sync" else "asyncio"

    def slept(self, *delays: float) -> Sleeps:
        """The recorded sleep sequence expected from this twin."""
        return [(self.sleep_channel, delay) for delay in delays]

    def build(self, password: str | None = PASSWORD, **kwargs: Any) -> Session:
        """Construct this twin's session against the mocked host."""
        if self.kind == "sync":
            return ApicSession(HOST, USER, password, **kwargs)
        return AsyncApicSession(HOST, USER, password, **kwargs)


SYNC = Twin("sync")
ASYNC = Twin("async")


@pytest.fixture(params=[SYNC, ASYNC], ids=["sync", "async"])
def twin(request: pytest.FixtureRequest) -> Twin:
    """Every test that asks for a twin runs once per side."""
    return cast(Twin, request.param)


async def maybe[T](value: Awaitable[T] | T) -> T:
    """Resolve a twin's return value whichever twin produced it."""
    if inspect.isawaitable(value):
        return cast(T, await value)
    return cast(T, value)


@pytest.fixture
async def make(twin: Twin) -> AsyncIterator[Callable[..., Session]]:
    """Session factory for the current twin; closes every session it built."""
    built: list[Session] = []

    def factory(**kwargs: Any) -> Session:
        session = twin.build(**kwargs)
        built.append(session)
        return session

    yield factory
    for session in built:
        await maybe(session.close())


async def logged_in(
    make: Callable[..., Session],
    httpx_mock: HTTPXMock,
    *,
    token: str = "t1",
    ttl: int = 600,
    **kwargs: Any,
) -> Session:
    """A session that has completed one mocked password login."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload(token, ttl))
    session = make(**kwargs)
    await maybe(session.login())
    return session


async def perform(session: Session, op: Op) -> object:
    """Issue one public request of kind *op* through either twin."""
    match op:
        case "GET":
            return await maybe(session.get(TENANTS))
        case "POST":
            return await maybe(session.post_mo(MO_DN, PAYLOAD))
        case "DELETE":
            return await maybe(session.delete_mo(MO_DN))


def op_url(op: Op) -> str:
    """The URL a request of kind *op* is sent to."""
    return TENANTS_URL if op == "GET" else MO_URL


async def all_pages(
    session: Session, path: str, params: dict[str, Any], page_size: int = 500
) -> list[list[dict[str, Any]]]:
    """Drain the page iterator — ``_iter_pages`` (sync) or ``_aiter_pages`` (async)."""
    if isinstance(session, AsyncApicSession):
        return [page async for page in session._aiter_pages(path, params, page_size=page_size)]
    return list(session._iter_pages(path, params, page_size=page_size))


async def get_all_pages(
    session: Session, path: str, params: dict[str, Any], page_size: int = 500
) -> list[dict[str, Any]]:
    """``_get_all_pages`` through either twin, with its precise return type."""
    if isinstance(session, AsyncApicSession):
        return await session._get_all_pages(path, params, page_size=page_size)
    return session._get_all_pages(path, params, page_size=page_size)


def token_of(session: Session) -> str | None:
    """The live token, or ``None`` when the session holds none."""
    state = session._token_state
    return state.token if state is not None else None


def cookie_of(request: httpx.Request) -> str:
    return request.headers.get("cookie", "")


def trail(httpx_mock: HTTPXMock) -> list[str]:
    """Every request sent so far, as ``"METHOD /path"``."""
    return [f"{r.method} {r.url.path}" for r in httpx_mock.get_requests()]


def refresh_payload(token: str, ttl: int = 600, version: str | None = "6.0(9c)") -> dict[str, Any]:
    """An aaaRefresh reply with a controllable token, TTL and version."""
    data = load_fixture("auth_refresh")
    attrs: dict[str, Any] = data["imdata"][0]["aaaRefresh"]["attributes"]
    attrs["token"] = token
    attrs["refreshTimeoutSeconds"] = str(ttl)
    if version is None:
        del attrs["version"]
    else:
        attrs["version"] = version
    return data


def error_payload(code: str = "401", text: str = "Unauthorized") -> dict[str, Any]:
    return {"imdata": [{"error": {"attributes": {"code": code, "text": text}}}]}


def bd(name: str) -> dict[str, Any]:
    return {"fvBD": {"attributes": {"dn": f"uni/tn-x/BD-{name}", "name": name}}}


def names(items: list[dict[str, Any]]) -> list[str]:
    return [item["fvBD"]["attributes"]["name"] for item in items]


def near_expiry(token: str) -> TokenState:
    """A token inside the 60 s refresh window but not yet expired."""
    return TokenState(
        token=token,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        refresh_threshold=timedelta(seconds=60),
    )


def expired(token: str) -> TokenState:
    return TokenState(token=token, expires_at=datetime.now(UTC) - timedelta(seconds=1))


def tls_failure() -> httpx.ConnectError:
    """A connect error caused by certificate verification, as httpx raises it."""
    exc = httpx.ConnectError("handshake failed")
    exc.__cause__ = ssl.SSLError("certificate verify failed")
    return exc


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sleeps]:
    """Record every sleep instead of sleeping, with stamina's real backoff on.

    The suite-wide fixture puts stamina in testing mode (one attempt, no
    backoff), which would make a retry test vacuous.  Here stamina retries for
    real, and both its backoff and the session's own ``Retry-After`` sleep land
    in the record — tenacity sleeps through ``time.sleep``, stamina's async
    path through ``asyncio.sleep``, and so do the two sessions.
    """
    record: Sleeps = []

    def fake_time_sleep(seconds: float) -> None:
        record.append(("time", float(seconds)))

    async def fake_asyncio_sleep(delay: float, result: object = None) -> object:
        record.append(("asyncio", float(delay)))
        return result

    monkeypatch.setattr(time, "sleep", fake_time_sleep)
    monkeypatch.setattr(asyncio, "sleep", fake_asyncio_sleep)
    stamina.set_testing(False)
    try:
        yield record
    finally:
        stamina.set_testing(True)


# ── Login ─────────────────────────────────────────────────────────────────────


class TestPasswordLogin:
    async def test_it_trades_the_password_for_a_token_and_a_cookie(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            json=login_payload("t1"),
            headers={"set-cookie": "APIC-cookie=t1; path=/; HttpOnly; Secure"},
        )
        session = make()

        await maybe(session.login())

        assert session.is_authenticated
        assert token_of(session) == "t1"
        (request,) = httpx_mock.get_requests()
        assert json.loads(request.content) == {
            "aaaUser": {"attributes": {"name": USER, "pwd": PASSWORD}}
        }
        jar = session._client.cookies
        assert jar.get("APIC-cookie") == "t1"
        assert len([c for c in jar.jar if c.name == "APIC-cookie"]) == 1

    async def test_a_rejection_is_a_login_error_carrying_the_apic_text(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST", url=LOGIN_URL, status_code=401, json=error_payload(text="bad creds")
        )
        session = make()

        with pytest.raises(exceptions.LoginError, match=r"HTTP 401.*bad creds"):
            await maybe(session.login())

        assert not session.is_authenticated

    @pytest.mark.parametrize(
        "body", [{"imdata": []}, {"wrong": "structure"}], ids=["empty-imdata", "no-imdata"]
    )
    async def test_a_malformed_reply_is_a_login_error(
        self, body: dict[str, Any], make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=body)

        with pytest.raises(exceptions.LoginError, match="Unexpected APIC response"):
            await maybe(make().login())

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            pytest.param(
                lambda: httpx.ConnectError("refused"), exceptions.ConnectionError, id="connect"
            ),
            pytest.param(lambda: httpx.ReadTimeout("slow"), exceptions.TimeoutError, id="timeout"),
            pytest.param(tls_failure, exceptions.TLSError, id="tls"),
            pytest.param(
                lambda: httpx.RemoteProtocolError("reset"), exceptions.TransportError, id="other"
            ),
        ],
    )
    async def test_a_transport_failure_is_typed_and_its_chain_severed(
        self,
        failure: Callable[[], httpx.TransportError],
        expected: type[exceptions.TransportError],
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
    ) -> None:
        """The httpx error holds the request, whose body holds the password."""
        raised = failure()
        httpx_mock.add_exception(raised, url=LOGIN_URL)

        with pytest.raises(exceptions.TransportError) as excinfo:
            await maybe(make().login())

        assert type(excinfo.value) is expected
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None
        assert type(raised).__name__ in str(excinfo.value)


@pytest.fixture(scope="module")
def key_pem(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway RSA key on disk — skips when the ``x509`` extra is missing."""
    pytest.importorskip("cryptography", reason="needs niwaki[x509]")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("twins-x509") / "admin.key"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


class TestSignedCertificateLogin:
    async def test_login_sends_nothing_and_every_request_is_signed(
        self, key_pem: Path, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())
        httpx_mock.add_response(method="GET", url=f"{HOST}{BDS}", json=ok())
        session = make(password=None, private_key=key_pem, cert_dn=CERT_DN)

        await maybe(session.login())
        await maybe(session.get(TENANTS))
        await maybe(session.get(BDS))

        assert token_of(session) is None
        assert session.apic_version is None
        assert trail(httpx_mock) == [f"GET {TENANTS}", f"GET {BDS}"]
        first, second = (cookie_of(r) for r in httpx_mock.get_requests())
        for jar in (first, second):
            assert "APIC-Request-Signature=" in jar
            assert f"APIC-Certificate-DN={CERT_DN}" in jar
            assert "APIC-cookie" not in jar
        assert first != second, "two different paths produced one signature"

    async def test_a_mid_session_401_replays_once_without_any_login(
        self, key_pem: Path, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """There is no token to renew: the recovery login is a no-op, the replay is not."""
        httpx_mock.add_response(method="GET", url=TENANTS_URL, status_code=401, json=ok())
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())
        session = make(password=None, private_key=key_pem, cert_dn=CERT_DN)

        assert await maybe(session.get(TENANTS)) == []
        assert trail(httpx_mock) == [f"GET {TENANTS}", f"GET {TENANTS}"]


class RecordingCache(TokenCache):
    """A real on-disk cache that also records which of its methods ran."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.log: list[str] = []

    def read(self, host: str, username: str) -> tuple[str, datetime] | None:
        self.log.append("read")
        return super().read(host, username)

    def write(self, host: str, username: str, token: str, expires_at: datetime) -> None:
        self.log.append(f"write:{token}")
        super().write(host, username, token, expires_at)

    def clear(self, host: str, username: str) -> None:
        self.log.append("clear")
        super().clear(host, username)


@pytest.fixture
def cache(tmp_path: Path) -> RecordingCache:
    return RecordingCache(tmp_path / "tokens")


class TestTokenCacheLogin:
    async def test_a_cached_token_skips_the_login_request(
        self, cache: RecordingCache, make: Callable[..., Session]
    ) -> None:
        cache.write(HOST, USER, "tok-cached", datetime.now(UTC) + timedelta(seconds=600))
        session = make(token_cache=cache)

        await maybe(session.login())  # no mock registered: a request would fail

        assert token_of(session) == "tok-cached"
        assert session._client.cookies.get("APIC-cookie") == "tok-cached"

    async def test_a_real_login_fills_the_cache(
        self, cache: RecordingCache, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock, token="t1", token_cache=cache)

        assert cache.log == ["read", "write:t1"]
        entry = cache.read(HOST, USER)
        assert entry is not None and entry[0] == token_of(session) == "t1"

    async def test_use_cache_false_reaches_the_controller(
        self, cache: RecordingCache, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        cache.write(HOST, USER, "tok-cached", datetime.now(UTC) + timedelta(seconds=600))
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t-fresh"))
        session = make(token_cache=cache)

        await maybe(session.login(use_cache=False))

        assert token_of(session) == "t-fresh"
        assert trail(httpx_mock) == ["POST /api/aaaLogin.json"]

    @pytest.mark.parametrize("path", ["mid-session-401", "expired", "refresh-rejected"])
    async def test_every_recovery_bypasses_and_purges_the_cache(
        self,
        path: str,
        cache: RecordingCache,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
    ) -> None:
        """A recovery served from the cache would answer a rejection with the rejected token.

        Both halves are observed directly: no ``read`` (bypass) and one
        ``clear`` (purge) between the first login and the recovery's ``write``.
        """
        cache.write(HOST, USER, "tok-cached", datetime.now(UTC) + timedelta(seconds=600))
        session = make(token_cache=cache)
        await maybe(session.login())
        cache.log.clear()

        match path:
            case "mid-session-401":
                httpx_mock.add_response(method="GET", url=TENANTS_URL, status_code=401, json=ok())
            case "expired":
                session._token_state = expired("tok-cached")
            case _:
                session._token_state = near_expiry("tok-cached")
                httpx_mock.add_response(
                    method="GET", url=REFRESH_URL, status_code=401, json=error_payload()
                )
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t-recovered"))
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert cache.log == ["clear", "write:t-recovered"]
        entry = cache.read(HOST, USER)
        assert entry is not None and entry[0] == "t-recovered"
        assert "APIC-cookie=t-recovered" in cookie_of(httpx_mock.get_requests()[-1])


# ── Token lifecycle ───────────────────────────────────────────────────────────


class TestTokenLifecycle:
    async def test_a_token_far_from_expiry_is_used_as_is(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock, ttl=600)
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert trail(httpx_mock) == ["POST /api/aaaLogin.json", f"GET {TENANTS}"]

    async def test_a_token_inside_the_threshold_is_refreshed_first(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock, ttl=30, refresh_threshold=60)
        httpx_mock.add_response(method="GET", url=REFRESH_URL, json=refresh_payload("t2"))
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert trail(httpx_mock) == [
            "POST /api/aaaLogin.json",
            "GET /api/aaaRefresh.json",
            f"GET {TENANTS}",
        ]
        assert token_of(session) == "t2"
        assert "APIC-cookie=t2" in cookie_of(httpx_mock.get_requests()[-1])

    async def test_an_expired_token_relogs_without_trying_a_refresh(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        session._token_state = expired("t1")
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t2"))
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert trail(httpx_mock) == [
            "POST /api/aaaLogin.json",
            "POST /api/aaaLogin.json",
            f"GET {TENANTS}",
        ]
        assert token_of(session) == "t2"

    @pytest.mark.parametrize("status", [401, 500])
    async def test_a_rejected_refresh_falls_back_to_a_relogin(
        self, status: int, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        session._token_state = near_expiry("t1")
        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, status_code=status, json=error_payload()
        )
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t3"))
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert trail(httpx_mock) == [
            "POST /api/aaaLogin.json",
            "GET /api/aaaRefresh.json",
            "POST /api/aaaLogin.json",
            f"GET {TENANTS}",
        ]
        assert token_of(session) == "t3"

    async def test_a_rejected_relogin_on_expiry_is_session_expired(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        session._token_state = expired("t1")
        httpx_mock.add_response(method="POST", url=LOGIN_URL, status_code=401, json=error_payload())

        with pytest.raises(exceptions.SessionExpiredError, match="token expired") as excinfo:
            await maybe(session.get(TENANTS))

        assert isinstance(excinfo.value.__cause__, exceptions.LoginError)
        assert trail(httpx_mock) == ["POST /api/aaaLogin.json", "POST /api/aaaLogin.json"]

    async def test_a_rejected_refresh_then_relogin_is_session_expired(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        session._token_state = near_expiry("t1")
        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, status_code=401, json=error_payload()
        )
        httpx_mock.add_response(method="POST", url=LOGIN_URL, status_code=401, json=error_payload())

        with pytest.raises(exceptions.SessionExpiredError, match="refresh failed"):
            await maybe(session.get(TENANTS))

    async def test_an_unreachable_refresh_is_a_transport_error_not_a_relogin(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        session._token_state = near_expiry("t1")
        httpx_mock.add_exception(httpx.ConnectError("refused"), url=REFRESH_URL)

        with pytest.raises(exceptions.ConnectionError):
            await maybe(session.get(TENANTS))

        assert trail(httpx_mock) == ["POST /api/aaaLogin.json", "GET /api/aaaRefresh.json"]

    @pytest.mark.parametrize("op", ["GET", "POST", "DELETE"])
    async def test_no_login_is_an_auth_error_before_any_request(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        with pytest.raises(exceptions.AuthError, match="Not authenticated"):
            await perform(make(), op)

        assert httpx_mock.get_requests() == []

    async def test_refresh_without_login_is_an_auth_error(
        self, make: Callable[..., Session]
    ) -> None:
        with pytest.raises(exceptions.AuthError, match="Call login"):
            await maybe(make()._refresh_token())


# ── Mid-session 401 ───────────────────────────────────────────────────────────

OPS = ["GET", "POST", "DELETE"]


class TestMidSession401:
    @pytest.mark.parametrize("op", OPS)
    async def test_it_relogs_and_replays_exactly_once(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock, token="t1")
        httpx_mock.add_response(method=op, url=op_url(op), status_code=401, json=error_payload())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t2"))
        httpx_mock.add_response(method=op, url=op_url(op), json=ok())

        await perform(session, op)

        path = httpx.URL(op_url(op)).path
        assert trail(httpx_mock) == [
            "POST /api/aaaLogin.json",
            f"{op} {path}",
            "POST /api/aaaLogin.json",
            f"{op} {path}",
        ]
        _, first, _, replay = httpx_mock.get_requests()
        assert "APIC-cookie=t1" in cookie_of(first)
        assert "APIC-cookie=t2" in cookie_of(replay)
        assert replay.content == first.content
        assert token_of(session) == "t2"

    @pytest.mark.parametrize("op", OPS)
    async def test_a_persistent_401_is_an_unauthorized_error(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method=op, url=op_url(op), status_code=401, json=error_payload())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t2"))
        httpx_mock.add_response(
            method=op, url=op_url(op), status_code=401, json=error_payload(text="still no")
        )

        with pytest.raises(exceptions.UnauthorizedError, match="still no") as excinfo:
            await perform(session, op)

        assert excinfo.value.status_code == 401
        assert len(httpx_mock.get_requests()) == 4  # login, request, relogin, one replay

    @pytest.mark.parametrize("op", OPS)
    async def test_a_rejected_relogin_is_session_expired_and_nothing_is_replayed(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method=op, url=op_url(op), status_code=401, json=error_payload())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, status_code=401, json=error_payload())

        with pytest.raises(exceptions.SessionExpiredError, match="mid-session 401"):
            await perform(session, op)

        assert len(httpx_mock.get_requests()) == 3

    @pytest.mark.parametrize("op", OPS)
    async def test_the_replay_is_one_raw_attempt_not_a_retry_loop(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        """A retryable status on the replay surfaces at once, even with retries on."""
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(method=op, url=op_url(op), status_code=401, json=error_payload())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t2"))
        httpx_mock.add_response(method=op, url=op_url(op), status_code=503, json=error_payload())

        with pytest.raises(exceptions.ServerError) as excinfo:
            await perform(session, op)

        assert excinfo.value.status_code == 503
        assert len(httpx_mock.get_requests()) == 4
        assert sleeps == []

    @pytest.mark.parametrize(
        "twin",
        [pytest.param(SYNC, id="sync"), pytest.param(ASYNC, id="async")],
    )
    @pytest.mark.parametrize("op", OPS)
    async def test_a_401_after_another_caller_renewed_reuses_their_token(
        self, op: Op, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """The deterministic half of the stampede guard: compare to the stale token.

        While this request is in flight with ``t1``, another caller renews the
        session to ``t-peer`` (simulated inside the controller's answer).  The
        401 that comes back is about ``t1`` — already superseded — so the
        session must replay with ``t-peer`` instead of logging in again.
        """
        logins = 0
        session = make()

        def controller(request: httpx.Request) -> httpx.Response:
            nonlocal logins
            if request.url.path == "/api/aaaLogin.json":
                logins += 1
                return httpx.Response(200, json=login_payload(f"t{logins}"))
            if "APIC-cookie=t1" in cookie_of(request):
                session._token_state = TokenState(
                    token="t-peer", expires_at=datetime.now(UTC) + timedelta(seconds=600)
                )
                session._client.cookies.set("APIC-cookie", "t-peer", domain="apic.test")
                return httpx.Response(401, json=error_payload())
            return httpx.Response(200, json=ok())

        httpx_mock.add_callback(controller, is_reusable=True)
        await maybe(session.login())

        await perform(session, op)

        assert logins == 1  # the initial login only — the peer's token was reused
        assert token_of(session) == "t-peer"
        assert "APIC-cookie=t-peer" in cookie_of(httpx_mock.get_requests()[-1])


# ── Retryable statuses ────────────────────────────────────────────────────────


class TestRetryableStatuses:
    @pytest.mark.parametrize("status", [502, 503, 504])
    async def test_a_read_retries_every_busy_status(
        self,
        status: int,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(method="GET", url=TENANTS_URL, status_code=status)
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        assert await maybe(session.get(TENANTS)) == []

        assert trail(httpx_mock).count(f"GET {TENANTS}") == 2
        assert sleeps == twin.slept(BACKOFF_1)

    async def test_a_read_does_not_retry_a_500(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(method="GET", url=TENANTS_URL, status_code=500)

        with pytest.raises(exceptions.ServerError):
            await maybe(session.get(TENANTS))

        assert trail(httpx_mock).count(f"GET {TENANTS}") == 1
        assert sleeps == []

    @pytest.mark.parametrize("op", ["POST", "DELETE"])
    @pytest.mark.parametrize(
        ("status", "retried"), [(503, True), (502, False), (504, False)], ids=["503", "502", "504"]
    )
    async def test_a_write_retries_only_the_status_proving_nothing_happened(
        self,
        op: Op,
        status: int,
        retried: bool,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(method=op, url=MO_URL, status_code=status)
        if retried:
            httpx_mock.add_response(method=op, url=MO_URL, json=ok())
            await perform(session, op)
        else:
            with pytest.raises(exceptions.ServerError) as excinfo:
                await perform(session, op)
            assert excinfo.value.status_code == status

        assert trail(httpx_mock).count(f"{op} /api/mo/{MO_DN}.json") == (2 if retried else 1)
        assert sleeps == (twin.slept(BACKOFF_1) if retried else [])

    @pytest.mark.parametrize("op", OPS)
    async def test_exhaustion_surfaces_the_status_after_every_attempt(
        self,
        op: Op,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(
            method=op,
            url=op_url(op),
            status_code=503,
            json=error_payload("503", "busy"),
            is_reusable=True,
        )

        with pytest.raises(exceptions.ServerError, match="busy") as excinfo:
            await perform(session, op)

        assert excinfo.value.status_code == 503
        assert len(httpx_mock.get_requests()) == 1 + RETRY.attempts
        assert sleeps == twin.slept(BACKOFF_1, BACKOFF_2)


class TestRetryAfter:
    async def test_the_numeric_form_is_slept_before_the_backoff(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(
            method="GET", url=TENANTS_URL, status_code=503, headers={"retry-after": "5"}
        )
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert sleeps == twin.slept(5.0, BACKOFF_1)

    async def test_the_http_date_form_is_measured_against_now(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        when = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
        httpx_mock.add_response(
            method="GET", url=TENANTS_URL, status_code=503, headers={"retry-after": when}
        )
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        (channel, delay), backoff = sleeps
        assert channel == twin.sleep_channel
        assert 15.0 < delay <= 20.0  # the date has one-second resolution
        assert backoff == (twin.sleep_channel, BACKOFF_1)

    @pytest.mark.parametrize("header", ["3600", "__date__"], ids=["seconds", "http-date"])
    async def test_it_is_bounded_by_retry_after_max(
        self,
        header: str,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        if header == "__date__":
            header = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
        capped = RetryConfig(
            attempts=3, wait_initial=0.5, wait_max=5.0, wait_jitter=0.0, retry_after_max=7.0
        )
        session = await logged_in(make, httpx_mock, retry=capped)
        httpx_mock.add_response(
            method="GET", url=TENANTS_URL, status_code=503, headers={"retry-after": header}
        )
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert sleeps == twin.slept(7.0, BACKOFF_1)

    async def test_it_is_not_slept_on_the_final_attempt(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        """Stamina gives up right after: a sleep there would only delay the error."""
        two = RetryConfig(attempts=2, wait_initial=0.5, wait_max=5.0, wait_jitter=0.0)
        session = await logged_in(make, httpx_mock, retry=two)
        httpx_mock.add_response(
            method="GET",
            url=TENANTS_URL,
            status_code=503,
            headers={"retry-after": "5"},
            is_reusable=True,
        )

        with pytest.raises(exceptions.ServerError):
            await maybe(session.get(TENANTS))

        assert trail(httpx_mock).count(f"GET {TENANTS}") == 2
        assert sleeps == twin.slept(5.0, BACKOFF_1)

    async def test_a_single_attempt_never_sleeps(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RetryConfig(attempts=1))
        httpx_mock.add_response(
            method="GET", url=TENANTS_URL, status_code=503, headers={"retry-after": "5"}
        )

        with pytest.raises(exceptions.ServerError):
            await maybe(session.get(TENANTS))

        assert sleeps == []

    async def test_a_write_honours_it_too(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(
            method="POST", url=MO_URL, status_code=503, headers={"retry-after": "2"}
        )
        httpx_mock.add_response(method="POST", url=MO_URL, json=ok())

        await maybe(session.post_mo(MO_DN, PAYLOAD))

        assert sleeps == twin.slept(2.0, BACKOFF_1)

    @pytest.mark.parametrize("header", ["soon", "-5", "0"])
    async def test_an_unusable_header_leaves_only_the_backoff(
        self,
        header: str,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_response(
            method="GET", url=TENANTS_URL, status_code=503, headers={"retry-after": header}
        )
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert sleeps == twin.slept(BACKOFF_1)


# ── Transport errors ──────────────────────────────────────────────────────────

TRANSPORT_FAILURES = [
    pytest.param(lambda: httpx.ConnectError("refused"), exceptions.ConnectionError, id="connect"),
    pytest.param(lambda: httpx.ReadTimeout("slow"), exceptions.TimeoutError, id="read-timeout"),
    pytest.param(tls_failure, exceptions.TLSError, id="tls"),
    pytest.param(
        lambda: httpx.RemoteProtocolError("reset"), exceptions.TransportError, id="protocol"
    ),
]


class TestTransportErrors:
    async def test_a_read_retries_through_transient_failures(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock, sleeps: Sleeps
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_exception(httpx.ConnectError("refused"), url=TENANTS_URL)
        httpx_mock.add_exception(httpx.RemoteProtocolError("reset"), url=TENANTS_URL)
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        assert await maybe(session.get(TENANTS)) == []
        assert sleeps == twin.slept(BACKOFF_1, BACKOFF_2)

    @pytest.mark.parametrize(("failure", "expected"), TRANSPORT_FAILURES)
    async def test_a_persistent_read_failure_is_retried_then_typed(
        self,
        failure: Callable[[], httpx.TransportError],
        expected: type[exceptions.TransportError],
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_exception(failure(), url=TENANTS_URL, is_reusable=True)

        with pytest.raises(exceptions.TransportError) as excinfo:
            await maybe(session.get(TENANTS))

        assert type(excinfo.value) is expected
        assert isinstance(excinfo.value.__cause__, httpx.TransportError)
        assert trail(httpx_mock).count(f"GET {TENANTS}") == RETRY.attempts
        assert sleeps == twin.slept(BACKOFF_1, BACKOFF_2)

    @pytest.mark.parametrize("op", ["POST", "DELETE"])
    @pytest.mark.parametrize(
        ("failure", "expected", "attempts"),
        [
            pytest.param(
                lambda: httpx.ConnectError("refused"), exceptions.ConnectionError, 3, id="connect"
            ),
            pytest.param(
                lambda: httpx.ConnectTimeout("slow"),
                exceptions.TimeoutError,
                3,
                id="connect-timeout",
            ),
            pytest.param(
                lambda: httpx.PoolTimeout("pool"), exceptions.TimeoutError, 3, id="pool-timeout"
            ),
            pytest.param(tls_failure, exceptions.TLSError, 3, id="tls"),
            pytest.param(
                lambda: httpx.ReadTimeout("slow"), exceptions.TimeoutError, 1, id="read-timeout"
            ),
            pytest.param(
                lambda: httpx.RemoteProtocolError("reset"),
                exceptions.TransportError,
                1,
                id="protocol",
            ),
        ],
    )
    async def test_a_write_retries_only_failures_before_sending(
        self,
        op: Op,
        failure: Callable[[], httpx.TransportError],
        expected: type[exceptions.TransportError],
        attempts: int,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        sleeps: Sleeps,
    ) -> None:
        """A timeout after sending may mean the APIC applied it: no replay."""
        session = await logged_in(make, httpx_mock, retry=RETRY)
        httpx_mock.add_exception(failure(), url=MO_URL, is_reusable=True)

        with pytest.raises(exceptions.TransportError) as excinfo:
            await perform(session, op)

        assert type(excinfo.value) is expected
        assert trail(httpx_mock).count(f"{op} /api/mo/{MO_DN}.json") == attempts
        assert sleeps == twin.slept(*[BACKOFF_1, BACKOFF_2][: attempts - 1])


# ── HTTP status mapping ───────────────────────────────────────────────────────


class TestStatusMapping:
    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, exceptions.APIError),
            (403, exceptions.ForbiddenError),
            (404, exceptions.NotFoundError),
            (500, exceptions.ServerError),
        ],
        ids=["400", "403", "404", "500"],
    )
    async def test_a_status_maps_to_one_typed_error_with_the_apic_code(
        self,
        op: Op,
        status: int,
        expected: type[exceptions.APIError],
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(
            method=op, url=op_url(op), status_code=status, json=error_payload("107", "nope")
        )

        with pytest.raises(exceptions.APIError) as excinfo:
            await perform(session, op)

        assert type(excinfo.value) is expected
        assert excinfo.value.status_code == status
        assert excinfo.value.apic_message == "nope"
        assert excinfo.value.apic_code == "107"

    @pytest.mark.parametrize(
        ("body", "message"),
        [("<html>gateway</html>", "non-JSON"), ("[1, 2]", "non-object")],
        ids=["html", "json-list"],
    )
    @pytest.mark.parametrize("reader", ["get", "all-pages"])
    async def test_a_2xx_without_a_json_object_is_an_api_error(
        self,
        body: str,
        message: str,
        reader: str,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method="GET", text=body)

        with pytest.raises(exceptions.APIError) as excinfo:
            if reader == "get":
                await maybe(session.get(TENANTS))
            else:
                await get_all_pages(session, TENANTS, {})

        assert type(excinfo.value) is exceptions.APIError
        assert excinfo.value.status_code == 200
        assert message in excinfo.value.apic_message

    @pytest.mark.parametrize("op", ["POST", "DELETE"])
    async def test_a_write_does_not_read_its_success_body(
        self, op: Op, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method=op, url=MO_URL, text="<html>ok</html>")

        assert await perform(session, op) is None


class TestGetMo:
    async def test_it_returns_the_typed_object(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
            json=load_fixture("fvBD_list"),
        )

        found = await maybe(session.get_mo("uni/tn-Prod/BD-Prod-BD", cls=fvBD))

        assert isinstance(found, fvBD)
        assert found.name == "Prod-BD"

    async def test_an_empty_answer_is_not_found(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method="GET", url=MO_URL, json=ok())

        with pytest.raises(exceptions.NotFoundError, match="MO not found"):
            await maybe(session.get_mo(MO_DN))


# ── Pagination ────────────────────────────────────────────────────────────────


def page_params(httpx_mock: HTTPXMock) -> list[dict[str, str]]:
    """Query parameters of every data GET (login excluded), in order."""
    return [dict(r.url.params) for r in httpx_mock.get_requests() if r.method == "GET"]


class TestPagination:
    async def test_it_pages_until_total_count_is_satisfied(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(
            method="GET", json={"totalCount": "3", "imdata": [bd("a"), bd("b")]}
        )
        httpx_mock.add_response(method="GET", json={"totalCount": "3", "imdata": [bd("c")]})

        items = await get_all_pages(session, BDS, {"query-target": "self"}, page_size=2)

        assert names(items) == ["a", "b", "c"]
        assert page_params(httpx_mock) == [
            {"query-target": "self", "page": "0", "page-size": "2"},
            {"query-target": "self", "page": "1", "page-size": "2"},
        ]

    @pytest.mark.parametrize("total", ["0", "banana", None], ids=["zero", "unreadable", "absent"])
    async def test_an_untrustworthy_total_count_pages_until_an_empty_page(
        self, total: str | None, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """Believe the data: "0" next to items is the controller contradicting itself."""
        session = await logged_in(make, httpx_mock)
        for page in ([bd("a")], [bd("b")], []):
            body: dict[str, Any] = {"imdata": page}
            if total is not None:
                body["totalCount"] = total
            httpx_mock.add_response(method="GET", json=body)

        items = await get_all_pages(session, BDS, {}, page_size=1)

        assert names(items) == ["a", "b"]
        assert [p["page"] for p in page_params(httpx_mock)] == ["0", "1", "2"]

    async def test_the_page_count_guard_stops_a_runaway_total_count(
        self,
        twin: Twin,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(twin.module, "_MAX_PAGINATION_PAGES", 2)
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(
            method="GET", json={"totalCount": "999", "imdata": [bd("x")]}, is_reusable=True
        )

        with pytest.raises(exceptions.ServerError, match="Pagination guard"):
            await get_all_pages(session, BDS, {})

        # page 0 before the loop, pages 1 and 2 inside it, the guard before page 3
        assert [p["page"] for p in page_params(httpx_mock)] == ["0", "1", "2"]

    async def test_an_explicit_page_is_one_request_passed_through(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method="GET", json={"totalCount": "99", "imdata": [bd("p3")]})

        items = await get_all_pages(session, BDS, {"page": "3", "page-size": "10"})

        assert names(items) == ["p3"]
        assert page_params(httpx_mock) == [{"page": "3", "page-size": "10"}]

    async def test_an_empty_first_page_is_one_request_and_no_pages(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(method="GET", json=ok())

        assert await all_pages(session, BDS, {}) == []
        assert len(page_params(httpx_mock)) == 1

    async def test_the_page_iterator_yields_each_page_as_served(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        httpx_mock.add_response(
            method="GET", json={"totalCount": "3", "imdata": [bd("a"), bd("b")]}
        )
        httpx_mock.add_response(method="GET", json={"totalCount": "3", "imdata": [bd("c")]})

        pages = await all_pages(session, BDS, {}, page_size=2)

        assert [names(page) for page in pages] == [["a", "b"], ["c"]]


# ── Version, lifecycle ────────────────────────────────────────────────────────


class TestApicVersion:
    async def test_it_is_none_before_login_and_read_at_login(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        assert make().apic_version is None
        session = await logged_in(make, httpx_mock)
        assert session.apic_version == "6.0(9c)"

    @pytest.mark.parametrize(
        ("stated", "kept"), [("6.1(1a)", "6.1(1a)"), (None, "6.0(9c)")], ids=["new", "silent"]
    )
    async def test_a_refresh_updates_it_but_a_silent_one_keeps_it(
        self,
        stated: str | None,
        kept: str,
        make: Callable[..., Session],
        httpx_mock: HTTPXMock,
    ) -> None:
        session = await logged_in(make, httpx_mock, ttl=30, refresh_threshold=60)
        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, json=refresh_payload("t2", version=stated)
        )
        httpx_mock.add_response(method="GET", url=TENANTS_URL, json=ok())

        await maybe(session.get(TENANTS))

        assert session.apic_version == kept


class TestClose:
    async def test_close_is_idempotent_and_is_closed_follows_it(
        self, make: Callable[..., Session]
    ) -> None:
        session = make()
        assert not session.is_closed

        await maybe(session.close())
        assert session.is_closed
        await maybe(session.close())  # a second close must not raise
        assert session.is_closed

    async def test_a_request_after_close_raises_the_httpx_runtime_error(
        self, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        session = await logged_in(make, httpx_mock)
        await maybe(session.close())

        with pytest.raises(RuntimeError, match="closed"):
            await maybe(session.get(TENANTS))

    async def test_the_context_manager_logs_in_and_closes(
        self, twin: Twin, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t1"))
        session = twin.build()

        if isinstance(session, AsyncApicSession):
            async with session as entered:
                assert entered is session
                assert session.is_authenticated
        else:
            with session as entered:
                assert entered is session
                assert session.is_authenticated

        assert session.is_closed


# ── Concurrency ───────────────────────────────────────────────────────────────


async def concurrently(twin: Twin, n: int, call: Callable[[], object]) -> list[object]:
    """Run *call* n times at once: threads for sync, coroutines for async."""
    if twin.kind == "async":
        return list(await asyncio.gather(*(maybe(call()) for _ in range(n))))
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(call) for _ in range(n)]
        return [future.result(timeout=10) for future in futures]


class TestConcurrency:
    async def test_a_proactive_refresh_runs_once_for_concurrent_callers(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """The token lock serialises the refresh on both twins."""
        refreshes = 0

        def count(request: httpx.Request) -> httpx.Response:
            nonlocal refreshes
            refreshes += 1
            return httpx.Response(200, json=refresh_payload(f"r{refreshes}"))

        if twin.kind == "sync":

            def sync_refresh(request: httpx.Request) -> httpx.Response:
                time.sleep(0.05)  # let the other threads pile up on the lock
                return count(request)

            httpx_mock.add_callback(sync_refresh, url=REFRESH_URL, is_reusable=True)
        else:

            async def async_refresh(request: httpx.Request) -> httpx.Response:
                await asyncio.sleep(0.05)
                return count(request)

            httpx_mock.add_callback(async_refresh, url=REFRESH_URL, is_reusable=True)

        session = await logged_in(make, httpx_mock, ttl=30, refresh_threshold=60)
        await concurrently(twin, 5, session._ensure_token)

        assert refreshes == 1
        assert token_of(session) == "r1"

    @pytest.mark.parametrize(
        "twin",
        [pytest.param(SYNC, id="sync"), pytest.param(ASYNC, id="async")],
    )
    async def test_a_401_stampede_relogs_exactly_once(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """Five callers hit a revoked token together: one re-login, not five.

        All five first requests are held until every one has arrived, so the
        401s genuinely land together — a barrier of threads on the sync twin,
        of coroutines on the async one.
        """
        callers = 5
        lock = threading.Lock()
        logins = 0

        def answer(request: httpx.Request) -> httpx.Response | None:
            """Serve logins and post-recovery reads; ``None`` means "a 401, gated"."""
            nonlocal logins
            with lock:
                if request.url.path == "/api/aaaLogin.json":
                    logins += 1
                    return httpx.Response(200, json=login_payload(f"t{logins}"))
                if logins >= 2:
                    return httpx.Response(200, json=load_fixture("fvTenant_list"))
            return None

        if twin.kind == "sync":
            gate = threading.Barrier(callers, timeout=5)

            def sync_handler(request: httpx.Request) -> httpx.Response:
                if (served := answer(request)) is not None:
                    return served
                gate.wait()
                return httpx.Response(401, json=error_payload())

            httpx_mock.add_callback(sync_handler, is_reusable=True)
        else:
            async_gate = asyncio.Barrier(callers)

            async def async_handler(request: httpx.Request) -> httpx.Response:
                if (served := answer(request)) is not None:
                    return served
                async with asyncio.timeout(5):
                    await async_gate.wait()
                return httpx.Response(401, json=error_payload())

            httpx_mock.add_callback(async_handler, is_reusable=True)

        session = make()
        await maybe(session.login())
        results = await concurrently(twin, callers, lambda: session.get(TENANTS))

        assert all(len(cast(list[Any], result)) == 3 for result in results)
        assert logins == 2  # the initial login plus exactly one reactive re-login
        assert token_of(session) == "t2"

    async def test_the_concurrency_bound_is_an_async_only_contract(
        self, twin: Twin, make: Callable[..., Session], httpx_mock: HTTPXMock
    ) -> None:
        """By design, not drift: the async session bounds its own fan-out.

        A coroutine fan-out has no natural limit, so ``AsyncApicSession``
        carries a semaphore (``max_concurrent``).  A sync caller bounds
        concurrency with the thread pool it chose, so ``ApicSession`` exposes
        no such knob — and must keep not exposing one.
        """
        if twin.kind == "sync":
            with pytest.raises(TypeError, match="max_concurrent"):
                make(max_concurrent=2)
            assert not hasattr(ApicSession, "max_concurrent")
            return

        with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
            make(max_concurrent=0)

        in_flight = 0
        peak = 0

        async def slow(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json=ok())

        httpx_mock.add_callback(slow, url=TENANTS_URL, is_reusable=True)
        session = await logged_in(make, httpx_mock, max_concurrent=2)
        assert isinstance(session, AsyncApicSession)
        assert session.max_concurrent == 2

        await concurrently(twin, 6, lambda: session.get(TENANTS))

        assert peak == 2
