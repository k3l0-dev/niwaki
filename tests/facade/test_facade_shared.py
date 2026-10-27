"""One construction path for both clients — ``Niwaki`` and ``AsyncNiwaki`` are twins.

The two façade clients validate the same options and hand the same keyword
arguments to their session class; only ``max_concurrent`` (async-only, the
fan-out bound) separates them.  Until now that sameness was a copy: the
constructor validation and the session-building block were written twice, and
every divergence this project has paid for had the same shape — a correction
landed in one twin only.  This module pins the behaviour from the outside
(sections 1-3, parametrised over both flavours, green before and after the
extraction), then pins the shared helper and the parity post-state the
extraction must reach (sections 4-5).

The shared helper — ``niwaki.facade._session_kwargs`` (contract)
----------------------------------------------------------------

A private, module-level, pure function.  It is *the* place where client options
are validated and turned into the session class's keyword arguments, so both
clients call it and neither keeps its own copy::

    def _session_kwargs(
        *,
        host: str | None,
        username: str | None,
        password: str | SecretStr | None,
        verify_ssl: bool | str,
        timeout: float,
        refresh_threshold: int,
        retry: RetryConfig | None,
        token_cache: TokenCache | None,
        private_key: str | Path | None,
        cert_dn: str | None,
        max_concurrent: int | None = None,
    ) -> dict[str, Any]: ...

- **Keyword-only**: a positional call raises ``TypeError``.
- **Validation** (raised by the helper, so a client calling it from its
  constructor fails where the mistake was written):

  - exactly one of ``private_key``/``cert_dn`` -> ``ValueError`` with the
    message ``"private_key and cert_dn must be given together"``;
  - ``max_concurrent`` given and ``< 1`` -> ``ValueError`` with the message
    ``f"max_concurrent must be >= 1, got {max_concurrent}"``.

- **Returned mapping** — a fresh ``dict`` on every call:

  - always ``host``, ``username``, ``verify_ssl``, ``timeout``,
    ``refresh_threshold``, passed through unchanged (``None`` stays ``None``:
    the environment fallback belongs to the session, read at entry);
  - always ``password``: a ``str`` (even ``""``) is wrapped in a
    ``SecretStr``, a ``SecretStr`` is passed through *by identity*, ``None``
    stays ``None``;
  - ``max_concurrent`` only when given (not ``None``);
  - ``retry`` and ``token_cache`` only when not ``None``;
  - ``private_key`` and ``cert_dn`` only when the pair is given;
  - never ``client`` — an injected ``httpx`` client (``with_client``) is
    attached by the caller at entry, after the mapping is built.

- **Pure**: it reads no environment variable and performs no I/O (a private
  key path is forwarded, never opened).
- **Used**: whatever the client hands its session class is exactly the
  helper's mapping, plus ``client`` when one was injected.

Calling it from ``__init__`` and storing the mapping is the natural shape (the
validation then fires at construction, which section 1 requires); the
session-building block of ``__enter__``/``__aenter__`` then becomes identical
in both twins, which is what section 5 pins in the twin parity guard.

Scenarios
---------

1. Construction (both flavours): the cert pairing errors (same type, same
   exact message, also through ``with_client``); construction is inert (no
   session built, no environment read, no key file opened); the password is
   never kept in clear; ``max_concurrent`` is refused by every sync entry
   point and validated by every async one.
2. Session hand-off (spy on ``niwaki.facade.ApicSession`` /
   ``AsyncApicSession``): context entry, ``connect()`` and ``with_client()``
   hand the session exactly the expected kwargs — the same on both sides,
   plus ``max_concurrent`` on the async one — including the ``SecretStr``
   passed by identity.
3. A failed login (rejected credentials, or any ``BaseException`` out of
   ``login``) closes the session it built, on both sides, through every entry
   point, and leaves the client re-enterable.
4. The helper contract above (RED until the helper exists).
5. Parity post-state: ``facade_client``'s ``Niwaki.__enter__`` divergence is
   retired, from the live diff and from the allowlist; the cert-pairing
   message has a single definition in ``facade.py`` (RED until extracted).
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NoReturn, NotRequired, Required, TypedDict, cast

import httpx
import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from niwaki import exceptions, facade
from niwaki.facade import AsyncNiwaki, Niwaki
from niwaki.transport import RetryConfig
from niwaki.transport._token_cache import TokenCache
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload
from tests.test_twin_parity import ALLOWLIST_PATH, PAIRS, pair_divergences

type Flavour = Literal["sync", "async"]
type Client = Niwaki | AsyncNiwaki
type Kwargs = dict[str, object]

FLAVOURS: tuple[Flavour, ...] = ("sync", "async")
CERT_MESSAGE = "private_key and cert_dn must be given together"
CERT_DN = "uni/userext/user-admin/usercert-ci"
MISSING_KEY = "/nonexistent/niwaki-l85/ci.key"  # never opened at construction or hand-off
DEFAULT_MAX_CONCURRENT = 10
ENV_VARS = ("APIC_HOST", "APIC_USERNAME", "APIC_PASSWORD")


class Options(TypedDict, total=False):
    """The constructor options both clients share (``max_concurrent`` aside)."""

    host: str | None
    username: str | None
    password: str | SecretStr | None
    verify_ssl: bool | str
    timeout: float
    refresh_threshold: int
    retry: RetryConfig | None
    token_cache: TokenCache | None
    private_key: str | Path | None
    cert_dn: str | None


class ConnectOptions(TypedDict):
    """The options both ``connect()`` factories share (``max_concurrent`` aside)."""

    host: Required[str]
    username: Required[str]
    password: Required[str | SecretStr]
    verify_ssl: NotRequired[bool | str]
    timeout: NotRequired[float]
    refresh_threshold: NotRequired[int]
    retry: NotRequired[RetryConfig | None]


class _SessionBuilt(Exception):
    """Raised by the spy in place of building a session: entry stops right there."""


class _Interrupted(BaseException):
    """A non-``Exception`` failure out of ``login`` (cancellation, interrupt)."""


# ── Flavour-generic drivers ───────────────────────────────────────────────────


def build(flavour: Flavour, options: Options) -> Client:
    return Niwaki(**options) if flavour == "sync" else AsyncNiwaki(**options)


async def enter(aci: Client) -> None:
    if isinstance(aci, AsyncNiwaki):
        await aci.__aenter__()
    else:
        aci.__enter__()


async def close(aci: Client) -> None:
    if isinstance(aci, AsyncNiwaki):
        await aci.close()
    else:
        aci.close()


async def connect(flavour: Flavour, options: ConnectOptions) -> Client:
    if flavour == "sync":
        return Niwaki.connect(**options)
    return await AsyncNiwaki.connect(**options)


def http_client(flavour: Flavour) -> httpx.Client | httpx.AsyncClient:
    return httpx.Client(base_url=HOST) if flavour == "sync" else httpx.AsyncClient(base_url=HOST)


def with_client(
    flavour: Flavour, client: httpx.Client | httpx.AsyncClient, options: Options
) -> Client:
    if flavour == "sync":
        assert isinstance(client, httpx.Client)
        return Niwaki.with_client(client, **options)
    assert isinstance(client, httpx.AsyncClient)
    return AsyncNiwaki.with_client(client, **options)


async def close_http(client: httpx.Client | httpx.AsyncClient) -> None:
    if isinstance(client, httpx.AsyncClient):
        await client.aclose()
    else:
        client.close()


def session_attr(flavour: Flavour) -> str:
    return "ApicSession" if flavour == "sync" else "AsyncApicSession"


def install_spy(monkeypatch: pytest.MonkeyPatch, flavour: Flavour) -> list[Kwargs]:
    """Replace the façade's session class: record the call, then stop entry."""
    recorded: list[Kwargs] = []

    def spy(*args: object, **kwargs: object) -> NoReturn:
        assert args == (), f"the session must be built from keywords only, got {args!r}"
        recorded.append(kwargs)
        raise _SessionBuilt

    monkeypatch.setattr(facade, session_attr(flavour), spy)
    return recorded


async def handed_off(monkeypatch: pytest.MonkeyPatch, aci: Client) -> Kwargs:
    """Enter *aci* against the spy and return the kwargs its session received."""
    flavour: Flavour = "async" if isinstance(aci, AsyncNiwaki) else "sync"
    recorded = install_spy(monkeypatch, flavour)
    with pytest.raises(_SessionBuilt):
        await enter(aci)
    assert len(recorded) == 1
    return recorded[0]


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ══ 1. Construction: identical validation ══════════════════════════════════════


@pytest.mark.parametrize("flavour", FLAVOURS)
class TestConstructionIsIdentical:
    @pytest.mark.parametrize(
        "half",
        [{"cert_dn": CERT_DN}, {"private_key": MISSING_KEY}, {"private_key": Path(MISSING_KEY)}],
        ids=["cert_dn-alone", "private_key-alone", "private_key-path-alone"],
    )
    def test_half_a_certificate_pair_is_refused(self, flavour: Flavour, half: Options) -> None:
        with pytest.raises(ValueError) as caught:
            build(flavour, {"host": HOST, "username": "admin", "password": "pw", **half})
        assert type(caught.value) is ValueError
        assert str(caught.value) == CERT_MESSAGE

    async def test_with_client_validates_the_pair_the_same_way(self, flavour: Flavour) -> None:
        client = http_client(flavour)
        try:
            with pytest.raises(ValueError) as caught:
                with_client(flavour, client, {"host": HOST, "cert_dn": CERT_DN})
            assert str(caught.value) == CERT_MESSAGE
        finally:
            await close_http(client)

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {"host": HOST, "username": "admin", "password": "pw"},
            {"host": HOST, "username": "admin", "password": SecretStr("pw")},
            {"host": HOST, "username": "admin", "private_key": MISSING_KEY, "cert_dn": CERT_DN},
            {"private_key": Path(MISSING_KEY), "cert_dn": CERT_DN, "password": ""},
        ],
        ids=["nothing", "str-password", "secretstr-password", "certificate", "cert-path-empty-pw"],
    )
    def test_construction_is_inert(
        self,
        flavour: Flavour,
        options: Options,
        monkeypatch: pytest.MonkeyPatch,
        no_env: None,
    ) -> None:
        """No session, no environment read, no key file opened — all deferred to entry."""
        recorded = install_spy(monkeypatch, flavour)
        aci = build(flavour, options)
        assert recorded == []
        assert aci.apic_version is None

    def test_password_is_never_kept_in_clear(self, flavour: Flavour) -> None:
        aci = build(flavour, {"host": HOST, "username": "admin", "password": "hunter2-l85"})
        assert "hunter2-l85" not in repr(vars(aci))
        assert "hunter2-l85" not in repr(aci)


class TestMaxConcurrentIsAsyncOnly:
    def test_sync_has_no_max_concurrent(self) -> None:
        assert "max_concurrent" not in inspect.signature(Niwaki).parameters
        assert "max_concurrent" not in inspect.signature(Niwaki.connect).parameters
        assert not hasattr(Niwaki, "max_concurrent")

    def test_sync_constructor_refuses_it(self) -> None:
        with pytest.raises(TypeError, match="max_concurrent"):
            Niwaki(HOST, "admin", "pw", max_concurrent=5)  # pyright: ignore[reportCallIssue]

    def test_sync_connect_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = install_spy(monkeypatch, "sync")
        with pytest.raises(TypeError, match="max_concurrent"):
            Niwaki.connect(HOST, "admin", "pw", max_concurrent=5)  # pyright: ignore[reportCallIssue]
        assert recorded == []

    def test_sync_with_client_refuses_it(self) -> None:
        with (
            httpx.Client(base_url=HOST) as client,
            pytest.raises(TypeError, match="max_concurrent"),
        ):
            Niwaki.with_client(client, HOST, "admin", "pw", max_concurrent=5)

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_async_constructor_validates_it(self, bad: int) -> None:
        with pytest.raises(ValueError) as caught:
            AsyncNiwaki(HOST, "admin", "pw", max_concurrent=bad)
        assert str(caught.value) == f"max_concurrent must be >= 1, got {bad}"

    async def test_async_connect_validates_it_before_any_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = install_spy(monkeypatch, "async")
        with pytest.raises(ValueError, match=r"^max_concurrent must be >= 1, got 0$"):
            await AsyncNiwaki.connect(HOST, "admin", "pw", max_concurrent=0)
        assert recorded == []

    async def test_async_with_client_validates_it(self) -> None:
        async with httpx.AsyncClient(base_url=HOST) as client:
            with pytest.raises(ValueError, match=r"^max_concurrent must be >= 1, got 0$"):
                AsyncNiwaki.with_client(client, HOST, "admin", "pw", max_concurrent=0)

    @pytest.mark.parametrize("value", [1, 2, 64])
    def test_async_accepts_and_exposes_it(self, value: int) -> None:
        assert AsyncNiwaki(HOST, "admin", "pw", max_concurrent=value).max_concurrent == value

    def test_async_default(self) -> None:
        assert AsyncNiwaki(HOST, "admin", "pw").max_concurrent == DEFAULT_MAX_CONCURRENT


# ══ 2. Session hand-off: the same kwargs on both sides ═════════════════════════

_RETRY = RetryConfig(attempts=5)
_TOKEN_CACHE = TokenCache(Path("/nonexistent/niwaki-l85-cache"))
_SECRET = SecretStr("pw-secret")

# (options, the exact kwargs the SYNC session must receive); the async session
# must receive the same plus ``max_concurrent``.
HANDOFF_SCENARIOS: dict[str, tuple[Options, Kwargs]] = {
    "defaults": (
        {"host": HOST, "username": "admin", "password": "pw"},
        {
            "host": HOST,
            "username": "admin",
            "password": SecretStr("pw"),
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
        },
    ),
    "environment-fallback": (
        {},
        {
            "host": None,
            "username": None,
            "password": None,
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
        },
    ),
    "empty-password": (
        {"host": HOST, "username": "admin", "password": ""},
        {
            "host": HOST,
            "username": "admin",
            "password": SecretStr(""),
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
        },
    ),
    "secretstr": (
        {"host": HOST, "username": "admin", "password": _SECRET},
        {
            "host": HOST,
            "username": "admin",
            "password": _SECRET,
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
        },
    ),
    "tuned-transport": (
        {
            "host": HOST,
            "username": "admin",
            "password": "pw",
            "verify_ssl": "/etc/ssl/private-ca.pem",
            "timeout": 5.5,
            "refresh_threshold": 120,
            "retry": _RETRY,
        },
        {
            "host": HOST,
            "username": "admin",
            "password": SecretStr("pw"),
            "verify_ssl": "/etc/ssl/private-ca.pem",
            "timeout": 5.5,
            "refresh_threshold": 120,
            "retry": _RETRY,
        },
    ),
    "explicit-nones": (
        {"host": HOST, "username": "admin", "password": None, "retry": None, "token_cache": None},
        {
            "host": HOST,
            "username": "admin",
            "password": None,
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
        },
    ),
    "certificate": (
        {
            "host": HOST,
            "username": "admin",
            "private_key": MISSING_KEY,
            "cert_dn": CERT_DN,
            "token_cache": _TOKEN_CACHE,
        },
        {
            "host": HOST,
            "username": "admin",
            "password": None,
            "verify_ssl": True,
            "timeout": 30.0,
            "refresh_threshold": 60,
            "token_cache": _TOKEN_CACHE,
            "private_key": MISSING_KEY,
            "cert_dn": CERT_DN,
        },
    ),
    "everything": (
        {
            "host": HOST,
            "username": "admin",
            "password": _SECRET,
            "verify_ssl": False,
            "timeout": 1.0,
            "refresh_threshold": 5,
            "retry": _RETRY,
            "token_cache": _TOKEN_CACHE,
            "private_key": Path(MISSING_KEY),
            "cert_dn": CERT_DN,
        },
        {
            "host": HOST,
            "username": "admin",
            "password": _SECRET,
            "verify_ssl": False,
            "timeout": 1.0,
            "refresh_threshold": 5,
            "retry": _RETRY,
            "token_cache": _TOKEN_CACHE,
            "private_key": Path(MISSING_KEY),
            "cert_dn": CERT_DN,
        },
    ),
}


def _async_expected(sync_expected: Kwargs, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> Kwargs:
    return {**sync_expected, "max_concurrent": max_concurrent}


def _assert_password_identity(handed: Kwargs, options: Options) -> None:
    """A ``SecretStr`` is handed over as is — never unwrapped, never re-wrapped."""
    given = options.get("password")
    if isinstance(given, SecretStr):
        assert handed["password"] is given
    elif isinstance(given, str):
        assert isinstance(handed["password"], SecretStr)


@pytest.mark.parametrize("scenario", sorted(HANDOFF_SCENARIOS))
class TestSessionHandOff:
    async def test_context_entry(
        self, scenario: str, monkeypatch: pytest.MonkeyPatch, no_env: None
    ) -> None:
        options, expected = HANDOFF_SCENARIOS[scenario]
        sync = await handed_off(monkeypatch, build("sync", options))
        asynchronous = await handed_off(monkeypatch, build("async", options))
        assert sync == expected
        assert asynchronous == _async_expected(expected)
        _assert_password_identity(sync, options)
        _assert_password_identity(asynchronous, options)

    async def test_with_client(
        self, scenario: str, monkeypatch: pytest.MonkeyPatch, no_env: None
    ) -> None:
        options, expected = HANDOFF_SCENARIOS[scenario]
        handed: dict[Flavour, Kwargs] = {}
        for flavour in FLAVOURS:
            client = http_client(flavour)
            try:
                kwargs = await handed_off(monkeypatch, with_client(flavour, client, options))
            finally:
                await close_http(client)
            assert kwargs.pop("client") is client
            handed[flavour] = kwargs
        assert handed["sync"] == expected
        assert handed["async"] == _async_expected(expected)


CONNECT_SCENARIOS: dict[str, tuple[ConnectOptions, Kwargs]] = {
    "defaults": (
        {"host": HOST, "username": "admin", "password": "pw"},
        HANDOFF_SCENARIOS["defaults"][1],
    ),
    "secretstr": (
        {"host": HOST, "username": "admin", "password": _SECRET},
        HANDOFF_SCENARIOS["secretstr"][1],
    ),
    "tuned-transport": (
        {
            "host": HOST,
            "username": "admin",
            "password": "pw",
            "verify_ssl": "/etc/ssl/private-ca.pem",
            "timeout": 5.5,
            "refresh_threshold": 120,
            "retry": _RETRY,
        },
        HANDOFF_SCENARIOS["tuned-transport"][1],
    ),
}


class TestConnectHandOff:
    @pytest.mark.parametrize("scenario", sorted(CONNECT_SCENARIOS))
    async def test_connect_hands_off_like_entry(
        self, scenario: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        options, expected = CONNECT_SCENARIOS[scenario]
        handed: dict[Flavour, Kwargs] = {}
        for flavour in FLAVOURS:
            recorded = install_spy(monkeypatch, flavour)
            with pytest.raises(_SessionBuilt):
                await connect(flavour, options)
            assert len(recorded) == 1
            handed[flavour] = recorded[0]
        assert handed["sync"] == expected
        assert handed["async"] == _async_expected(expected)
        if isinstance(options["password"], SecretStr):
            assert handed["sync"]["password"] is options["password"]
            assert handed["async"]["password"] is options["password"]

    @pytest.mark.parametrize("value", [1, 3, 32])
    async def test_async_max_concurrent_reaches_the_session(
        self, value: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = install_spy(monkeypatch, "async")
        with pytest.raises(_SessionBuilt):
            await AsyncNiwaki.connect(HOST, "admin", "pw", max_concurrent=value)
        assert recorded == [_async_expected(HANDOFF_SCENARIOS["defaults"][1], value)]
        handed = await handed_off(
            monkeypatch, AsyncNiwaki(HOST, "admin", "pw", max_concurrent=value)
        )
        assert handed == _async_expected(HANDOFF_SCENARIOS["defaults"][1], value)


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_every_entry_hands_off_the_same_kwargs(
    flavour: Flavour, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within one flavour, entry, re-entry, ``connect`` and ``with_client`` agree."""
    options: ConnectOptions = {
        "host": HOST,
        "username": "admin",
        "password": _SECRET,
        "retry": _RETRY,
    }
    aci = build(flavour, {**options})
    first = await handed_off(monkeypatch, aci)
    second = await handed_off(monkeypatch, aci)  # the failed entry left nothing behind

    recorded = install_spy(monkeypatch, flavour)
    with pytest.raises(_SessionBuilt):
        await connect(flavour, options)

    client = http_client(flavour)
    try:
        injected = await handed_off(monkeypatch, with_client(flavour, client, {**options}))
    finally:
        await close_http(client)
    assert injected.pop("client") is client

    assert first == second == recorded[0] == injected


# ══ 3. A failed login closes what it built, on both sides ══════════════════════

_REJECTED = {"imdata": [{"error": {"attributes": {"code": "401", "text": "bad creds"}}}]}


def record_logins(
    monkeypatch: pytest.MonkeyPatch, flavour: Flavour, *, interrupt: bool
) -> list[ApicSession | AsyncApicSession]:
    """Record every session whose ``login`` runs; optionally make it raise ``_Interrupted``."""
    built: list[ApicSession | AsyncApicSession] = []
    if flavour == "sync":
        original_sync = ApicSession.login

        def sync_login(self: ApicSession, *, use_cache: bool = True) -> None:
            built.append(self)
            if interrupt:
                raise _Interrupted
            original_sync(self, use_cache=use_cache)

        monkeypatch.setattr(ApicSession, "login", sync_login)
    else:
        original_async = AsyncApicSession.login

        async def async_login(self: AsyncApicSession, *, use_cache: bool = True) -> None:
            built.append(self)
            if interrupt:
                raise _Interrupted
            await original_async(self, use_cache=use_cache)

        monkeypatch.setattr(AsyncApicSession, "login", async_login)
    return built


type Entry = Literal["context", "connect", "with_client"]
ENTRIES: tuple[Entry, ...] = ("context", "connect", "with_client")


async def _attempt(
    flavour: Flavour, entry: Entry, client: httpx.Client | httpx.AsyncClient
) -> None:
    match entry:
        case "context":
            await enter(build(flavour, {"host": HOST, "username": "admin", "password": "wrong"}))
        case "connect":
            await connect(flavour, {"host": HOST, "username": "admin", "password": "wrong"})
        case "with_client":
            injected = with_client(
                flavour, client, {"host": HOST, "username": "admin", "password": "wrong"}
            )
            await enter(injected)


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("entry", ENTRIES)
class TestFailedLoginClosesTheSession:
    async def test_rejected_credentials(
        self,
        flavour: Flavour,
        entry: Entry,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, status_code=401, json=_REJECTED)
        built = record_logins(monkeypatch, flavour, interrupt=False)
        client = http_client(flavour)
        try:
            with pytest.raises(exceptions.LoginError):
                await _attempt(flavour, entry, client)
            assert len(built) == 1
            assert built[0].is_closed
            if entry == "with_client":
                # Closing the session closes the client it was handed.
                assert client.is_closed
        finally:
            await close_http(client)

    async def test_any_base_exception_out_of_login(
        self,
        flavour: Flavour,
        entry: Entry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        built = record_logins(monkeypatch, flavour, interrupt=True)
        client = http_client(flavour)
        try:
            with pytest.raises(_Interrupted):
                await _attempt(flavour, entry, client)
            assert len(built) == 1
            assert built[0].is_closed
        finally:
            await close_http(client)


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_a_failed_entry_leaves_the_client_reenterable(
    flavour: Flavour, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, status_code=401, json=_REJECTED)
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("tok"))
    aci = build(flavour, {"host": HOST, "username": "admin", "password": "pw"})
    with pytest.raises(exceptions.LoginError):
        await enter(aci)
    await enter(aci)
    try:
        session = aci._active_session if isinstance(aci, AsyncNiwaki) else aci._sync_session
        assert session.is_authenticated
        assert not session.is_closed
    finally:
        await close(aci)


# ══ 4. The shared helper (RED until it exists) ═════════════════════════════════

type SessionKwargs = Callable[..., dict[str, object]]


@pytest.fixture
def session_kwargs() -> SessionKwargs:
    helper = getattr(facade, "_session_kwargs", None)
    if helper is None:
        pytest.fail("niwaki.facade._session_kwargs does not exist yet (L8.5 shared helper)")
    assert callable(helper)
    return cast(SessionKwargs, helper)


def full_options(**overrides: object) -> dict[str, object]:
    """Every required helper option, at the constructors' defaults."""
    base: dict[str, object] = {
        "host": HOST,
        "username": "admin",
        "password": "pw",
        "verify_ssl": True,
        "timeout": 30.0,
        "refresh_threshold": 60,
        "retry": None,
        "token_cache": None,
        "private_key": None,
        "cert_dn": None,
    }
    return {**base, **overrides}


class TestSessionKwargsHelper:
    def test_is_a_module_level_function(self, session_kwargs: SessionKwargs) -> None:
        assert inspect.isfunction(session_kwargs)
        assert session_kwargs.__module__ == "niwaki.facade"

    def test_nominal_mapping(self, session_kwargs: SessionKwargs) -> None:
        assert session_kwargs(**full_options()) == HANDOFF_SCENARIOS["defaults"][1]

    @pytest.mark.parametrize("scenario", sorted(HANDOFF_SCENARIOS))
    def test_matches_every_handoff_scenario(
        self, session_kwargs: SessionKwargs, scenario: str
    ) -> None:
        options, expected = HANDOFF_SCENARIOS[scenario]
        defaults = {"host": None, "username": None, "password": None}
        assert session_kwargs(**full_options(**{**defaults, **options})) == expected

    def test_str_password_is_wrapped(self, session_kwargs: SessionKwargs) -> None:
        password = session_kwargs(**full_options(password="pw"))["password"]
        assert isinstance(password, SecretStr)
        assert password.get_secret_value() == "pw"

    def test_empty_password_is_wrapped_not_dropped(self, session_kwargs: SessionKwargs) -> None:
        """The empty-means-environment fallback is the session's call, at entry."""
        password = session_kwargs(**full_options(password=""))["password"]
        assert isinstance(password, SecretStr)
        assert password.get_secret_value() == ""

    def test_secretstr_is_passed_by_identity(self, session_kwargs: SessionKwargs) -> None:
        secret = SecretStr("pw")
        assert session_kwargs(**full_options(password=secret))["password"] is secret

    def test_none_password_stays_none(self, session_kwargs: SessionKwargs) -> None:
        assert session_kwargs(**full_options(password=None))["password"] is None

    def test_reads_no_environment(
        self, session_kwargs: SessionKwargs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ENV_VARS:
            monkeypatch.setenv(name, f"from-env-{name}")
        result = session_kwargs(**full_options(host=None, username=None, password=None))
        assert (result["host"], result["username"], result["password"]) == (None, None, None)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("retry", _RETRY), ("token_cache", _TOKEN_CACHE)],
    )
    def test_optional_keys_present_only_when_given(
        self, session_kwargs: SessionKwargs, key: str, value: object
    ) -> None:
        assert key not in session_kwargs(**full_options())
        assert session_kwargs(**full_options(**{key: value}))[key] is value

    @pytest.mark.parametrize("private_key", [MISSING_KEY, Path(MISSING_KEY)])
    def test_certificate_pair_is_forwarded_unopened(
        self, session_kwargs: SessionKwargs, private_key: str | Path
    ) -> None:
        result = session_kwargs(**full_options(private_key=private_key, cert_dn=CERT_DN))
        assert result["private_key"] is private_key
        assert result["cert_dn"] == CERT_DN

    def test_no_certificate_keys_without_the_pair(self, session_kwargs: SessionKwargs) -> None:
        result = session_kwargs(**full_options())
        assert "private_key" not in result
        assert "cert_dn" not in result

    @pytest.mark.parametrize(
        "half",
        [{"cert_dn": CERT_DN}, {"private_key": MISSING_KEY}],
        ids=["cert_dn-alone", "private_key-alone"],
    )
    def test_half_a_pair_raises(
        self, session_kwargs: SessionKwargs, half: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError) as caught:
            session_kwargs(**full_options(**half))
        assert str(caught.value) == CERT_MESSAGE

    def test_max_concurrent_absent_by_default(self, session_kwargs: SessionKwargs) -> None:
        assert "max_concurrent" not in session_kwargs(**full_options())
        assert "max_concurrent" not in session_kwargs(**full_options(), max_concurrent=None)

    @pytest.mark.parametrize("value", [1, 10, 500])
    def test_max_concurrent_forwarded_when_given(
        self, session_kwargs: SessionKwargs, value: int
    ) -> None:
        assert session_kwargs(**full_options(), max_concurrent=value)["max_concurrent"] == value

    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_concurrent_below_one_raises(self, session_kwargs: SessionKwargs, bad: int) -> None:
        with pytest.raises(ValueError) as caught:
            session_kwargs(**full_options(), max_concurrent=bad)
        assert str(caught.value) == f"max_concurrent must be >= 1, got {bad}"

    def test_never_carries_a_client(self, session_kwargs: SessionKwargs) -> None:
        options = full_options(
            retry=_RETRY, token_cache=_TOKEN_CACHE, private_key=MISSING_KEY, cert_dn=CERT_DN
        )
        assert "client" not in session_kwargs(**options, max_concurrent=4)

    def test_is_keyword_only(self, session_kwargs: SessionKwargs) -> None:
        with pytest.raises(TypeError):
            session_kwargs(HOST, "admin", "pw")
        parameters = inspect.signature(session_kwargs).parameters.values()
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)

    def test_returns_a_fresh_dict(self, session_kwargs: SessionKwargs) -> None:
        first = session_kwargs(**full_options())
        first["client"] = object()
        second = session_kwargs(**full_options())
        assert first is not second
        assert "client" not in second


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_clients_hand_their_session_exactly_the_helper_mapping(
    flavour: Flavour, session_kwargs: SessionKwargs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both clients go through the helper; the session gets its mapping, plus ``client``."""
    produced: list[dict[str, object]] = []

    def recording(**kwargs: object) -> dict[str, object]:
        result = session_kwargs(**kwargs)
        produced.append(dict(result))
        return result

    monkeypatch.setattr(facade, "_session_kwargs", recording)
    options: Options = {
        "host": HOST,
        "username": "admin",
        "password": _SECRET,
        "retry": _RETRY,
        "private_key": MISSING_KEY,
        "cert_dn": CERT_DN,
    }
    handed = await handed_off(monkeypatch, build(flavour, options))
    assert len(produced) == 1
    assert handed == produced[0]

    produced.clear()
    client = http_client(flavour)
    try:
        injected = await handed_off(monkeypatch, with_client(flavour, client, options))
    finally:
        await close_http(client)
    assert len(produced) == 1
    assert injected.pop("client") is client
    assert injected == produced[0]


# ══ 5. Parity post-state: the session-building divergence is retired ══════════

FACADE_SOURCE = Path(facade.__file__)

# The divergences the extraction leaves — each one is about the async-only
# surface (max_concurrent, gather) or the flavour-specific error message.
# ``Niwaki.__enter__`` is gone: the session-building block is identical once the
# options come from the shared helper.
EXPECTED_FACADE_CLIENT_DIVERGENCES = frozenset(
    {
        "Niwaki.__init__",
        "Niwaki.connect",
        "Niwaki.gather",
        "Niwaki.max_concurrent",
        "Niwaki._sync_session",
    }
)


def test_session_building_no_longer_diverges() -> None:
    live = pair_divergences(PAIRS["facade_client"])
    assert "Niwaki.__enter__" not in live, (
        "__enter__/__aenter__ still build their session differently:\n" + live["Niwaki.__enter__"]
    )
    assert set(live) == EXPECTED_FACADE_CLIENT_DIVERGENCES


def test_the_enter_allowlist_entry_is_retired() -> None:
    parity = json.loads(ALLOWLIST_PATH.read_text())["parity"]
    assert set(parity["facade_client"]) == EXPECTED_FACADE_CLIENT_DIVERGENCES


_MAX_CONCURRENT_ARG = (
    re.compile(r",\s*max_concurrent(?:: int)?=[^,)]+"),
    re.compile(r"max_concurrent(?:: int)?=[^,)]+,\s*"),
)


def _without_max_concurrent(line: str) -> str:
    for pattern in _MAX_CONCURRENT_ARG:
        line = pattern.sub("", line)
    return line


def test_init_diverges_on_max_concurrent_only() -> None:
    """Whatever the extraction leaves in ``__init__``, only the async bound may differ.

    Green before and after: a guard that the extraction moves no other
    difference into the constructors.  A sync-only line must be an async line
    minus its ``max_concurrent`` argument (the signature, the helper call); an
    async-only line must be about ``max_concurrent``.
    """
    diff = pair_divergences(PAIRS["facade_client"])["Niwaki.__init__"].splitlines()
    sync_lines = {line[1:] for line in diff if line.startswith("-")}
    async_lines = {line[1:] for line in diff if line.startswith("+")}
    sync_only, async_only = sync_lines - async_lines, async_lines - sync_lines
    stripped_async = {_without_max_concurrent(line) for line in async_only}
    assert sync_only, "the sync signature has no max_concurrent: it must diverge"
    assert all(line in stripped_async for line in sync_only), sync_only
    assert all("max_concurrent" in line for line in async_only), async_only


def _string_constants(tree: ast.AST, text: str) -> list[ast.Constant]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and text in node.value
    ]


def _helper_node(tree: ast.Module) -> ast.FunctionDef:
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_session_kwargs"
    ]
    assert len(found) == 1, "facade.py must define _session_kwargs once, at module level"
    return found[0]


@pytest.mark.parametrize(
    "message",
    [CERT_MESSAGE, "max_concurrent must be >= 1, got "],
    ids=["certificate-pairing", "max-concurrent-bound"],
)
def test_each_validation_rule_has_one_definition_in_the_helper(message: str) -> None:
    """The rule lives in ``_session_kwargs`` and nowhere else in ``facade.py``."""
    tree = ast.parse(FACADE_SOURCE.read_text())
    assert len(_string_constants(tree, message)) == 1
    assert len(_string_constants(_helper_node(tree), message)) == 1
