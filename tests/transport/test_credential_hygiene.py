"""Credential hygiene: the APIC password is never held in plaintext.

A session lives for the whole life of a process, and everything that dumps
object state — an error tracker capturing attributes, a debugger's variable
pane, ``vars(session)`` pasted into a bug report, a stray ``log.debug(obj.__dict__)``
— would publish a password stored as a plain ``str`` attribute.  The contract
pinned here: the password may only exist in clear inside the aaaLogin request
body, built at the moment it is sent.  Everywhere else it is held behind an
opaque secret wrapper.

What is pinned, for both :class:`~niwaki.transport.session.ApicSession` and
:class:`~niwaki.transport.session_async.AsyncApicSession`, and for both façades
(:class:`~niwaki.Niwaki`, :class:`~niwaki.AsyncNiwaki`):

- **No plaintext in state** — a deep, recursive walk of the instance state
  (constructed, logged in, re-logged in, built through ``with_client``, built
  from ``APIC_PASSWORD``, signed with a certificate) finds no ``str``/``bytes``
  equal to or containing the password.
- **The secret still works** — every login (first login, re-login on an
  expired token, re-login on a mid-session 401) sends the exact plaintext
  password in ``aaaUser.attributes.pwd``.
- **Behaviour is unchanged** — explicit password beats the environment; an
  empty password falls back to ``APIC_PASSWORD``; neither present raises
  ``KeyError``; a certificate session needs no password at all.
- **Nothing else leaks it** — ``repr``/``str``, log records, the error of a
  rejected login, the on-disk token cache.
- **A failed login carries no request** — a connect error, timeout or TLS
  failure on aaaLogin is raised as the same typed niwaki error, but with its
  chain severed (``__cause__`` and ``__context__`` both ``None``,
  ``__suppress_context__`` true): the underlying httpx error keeps the
  request it failed on, and that request's body *is* the password.  The
  message still names the failure (httpx class name and its text).
- **A ``SecretStr`` is accepted as the password** — by both session
  constructors, both ``with_client``, both façade constructors and both
  ``connect()``; it sends its exact plaintext and is never wrapped twice.
  The façades hand their ``SecretStr`` straight to the session, so no plain
  ``password`` keyword is built on the way.

The state walker (:func:`find_plaintext`)
-----------------------------------------

The walker is the instrument every "not held" assertion depends on, so it is
tested on its own (:class:`TestTheWalker`) and every session/façade scenario
has a *positive control* proving the walk really reaches the state it claims
to inspect (the username, and the session token inside the httpx cookie jar).

Bounds, chosen to be thorough without wandering out of the object graph:

- **Descends into** ``dict`` and ``MappingProxyType`` (keys and values),
  ``list``/``tuple``/``set``/``frozenset``/``deque``, any object's
  ``__dict__`` and ``__slots__`` (read through the slot descriptors, never
  through ``__getattr__``), function closures/defaults, bound methods (Python
  *and* builtin — ``[pw].copy`` keeps its list alive through ``__self__``),
  ``functools.partial``, and exceptions (``args``, ``__cause__``,
  ``__context__``, ``__notes__``, plus their ``__dict__`` — where an httpx
  error keeps the request it failed on).  httpx clients are *not*
  special-cased.
- **Matches encoded forms too** — not only the raw password in a ``str`` or
  in ``bytes``/``bytearray``/``memoryview`` (UTF-8), but also its base64, the
  base64 of ``user:password`` for each username given (``httpx.BasicAuth``
  stores exactly ``"Basic " + b64("user:password")``), and its percent- and
  form-encoded spellings (a password in a URL or a form body).
- **Never descends into** modules, classes, event loops, loggers, weak
  references, frames, code objects, generators and coroutines — each either
  leads to the whole interpreter or holds no instance state of ours.
  Tracebacks in particular are *not* walked: frame locals (the ``payload``
  dict inside ``login()`` while the request is in flight) are explicitly out
  of scope.
- **Opaque secret wrappers** — any object whose *type* defines a callable
  ``get_secret_value`` (``pydantic.SecretStr``, ``pydantic.Secret[...]``, or a
  hand-written wrapper following the same protocol) is a leaf: its insides are
  not walked, but its ``repr()`` and ``str()`` are checked and must not reveal
  the value.  This is the sanctioned way to hold the password.
- **Depth** is capped at :data:`MAX_DEPTH` levels below the root (the deepest
  real state — a cookie value inside the httpx cookie jar — sits well inside
  it), and the walk fails loudly past :data:`MAX_NODES` visited objects rather
  than returning a silently partial answer.
- **Cycles** are cut by object identity.

What is deliberately *not* pinned: the session token itself is plaintext by
necessity (the cookie jar must carry it), and the local ``payload`` dict inside
``login()`` holds the password for the duration of the request — a frame local
is not instance state.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import functools
import inspect
import json
import logging
import ssl
import traceback
import types
import urllib.parse
import weakref
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from niwaki import AsyncNiwaki, Niwaki, exceptions
from niwaki.transport._token import TokenState
from niwaki.transport._token_cache import TokenCache
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload, ok

# ── Constants ─────────────────────────────────────────────────────────────────

USERNAME = "op-zz9-user"
PASSWORD = "s3cr3t-Pa55-zz9"  # pragma: allowlist secret — distinctive test value
ENV_PASSWORD = "env-s3cr3t-Pa55-zz9"  # pragma: allowlist secret — distinctive test value
OTHER_PASSWORD = "0ther-Pa55-zz9"  # pragma: allowlist secret — distinctive test value
#: Has characters a URL or form body must escape, unlike :data:`PASSWORD`.
SPECIAL_PASSWORD = "p@ss w/rd:zz9%"  # pragma: allowlist secret — distinctive test value
TOKEN = "tok-zz9-marker"
CERT_DN = "uni/userext/user-admin/usercert-c"

GET_PATH = "/api/class/fvTenant.json"
GET_URL = f"{HOST}{GET_PATH}"

#: Levels below the root the walker descends.  The deepest legitimate state —
#: a cookie value in ``session._client._cookies.jar._cookies[domain][path][name]``
#: — sits at about eight.
MAX_DEPTH = 12

#: Visited-object budget.  Exceeding it fails the walk rather than truncating it.
MAX_NODES = 200_000

Flavour = Literal["sync", "async"]
AnySession = ApicSession | AsyncApicSession
AnyFacade = Niwaki | AsyncNiwaki


# ── The state walker ──────────────────────────────────────────────────────────


class WalkBudgetExceededError(AssertionError):
    """The walk visited more than :data:`MAX_NODES` objects.

    Raised instead of returning a partial answer: a scan that silently stopped
    early would let a leak through as a pass.
    """


#: Objects never walked into: each leads out of the instance graph (to the
#: interpreter, the loop, the logging tree) or carries no instance state.
_NEVER_DESCEND: tuple[type, ...] = (
    types.ModuleType,
    type,
    asyncio.AbstractEventLoop,
    logging.Logger,
    logging.Manager,
    weakref.ReferenceType,
    types.FrameType,
    types.CodeType,
    types.GeneratorType,
    types.CoroutineType,
    types.AsyncGeneratorType,
    types.TracebackType,
)

_LEAF_SCALARS: tuple[type, ...] = (int, float, complex, bool, type(None))


def _is_secret_wrapper(obj: object) -> bool:
    """Whether *obj* follows the opaque-secret protocol (``get_secret_value``).

    Looked up on the type, never the instance, so an object with a permissive
    ``__getattr__`` is not mistaken for a wrapper.
    """
    return callable(getattr(type(obj), "get_secret_value", None))


def _slot_values(obj: object) -> Iterator[tuple[str, object]]:
    """Yield ``(name, value)`` for every *set* slot of *obj*, across its MRO.

    Reads through the slot's member descriptor so a class-level
    ``__getattr__`` (which ``ManagedObject`` has, catalogue-backed) is never
    triggered by an unset slot.
    """
    for cls in type(obj).__mro__:
        slots: object = cls.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            names: tuple[str, ...] = (slots,)
        elif isinstance(slots, Iterable):
            names = tuple(str(name) for name in slots)
        else:
            continue
        for name in names:
            if name in ("__dict__", "__weakref__"):
                continue
            stored = (
                f"_{cls.__name__.lstrip('_')}{name}"
                if name.startswith("__") and not name.endswith("__")
                else name
            )
            descriptor = cls.__dict__.get(stored)
            if not isinstance(descriptor, types.MemberDescriptorType):
                continue
            try:
                yield stored, descriptor.__get__(obj, cls)
            except AttributeError:
                continue


def _instance_dict(obj: object) -> dict[str, object] | None:
    """Return *obj*'s ``__dict__`` without going through ``__getattr__``."""
    try:
        state = object.__getattribute__(obj, "__dict__")
    except (AttributeError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def needle_forms(needle: str, usernames: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Every spelling of *needle* the walker treats as "in clear".

    Args:
        needle: The secret.
        usernames: Login names to pair with it for the ``user:password``
            base64 form (HTTP Basic auth).

    Returns:
        ``(label, form)`` pairs, the raw form first with an empty label, then
        the encoded forms labelled ``" (base64)"``, ``" (base64 user:password)"``,
        ``" (percent-encoded)"``, ``" (form-encoded)"``.  A form identical to
        an earlier one (a password with nothing to escape percent-encodes to
        itself) is listed once, under the earlier label.
    """
    candidates = [
        ("", needle),
        (" (base64)", base64.b64encode(needle.encode()).decode()),
        *(
            (" (base64 user:password)", base64.b64encode(f"{user}:{needle}".encode()).decode())
            for user in usernames
        ),
        (" (percent-encoded)", urllib.parse.quote(needle, safe="")),
        (" (form-encoded)", urllib.parse.quote_plus(needle, safe="")),
    ]
    forms: list[tuple[str, str]] = []
    for label, form in candidates:
        if all(form != known for _, known in forms):
            forms.append((label, form))
    return forms


def find_plaintext(root: object, needle: str, usernames: Iterable[str] = ()) -> list[str]:
    """Walk *root*'s state and return every path where *needle* is in clear.

    Args:
        root: The object whose state is inspected (a session, a façade, an
            exception, any fixture object for the walker's own tests).
        needle: The secret to look for.  A ``str`` *containing* any of its
            :func:`needle_forms` counts, and so does a ``bytes``/
            ``bytearray``/``memoryview`` containing one's UTF-8 encoding.
        usernames: Login names for the ``base64(user:password)`` form.

    Returns:
        Human-readable paths (``root._password``,
        ``root.__cause__._request._content``) to every occurrence, suffixed
        with the form's label when it is an encoded one; empty when the
        secret is nowhere in clear.  A secret wrapper whose ``repr``/``str``
        reveals the value is reported as ``<path> (repr)``/``<path> (str)``.

    Raises:
        WalkBudgetExceededError: More than :data:`MAX_NODES` objects were
            visited — the walk refuses to answer rather than answer partially.

    Note:
        Exceptions are walked through their chain, never their traceback:
        frame locals are out of scope (see the module docstring).

    Example::

        assert find_plaintext(session, "hunter2", usernames=["admin"]) == []
    """
    forms = needle_forms(needle, usernames)
    raw_forms = [(label, form.encode()) for label, form in forms]
    found: list[str] = []
    seen: set[int] = set()
    visited = 0
    stack: list[tuple[object, str, int]] = [(root, "root", 0)]

    while stack:
        obj, path, depth = stack.pop()

        if isinstance(obj, str):
            found.extend(f"{path}{label}" for label, form in forms if form in obj)
            continue
        if isinstance(obj, bytes | bytearray | memoryview):
            data = bytes(obj)
            found.extend(f"{path}{label}" for label, form in raw_forms if form in data)
            continue
        if isinstance(obj, _LEAF_SCALARS + _NEVER_DESCEND):
            continue

        if id(obj) in seen:
            continue
        seen.add(id(obj))
        visited += 1
        if visited > MAX_NODES:
            raise WalkBudgetExceededError(
                f"walk exceeded {MAX_NODES} objects at {path}: extend the skip list"
            )

        if _is_secret_wrapper(obj):
            rendered = {"repr": repr(obj), "str": str(obj)}
            for kind, text in rendered.items():
                if any(form in text for _, form in forms):
                    found.append(f"{path} ({kind})")
            continue

        if depth >= MAX_DEPTH:
            continue
        below = depth + 1

        if isinstance(obj, dict | types.MappingProxyType):
            mapping: Any = obj
            for key, value in mapping.items():
                stack.append((key, f"{path}<key {key!r}>", below))
                stack.append((value, f"{path}[{key!r}]", below))
            continue
        if isinstance(obj, list | tuple | set | frozenset | collections.deque):
            items: Any = obj
            for index, item in enumerate(items):
                stack.append((item, f"{path}[{index}]", below))
            continue
        if isinstance(obj, types.FunctionType):
            for index, cell in enumerate(obj.__closure__ or ()):
                try:
                    contents = cell.cell_contents
                except ValueError:  # an empty cell
                    continue
                stack.append((contents, f"{path}.<closure {index}>", below))
            stack.append((obj.__defaults__, f"{path}.__defaults__", below))
            stack.append((obj.__kwdefaults__, f"{path}.__kwdefaults__", below))
            continue
        if isinstance(obj, types.MethodType):
            stack.append((obj.__self__, f"{path}.__self__", below))
            stack.append((obj.__func__, f"{path}.__func__", below))
            continue
        if isinstance(obj, types.BuiltinMethodType | types.MethodWrapperType):
            # A module-level builtin's __self__ is its module: skipped on pop.
            stack.append((obj.__self__, f"{path}.__self__", below))
            continue
        if isinstance(obj, functools.partial):
            partial: functools.partial[object] = obj
            stack.append((partial.func, f"{path}.func", below))
            stack.append((partial.args, f"{path}.args", below))
            stack.append((partial.keywords, f"{path}.keywords", below))
        if isinstance(obj, BaseException):
            # The chain, never the traceback: frame locals are out of scope.
            stack.append((obj.args, f"{path}.args", below))
            stack.append((obj.__cause__, f"{path}.__cause__", below))
            stack.append((obj.__context__, f"{path}.__context__", below))
            stack.append((getattr(obj, "__notes__", None), f"{path}.__notes__", below))

        state = _instance_dict(obj)
        if state is not None:
            for name, value in state.items():
                stack.append((value, f"{path}.{name}", below))
        for name, value in _slot_values(obj):
            stack.append((value, f"{path}.{name}", below))

    return sorted(found)


def assert_not_held(obj: object, needle: str = PASSWORD) -> None:
    """Fail, naming every location, if *obj* holds *needle* in any clear form.

    The ``user:password`` base64 form is checked against :data:`USERNAME`,
    the login name every scenario here uses.
    """
    leaks = find_plaintext(obj, needle, usernames=[USERNAME])
    assert not leaks, f"{type(obj).__name__} holds the password in plaintext at: {leaks}"


# ── Flavour adapters (one test body for sync and async) ──────────────────────


@pytest.fixture(params=["sync", "async"])
def flavour(request: pytest.FixtureRequest) -> Flavour:
    """Run the test once against the sync stack and once against the async one."""
    value: Flavour = request.param
    return value


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no ``APIC_*`` credential in the environment.

    ``tests/conftest.py`` loads the repository ``.env``, which may define a
    real ``APIC_PASSWORD`` — that would mask the fallback and missing-password
    behaviour pinned here.
    """
    monkeypatch.delenv("APIC_PASSWORD", raising=False)
    monkeypatch.delenv("APIC_USERNAME", raising=False)
    monkeypatch.delenv("APIC_HOST", raising=False)


#: What a test may hand in as ``password``.  The public signatures accept
#: ``SecretStr`` too (pinned by :class:`TestSecretStrIsAcceptedAsThePassword`);
#: the helpers forward it untyped so this module type-checks either way.
Password = str | SecretStr | None


def make_session(flavour: Flavour, password: Password = PASSWORD, **kwargs: Any) -> AnySession:
    """Build an unauthenticated session of the requested flavour."""
    secret: Any = password
    if flavour == "sync":
        return ApicSession(HOST, USERNAME, secret, **kwargs)
    return AsyncApicSession(HOST, USERNAME, secret, **kwargs)


def make_session_with_client(flavour: Flavour, password: Password = PASSWORD) -> AnySession:
    """Build a session through ``with_client`` with a fresh httpx client."""
    secret: Any = password
    if flavour == "sync":
        return ApicSession.with_client(httpx.Client(base_url=HOST), HOST, USERNAME, secret)
    return AsyncApicSession.with_client(httpx.AsyncClient(base_url=HOST), HOST, USERNAME, secret)


async def login(session: AnySession, *, use_cache: bool = True) -> None:
    """Log *session* in, awaiting when it is the async flavour."""
    result = session.login(use_cache=use_cache)
    if inspect.isawaitable(result):
        await result


async def ensure_token(session: AnySession) -> None:
    """Run the proactive token check, awaiting when async."""
    result = session._ensure_token()
    if inspect.isawaitable(result):
        await result


async def get(session: AnySession, path: str) -> list[dict[str, Any]]:
    """Issue an authenticated GET, awaiting when async."""
    result = session.get(path)
    if inspect.isawaitable(result):
        return await result
    return result


async def close(session: AnySession) -> None:
    """Close *session*, awaiting when async."""
    result = session.close()
    if inspect.isawaitable(result):
        await result


def make_facade(flavour: Flavour, password: Password = PASSWORD, **kwargs: Any) -> AnyFacade:
    """Build an unconnected façade of the requested flavour."""
    secret: Any = password
    if flavour == "sync":
        return Niwaki(HOST, USERNAME, secret, **kwargs)
    return AsyncNiwaki(HOST, USERNAME, secret, **kwargs)


def make_facade_with_client(flavour: Flavour, password: Password = PASSWORD) -> AnyFacade:
    """Build a façade through ``with_client`` with a fresh httpx client."""
    secret: Any = password
    if flavour == "sync":
        return Niwaki.with_client(httpx.Client(base_url=HOST), HOST, USERNAME, secret)
    return AsyncNiwaki.with_client(httpx.AsyncClient(base_url=HOST), HOST, USERNAME, secret)


async def enter(facade: AnyFacade) -> None:
    """Enter the façade's context manager (connect and log in)."""
    if isinstance(facade, Niwaki):
        facade.__enter__()
    else:
        await facade.__aenter__()


async def connect(flavour: Flavour, password: str | SecretStr = PASSWORD) -> AnyFacade:
    """Build and log in a façade through the ``connect()`` classmethod."""
    secret: Any = password
    if flavour == "sync":
        return Niwaki.connect(HOST, USERNAME, secret)
    return await AsyncNiwaki.connect(HOST, USERNAME, secret)


async def close_facade(facade: AnyFacade) -> None:
    """Close the façade, awaiting when async."""
    result = facade.close()
    if inspect.isawaitable(result):
        await result


def mock_login(httpx_mock: HTTPXMock, token: str = TOKEN) -> None:
    """Queue one successful aaaLogin response carrying *token*."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload(token))


def sent_passwords(httpx_mock: HTTPXMock) -> list[object]:
    """Return ``aaaUser.attributes.pwd`` from every captured aaaLogin request, in order."""
    bodies = [
        json.loads(request.content)
        for request in httpx_mock.get_requests(method="POST", url=LOGIN_URL)
    ]
    return [body["aaaUser"]["attributes"]["pwd"] for body in bodies]


def sent_usernames(httpx_mock: HTTPXMock) -> list[object]:
    """Return ``aaaUser.attributes.name`` from every captured aaaLogin request."""
    bodies = [
        json.loads(request.content)
        for request in httpx_mock.get_requests(method="POST", url=LOGIN_URL)
    ]
    return [body["aaaUser"]["attributes"]["name"] for body in bodies]


def expire(session: AnySession) -> None:
    """Replace the session token with one that expired ten seconds ago."""
    session._token_state = TokenState(
        token="expired",
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=10),
    )


# ── The walker's own tests ────────────────────────────────────────────────────


class _Box:
    """A plain object with a ``__dict__``."""

    def __init__(self, **attrs: object) -> None:
        self.__dict__.update(attrs)


class _Slotted:
    """An object with ``__slots__`` only, including a name-mangled slot."""

    __slots__ = ("__hidden", "visible")

    def __init__(self, visible: object, hidden: object) -> None:
        self.visible = visible
        self.__hidden = hidden


class _LeakyWrapper:
    """Follows the secret protocol but forgets to mask its ``repr``."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        """Return the wrapped value."""
        return self._value

    def __repr__(self) -> str:
        return f"_LeakyWrapper({self._value!r})"


class _AttrHungry:
    """Answers *any* attribute through ``__getattr__`` — like ``ManagedObject``."""

    __slots__ = ("unset",)

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the walker must not trigger __getattr__ ({name})")


class TestTheWalker:
    """The instrument behind every assertion below must itself be right."""

    def test_finds_a_plain_attribute(self) -> None:
        assert find_plaintext(_Box(pw=PASSWORD), PASSWORD) == ["root.pw"]

    def test_finds_a_substring(self) -> None:
        """A password embedded in a larger string (a header, a URL) is a leak."""
        assert find_plaintext(_Box(h=f"Basic x:{PASSWORD};"), PASSWORD) == ["root.h"]

    def test_finds_bytes_and_bytearray(self) -> None:
        box = _Box(a=PASSWORD.encode(), b=bytearray(b"pre" + PASSWORD.encode()))
        assert find_plaintext(box, PASSWORD) == ["root.a", "root.b"]

    def test_finds_through_nested_containers(self) -> None:
        box = _Box(outer=[{"k": ({PASSWORD},)}])
        assert find_plaintext(box, PASSWORD) == ["root.outer[0]['k'][0][0]"]

    def test_finds_a_dict_key(self) -> None:
        assert find_plaintext({PASSWORD: 1}, PASSWORD) == [f"root<key {PASSWORD!r}>"]

    def test_finds_slots_including_name_mangled_ones(self) -> None:
        paths = find_plaintext(_Slotted(PASSWORD, PASSWORD), PASSWORD)
        assert paths == ["root._Slotted__hidden", "root.visible"]

    def test_finds_closure_defaults_and_partials(self) -> None:
        secret = PASSWORD

        def closure() -> str:
            return secret

        def defaults(value: str = PASSWORD, *, kw: str = PASSWORD) -> str:
            return value + kw

        holder = _Box(c=closure, d=defaults, p=functools.partial(print, PASSWORD, sep=PASSWORD))
        paths = find_plaintext(holder, PASSWORD)
        assert "root.c.<closure 0>" in paths
        assert "root.d.__defaults__[0]" in paths
        assert "root.d.__kwdefaults__['kw']" in paths
        assert "root.p.args[0]" in paths
        assert "root.p.keywords['sep']" in paths

    def test_finds_through_a_bound_method(self) -> None:
        inner = _Box(pw=PASSWORD)
        hook = _Box(cb=inner.__init__)
        assert find_plaintext(hook, PASSWORD) == ["root.cb.__self__.pw"]

    def test_cycles_terminate(self) -> None:
        a = _Box()
        b = _Box(back=a, pw=PASSWORD)
        a.__dict__["fwd"] = b
        a.__dict__["me"] = [a]
        assert find_plaintext(a, PASSWORD) == ["root.fwd.pw"]

    def test_a_pydantic_secret_is_an_opaque_leaf(self) -> None:
        """``SecretStr`` keeps the value in its ``__dict__``; the protocol
        makes it a leaf, and its masked ``repr``/``str`` reveal nothing."""
        assert find_plaintext(_Box(pw=SecretStr(PASSWORD)), PASSWORD) == []

    def test_a_wrapper_whose_repr_reveals_the_value_is_a_leak(self) -> None:
        """``str()`` falls back to the leaky ``repr`` here, so both are reported."""
        assert find_plaintext(_Box(pw=_LeakyWrapper(PASSWORD)), PASSWORD) == [
            "root.pw (repr)",
            "root.pw (str)",
        ]

    def test_never_triggers_getattr(self) -> None:
        assert find_plaintext(_AttrHungry(), PASSWORD) == []

    def test_depth_bound(self) -> None:
        """State within :data:`MAX_DEPTH` is scanned; beyond it is not."""
        reachable: object = PASSWORD
        for _ in range(MAX_DEPTH):
            reachable = [reachable]
        assert find_plaintext(reachable, PASSWORD) == ["root" + "[0]" * MAX_DEPTH]

        unreachable: object = PASSWORD
        for _ in range(MAX_DEPTH + 1):
            unreachable = [unreachable]
        assert find_plaintext(unreachable, PASSWORD) == []

    def test_the_budget_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{__name__}.MAX_NODES", 3)
        with pytest.raises(WalkBudgetExceededError):
            find_plaintext(_Box(a=_Box(), b=_Box(), c=_Box()), PASSWORD)

    def test_does_not_wander_into_modules_or_classes(self) -> None:
        """A module global or a class attribute is not instance state."""
        fake_module = types.ModuleType("fake")
        fake_module.__dict__["pw"] = PASSWORD
        holder_cls = type("Holder", (), {"pw": PASSWORD})
        assert find_plaintext(_Box(m=fake_module, c=holder_cls), PASSWORD) == []


class TestTheWalkerSeesEncodedAndIndirectForms:
    """Blind spots a real leak would hide in: encodings, exception chains,
    read-only mappings, buffers, builtin bound methods."""

    def test_http_basic_auth_is_found(self) -> None:
        """``httpx.BasicAuth`` keeps only ``"Basic " + b64("user:password")``."""
        paths = find_plaintext(_Box(auth=httpx.BasicAuth("u", PASSWORD)), PASSWORD, usernames=["u"])
        assert paths == ["root.auth._auth_header (base64 user:password)"]

    def test_the_base64_of_the_password_alone_is_found(self) -> None:
        encoded = base64.b64encode(PASSWORD.encode())
        paths = find_plaintext(_Box(s=encoded.decode(), b=encoded), PASSWORD)
        assert paths == ["root.b (base64)", "root.s (base64)"]

    def test_a_percent_encoded_password_is_found(self) -> None:
        """A password with characters to escape, as it would sit in a URL."""
        url = f"https://apic.test/login?pw={urllib.parse.quote(SPECIAL_PASSWORD, safe='')}"
        paths = find_plaintext(_Box(url=url), SPECIAL_PASSWORD)
        assert paths == ["root.url (percent-encoded)"]

    def test_a_form_encoded_password_is_found(self) -> None:
        body = f"user=u&pwd={urllib.parse.quote_plus(SPECIAL_PASSWORD, safe='')}".encode()
        paths = find_plaintext(_Box(body=body), SPECIAL_PASSWORD)
        assert paths == ["root.body (form-encoded)"]

    def test_an_exception_cause_args_is_walked(self) -> None:
        error = RuntimeError("login failed")
        error.__cause__ = ValueError(PASSWORD)
        assert find_plaintext(error, PASSWORD) == ["root.__cause__.args[0]"]

    def test_an_exception_context_and_notes_are_walked(self) -> None:
        error = RuntimeError("login failed")
        error.__context__ = KeyError(PASSWORD)
        error.add_note(f"while sending {PASSWORD}")
        paths = find_plaintext(error, PASSWORD)
        assert paths == ["root.__context__.args[0]", "root.__notes__[0]"]

    def test_the_request_an_httpx_error_keeps_is_found(self) -> None:
        """The concrete leak: an httpx error remembers the request it failed
        on, and an aaaLogin request's body is the password."""
        failed = httpx.ConnectError("All connection attempts failed")
        failed.request = httpx.Request("POST", LOGIN_URL, json={"pwd": PASSWORD})
        wrapper = exceptions.ConnectionError("Cannot reach APIC")
        wrapper.__cause__ = failed
        assert "root.__cause__._request._content" in find_plaintext(wrapper, PASSWORD)

    def test_frame_locals_are_out_of_scope(self) -> None:
        """Tracebacks are not walked: a secret that only lives in a frame
        local of a raising function is not reported."""

        def fails() -> None:
            payload = {"pwd": PASSWORD}
            raise RuntimeError(f"failed with {len(payload)} field")

        with pytest.raises(RuntimeError) as excinfo:
            fails()
        assert excinfo.value.__traceback__ is not None
        assert find_plaintext(excinfo.value, PASSWORD) == []

    def test_a_mapping_proxy_is_walked(self) -> None:
        proxy = types.MappingProxyType({"k": PASSWORD})
        assert find_plaintext(_Box(view=proxy), PASSWORD) == ["root.view['k']"]

    def test_a_memoryview_is_walked(self) -> None:
        view = memoryview(b"pwd=" + PASSWORD.encode())
        assert find_plaintext(_Box(buf=view), PASSWORD) == ["root.buf"]

    def test_a_builtin_bound_method_keeps_its_self(self) -> None:
        """``[pw].copy`` is a builtin method; its list lives on in ``__self__``."""
        assert find_plaintext(_Box(cb=[PASSWORD].copy), PASSWORD) == ["root.cb.__self__[0]"]

    def test_a_password_with_nothing_to_escape_is_reported_once(self) -> None:
        """Its percent-encoding is itself: one path, under the raw label."""
        assert urllib.parse.quote(PASSWORD, safe="") == PASSWORD
        assert find_plaintext(_Box(pw=PASSWORD), PASSWORD) == ["root.pw"]


# ── Sessions: nothing in clear, before or after login ─────────────────────────


class TestSessionStateHoldsNoPlaintext:
    """The leak itself: a deep scan of a session finds no password."""

    async def test_a_constructed_session(self, flavour: Flavour) -> None:
        session = make_session(flavour)
        try:
            assert_not_held(session)
        finally:
            await close(session)

    async def test_a_logged_in_session(self, flavour: Flavour, httpx_mock: HTTPXMock) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
            assert_not_held(session)
        finally:
            await close(session)

    async def test_a_session_built_with_client(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session_with_client(flavour)
        mock_login(httpx_mock)
        try:
            before = find_plaintext(session, PASSWORD)
            await login(session)
            after = find_plaintext(session, PASSWORD)
        finally:
            await close(session)
        assert (before, after) == ([], [])

    async def test_a_password_read_from_the_environment(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        session = make_session(flavour, password=None)
        mock_login(httpx_mock)
        try:
            before = find_plaintext(session, ENV_PASSWORD)
            await login(session)
            after = find_plaintext(session, ENV_PASSWORD)
        finally:
            await close(session)
        assert (before, after) == ([], [])

    async def test_after_a_relogin_on_an_expired_token(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        mock_login(httpx_mock, token="fresh")
        try:
            await login(session)
            expire(session)
            await ensure_token(session)
            assert_not_held(session)
        finally:
            await close(session)

    async def test_a_session_with_a_token_cache(
        self, flavour: Flavour, httpx_mock: HTTPXMock, tmp_path: Path
    ) -> None:
        session = make_session(flavour, token_cache=TokenCache(tmp_path))
        mock_login(httpx_mock)
        try:
            await login(session)
            assert_not_held(session)
        finally:
            await close(session)


class TestTheWalkReachesSessionState:
    """Positive controls: a green "not held" means something only if the walk
    demonstrably reaches the session's attributes and the httpx client."""

    async def test_the_username_is_found(self, flavour: Flavour) -> None:
        session = make_session(flavour)
        try:
            assert "root._username" in find_plaintext(session, USERNAME)
        finally:
            await close(session)

    async def test_the_token_is_found_inside_the_httpx_cookie_jar(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
            paths = find_plaintext(session, TOKEN)
            assert any(path.startswith("root._token_state") for path in paths), paths
            assert any(path.startswith("root._client.") for path in paths), paths
        finally:
            await close(session)


class TestSessionRendering:
    """``repr``/``str`` are what error trackers and log lines capture first."""

    async def test_repr_and_str_do_not_reveal_the_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            for rendered in (repr(session), str(session)):
                assert PASSWORD not in rendered
            await login(session)
            for rendered in (repr(session), str(session)):
                assert PASSWORD not in rendered
        finally:
            await close(session)


# ── Sessions: the secret still works ──────────────────────────────────────────


class TestLoginStillSendsThePassword:
    """Wrapping the password must not change one byte of what the APIC sees."""

    async def test_login_sends_the_exact_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD]
        assert sent_usernames(httpx_mock) == [USERNAME]

    async def test_the_password_is_a_plain_json_string(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        """Not a masked ``"**********"``, not a nested object, not ``null``."""
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        request = httpx_mock.get_request(method="POST", url=LOGIN_URL)
        assert request is not None
        assert f'"pwd":"{PASSWORD}"' in request.content.decode().replace(" ", "")

    async def test_with_client_login_sends_the_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session_with_client(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_the_environment_password_is_sent(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        session = make_session(flavour, password=None)
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [ENV_PASSWORD]

    async def test_relogin_on_an_expired_token_resends_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        mock_login(httpx_mock, token="fresh")
        try:
            await login(session)
            expire(session)
            await ensure_token(session)
            assert session._token_state is not None
            assert session._token_state.token == "fresh"
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD, PASSWORD]

    async def test_relogin_on_a_mid_session_401_resends_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=GET_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "403", "text": "denied"}}}]},
        )
        mock_login(httpx_mock, token="fresh")
        httpx_mock.add_response(method="GET", url=GET_URL, json=ok())
        try:
            await login(session)
            assert await get(session, GET_PATH) == []
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD, PASSWORD]

    async def test_a_forced_relogin_bypassing_the_cache_resends_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock, tmp_path: Path
    ) -> None:
        """``login(use_cache=False)`` is the recovery primitive: it must still
        have the password to send long after construction."""
        session = make_session(flavour, token_cache=TokenCache(tmp_path))
        mock_login(httpx_mock)
        mock_login(httpx_mock, token="fresh")
        try:
            await login(session)
            await login(session, use_cache=False)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD, PASSWORD]


# ── Sessions: resolution behaviour is unchanged ───────────────────────────────


class TestPasswordResolutionIsUnchanged:
    """Explicit beats environment; empty falls back; missing raises ``KeyError``."""

    async def test_an_explicit_password_wins_over_the_environment(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", OTHER_PASSWORD)
        session = make_session(flavour)
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_an_empty_password_falls_back_to_the_environment(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        session = make_session(flavour, password="")
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [ENV_PASSWORD]

    @pytest.mark.parametrize("password", [None, ""], ids=["none", "empty"])
    def test_no_password_and_no_environment_raises_key_error(
        self, flavour: Flavour, password: str | None
    ) -> None:
        with pytest.raises(KeyError, match="APIC_PASSWORD"):
            make_session(flavour, password=password)


# ── Sessions: nothing else echoes it ──────────────────────────────────────────


class TestNoOtherChannelEchoesThePassword:
    """Logs, errors and the on-disk cache are dumps too."""

    async def test_no_log_record_carries_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = make_session(flavour)
        mock_login(httpx_mock)
        mock_login(httpx_mock, token="fresh")
        caplog.set_level(logging.DEBUG)
        try:
            await login(session)
            expire(session)
            await ensure_token(session)
        finally:
            await close(session)
        # Positive control: httpx logs every request at INFO, so a capture
        # that saw nothing would make the loop below vacuous.
        assert any(record.name == "httpx" for record in caplog.records), caplog.records
        for record in caplog.records:
            assert PASSWORD not in record.getMessage()
            assert PASSWORD not in repr(record.args)

    async def test_a_rejected_login_does_not_echo_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour)
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "401", "text": "bad creds"}}}]},
        )
        try:
            with pytest.raises(exceptions.LoginError) as excinfo:
                await login(session)
        finally:
            await close(session)
        error = excinfo.value
        assert PASSWORD not in str(error)
        assert PASSWORD not in repr(error)
        # Raised from a status code, not from an httpx error: nothing chained
        # that could carry ``resp.request.content``.
        assert error.__cause__ is None
        assert error.__context__ is None
        assert_not_held(error)
        assert PASSWORD not in "".join(traceback.format_exception(error))

    async def test_the_token_cache_file_never_holds_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock, tmp_path: Path
    ) -> None:
        session = make_session(flavour, token_cache=TokenCache(tmp_path))
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        files = [path for path in tmp_path.rglob("*") if path.is_file()]
        assert files, "the cache wrote nothing: the guard would be vacuous"
        for path in files:
            assert PASSWORD.encode() not in path.read_bytes()


# ── Certificate (X.509) sessions ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def key_pem(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway unencrypted RSA key on disk (skips without ``niwaki[x509]``)."""
    pytest.importorskip("cryptography", reason="needs niwaki[x509]")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("x509-hygiene") / "admin.key"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


def _pem_body_fragment(key_pem: Path) -> str:
    """A 48-character run of the key's base64 body — unique to this key."""
    lines = [line for line in key_pem.read_text().splitlines() if "-----" not in line]
    return "".join(lines)[64:112]


class TestCertificateSessions:
    """A signed session sends no password, and holds no key material in clear."""

    async def test_no_password_and_no_environment_is_fine(
        self, flavour: Flavour, key_pem: Path
    ) -> None:
        """The ``password or ""`` branch: CI has no password to provide."""
        session = make_session(flavour, password=None, private_key=key_pem, cert_dn=CERT_DN)
        try:
            await login(session)  # no mock queued: any request would fail the test
            assert session._token_state is None
        finally:
            await close(session)

    async def test_an_empty_password_does_not_consult_the_environment(
        self, flavour: Flavour, key_pem: Path
    ) -> None:
        session = make_session(flavour, password="", private_key=key_pem, cert_dn=CERT_DN)
        await close(session)

    async def test_a_password_given_anyway_is_not_held(
        self, flavour: Flavour, key_pem: Path
    ) -> None:
        session = make_session(flavour, private_key=key_pem, cert_dn=CERT_DN)
        try:
            assert_not_held(session)
        finally:
            await close(session)

    async def test_the_private_key_material_is_not_held_in_clear(
        self, flavour: Flavour, key_pem: Path
    ) -> None:
        fragment = _pem_body_fragment(key_pem)
        assert len(fragment) == 48
        session = make_session(flavour, password=None, private_key=key_pem, cert_dn=CERT_DN)
        try:
            assert_not_held(session, fragment)
            assert "root._cert_dn" in find_plaintext(session, CERT_DN)  # positive control
        finally:
            await close(session)

    async def test_a_cert_facade_given_a_password_does_not_hold_it(
        self, flavour: Flavour, key_pem: Path
    ) -> None:
        facade = make_facade(flavour, private_key=key_pem, cert_dn=CERT_DN)
        try:
            before = find_plaintext(facade, PASSWORD)
            await enter(facade)  # signed: no login request is made
            after = find_plaintext(facade, PASSWORD)
            key_material = find_plaintext(facade, _pem_body_fragment(key_pem))
        finally:
            await close_facade(facade)
        assert (before, after, key_material) == ([], [], [])


# ── Façades ───────────────────────────────────────────────────────────────────


class TestFacadeStateHoldsNoPlaintext:
    """``Niwaki``/``AsyncNiwaki`` keep their constructor arguments until entry;
    the password must not be one of them in clear."""

    async def test_an_unconnected_facade(self, flavour: Flavour) -> None:
        facade = make_facade(flavour)
        try:
            assert_not_held(facade)
        finally:
            await close_facade(facade)

    async def test_a_facade_entered_as_a_context_manager(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade(flavour)
        mock_login(httpx_mock)
        try:
            await enter(facade)
            assert_not_held(facade)
        finally:
            await close_facade(facade)

    async def test_a_facade_built_by_connect(self, flavour: Flavour, httpx_mock: HTTPXMock) -> None:
        mock_login(httpx_mock)
        facade = await connect(flavour)
        try:
            assert_not_held(facade)
        finally:
            await close_facade(facade)

    async def test_a_facade_built_with_client(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade_with_client(flavour)
        mock_login(httpx_mock)
        try:
            before = find_plaintext(facade, PASSWORD)
            await enter(facade)
            after = find_plaintext(facade, PASSWORD)
        finally:
            await close_facade(facade)
        assert (before, after) == ([], [])

    async def test_a_facade_reading_the_environment(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        facade = make_facade(flavour, password=None)
        mock_login(httpx_mock)
        try:
            await enter(facade)
            assert_not_held(facade, ENV_PASSWORD)
        finally:
            await close_facade(facade)

    async def test_repr_and_str_do_not_reveal_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade(flavour)
        mock_login(httpx_mock)
        try:
            assert PASSWORD not in repr(facade)
            assert PASSWORD not in str(facade)
            await enter(facade)
            assert PASSWORD not in repr(facade)
            assert PASSWORD not in str(facade)
        finally:
            await close_facade(facade)


class TestFacadeBehaviourIsUnchanged:
    """Positive control, payload and error timing for the façades."""

    async def test_the_walk_reaches_the_facade_and_its_session(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade(flavour)
        mock_login(httpx_mock)
        try:
            await enter(facade)
            paths = find_plaintext(facade, USERNAME)
            assert "root._username" in paths
            assert "root._session._username" in paths
        finally:
            await close_facade(facade)

    async def test_entry_sends_the_exact_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade(flavour)
        mock_login(httpx_mock)
        try:
            await enter(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_connect_sends_the_exact_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        mock_login(httpx_mock)
        facade = await connect(flavour)
        await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_with_client_sends_the_exact_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade_with_client(flavour)
        mock_login(httpx_mock)
        try:
            await enter(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_the_environment_password_is_sent(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        facade = make_facade(flavour, password=None)
        mock_login(httpx_mock)
        try:
            await enter(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [ENV_PASSWORD]

    async def test_a_missing_password_still_raises_key_error_at_entry(
        self, flavour: Flavour
    ) -> None:
        """Construction stays lazy; the session raises when it is built."""
        facade = make_facade(flavour, password=None)
        try:
            with pytest.raises(KeyError, match="APIC_PASSWORD"):
                await enter(facade)
        finally:
            await close_facade(facade)


# ── A failed login carries no request (item: severed exception chain) ────────


class LoginFailure:
    """One way the aaaLogin request can fail in transport, and what must come of it.

    Attributes:
        name: Test id.
        build: Builds a fresh httpx exception (pytest-httpx then attaches the
            request it failed on, as httpx itself does).
        expected: The niwaki exception type raised today — and still.
        detail: Picks, from the httpx exception, the text the message must
            still carry (its own ``str()``, or the SSL error's for TLS).
    """

    def __init__(
        self,
        name: str,
        build: Callable[[], httpx.TransportError],
        expected: type[exceptions.NiwakiError],
        detail: Callable[[httpx.TransportError], str],
    ) -> None:
        self.name = name
        self.build = build
        self.expected = expected
        self.detail = detail

    def __repr__(self) -> str:
        return self.name


def _tls_failure() -> httpx.TransportError:
    """A connect error caused by certificate verification, as httpx raises it."""
    error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed: zz9-self-signed")
    return error


def _cause_text(error: httpx.TransportError) -> str:
    return str(error.__cause__)


LOGIN_FAILURES = [
    LoginFailure(
        "connect-error",
        lambda: httpx.ConnectError("All connection attempts failed"),
        exceptions.ConnectionError,
        str,
    ),
    LoginFailure(
        "connect-timeout",
        lambda: httpx.ConnectTimeout("timed out after 30s (zz9)"),
        exceptions.TimeoutError,
        str,
    ),
    LoginFailure("tls-error", _tls_failure, exceptions.TLSError, _cause_text),
]


@pytest.fixture(params=LOGIN_FAILURES, ids=lambda failure: failure.name)
def login_failure(request: pytest.FixtureRequest) -> LoginFailure:
    """Each transport failure the login path maps to a typed niwaki error."""
    failure: LoginFailure = request.param
    return failure


def queue_login_failure(httpx_mock: HTTPXMock, failure: LoginFailure) -> httpx.TransportError:
    """Make the next aaaLogin request raise *failure*'s httpx error; return it."""
    error = failure.build()
    httpx_mock.add_exception(error, method="POST", url=LOGIN_URL)
    return error


async def failed_session_login(
    flavour: Flavour, httpx_mock: HTTPXMock, failure: LoginFailure
) -> tuple[BaseException, httpx.TransportError]:
    """Log a fresh session in against *failure*; return (raised, underlying)."""
    underlying = queue_login_failure(httpx_mock, failure)
    session = make_session(flavour)
    try:
        with pytest.raises(exceptions.NiwakiError) as excinfo:
            await login(session)
    finally:
        await close(session)
    return excinfo.value, underlying


def assert_chain_severed(error: BaseException) -> None:
    """The decided contract: nothing chained, and the context suppressed.

    ``raise ... from None`` inside the ``except`` block is *not* enough: it
    sets ``__suppress_context__`` but Python still records ``__context__``,
    and an error tracker walking ``__context__`` reaches the request anyway.
    """
    assert error.__cause__ is None, f"__cause__ is {error.__cause__!r}"
    assert error.__context__ is None, f"__context__ is {error.__context__!r}"
    assert error.__suppress_context__ is True


class TestALoginTransportFailureCarriesNoRequest:
    """The httpx error a failed login raises keeps its request, whose body is
    the password.  The typed niwaki error must not chain to it."""

    def test_the_underlying_httpx_error_does_hold_the_password(
        self, login_failure: LoginFailure
    ) -> None:
        """Why the chain must go: ``.request.content`` is the aaaLogin body."""
        underlying = login_failure.build()
        underlying.request = httpx.Request(
            "POST", LOGIN_URL, json={"aaaUser": {"attributes": {"pwd": PASSWORD}}}
        )
        assert PASSWORD.encode() in underlying.request.content
        # ...while its own text does not — so naming it in the message is safe.
        assert PASSWORD not in str(underlying)
        assert PASSWORD not in login_failure.detail(underlying)

    async def test_the_exception_type_is_unchanged(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        raised, _ = await failed_session_login(flavour, httpx_mock, login_failure)
        assert type(raised) is login_failure.expected

    async def test_the_chain_is_severed(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        raised, _ = await failed_session_login(flavour, httpx_mock, login_failure)
        assert_chain_severed(raised)

    async def test_nothing_reachable_from_the_error_holds_the_password(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        raised, _ = await failed_session_login(flavour, httpx_mock, login_failure)
        assert_not_held(raised)

    async def test_the_message_still_carries_the_failure_text(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        raised, underlying = await failed_session_login(flavour, httpx_mock, login_failure)
        assert login_failure.detail(underlying) in str(raised)

    async def test_the_message_names_the_httpx_failure_class(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        """With the chain gone, the class name is the only record of which
        httpx failure it was (``ConnectTimeout`` versus ``ReadTimeout``...)."""
        raised, underlying = await failed_session_login(flavour, httpx_mock, login_failure)
        assert type(underlying).__name__ in str(raised)

    async def test_a_formatted_traceback_never_shows_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        raised, _ = await failed_session_login(flavour, httpx_mock, login_failure)
        assert PASSWORD not in "".join(traceback.format_exception(raised))

    async def test_a_relogin_that_fails_in_transport_carries_nothing(
        self, flavour: Flavour, httpx_mock: HTTPXMock, login_failure: LoginFailure
    ) -> None:
        """Re-login goes through ``login()``; whatever surfaces from it, the
        walk over the error and its chain finds no password."""
        mock_login(httpx_mock)
        session = make_session(flavour)
        try:
            await login(session)
            expire(session)
            queue_login_failure(httpx_mock, login_failure)
            with pytest.raises(exceptions.NiwakiError) as excinfo:
                await ensure_token(session)
        finally:
            await close(session)
        assert_not_held(excinfo.value)


class TestAFacadeLoginTransportFailureCarriesNoRequest:
    """The façades re-raise the session's error untouched — so it must
    already be clean, through ``__enter__``/``__aenter__`` and ``connect()``."""

    @pytest.mark.parametrize("entry", ["enter", "connect"])
    async def test_the_type_is_unchanged_and_the_chain_severed(
        self,
        flavour: Flavour,
        httpx_mock: HTTPXMock,
        login_failure: LoginFailure,
        entry: str,
    ) -> None:
        underlying = queue_login_failure(httpx_mock, login_failure)
        with pytest.raises(exceptions.NiwakiError) as excinfo:
            if entry == "connect":
                await connect(flavour)
            else:
                facade = make_facade(flavour)
                try:
                    await enter(facade)
                finally:
                    await close_facade(facade)
        raised = excinfo.value
        assert type(raised) is login_failure.expected
        assert login_failure.detail(underlying) in str(raised)
        assert_chain_severed(raised)
        assert_not_held(raised)


# ── SecretStr is accepted as the password ─────────────────────────────────────


def assert_not_double_wrapped(obj: object) -> None:
    """No top-level secret wrapper of *obj* wraps another secret wrapper."""
    state = _instance_dict(obj) or {}
    for name, value in state.items():
        if _is_secret_wrapper(value):
            inner: object = value.get_secret_value()  # pyright: ignore[reportAttributeAccessIssue]
            assert not _is_secret_wrapper(inner), f"{name} wraps a {type(inner).__name__}"
            assert isinstance(inner, str), f"{name} unwraps to {type(inner).__name__}"


_SECRET_SIGNATURES: list[tuple[str, Callable[..., object]]] = [
    ("ApicSession.__init__", ApicSession.__init__),
    ("AsyncApicSession.__init__", AsyncApicSession.__init__),
    ("ApicSession.with_client", ApicSession.with_client),
    ("AsyncApicSession.with_client", AsyncApicSession.with_client),
    ("Niwaki.__init__", Niwaki.__init__),
    ("AsyncNiwaki.__init__", AsyncNiwaki.__init__),
    ("Niwaki.connect", Niwaki.connect),
    ("AsyncNiwaki.connect", AsyncNiwaki.connect),
    ("Niwaki.with_client", Niwaki.with_client),
    ("AsyncNiwaki.with_client", AsyncNiwaki.with_client),
]


# Unconsumed responses are not a failure here: each test asserts the exact
# sequence of logins sent, and a login that crashes before sending must
# surface as that test's failure, not as a teardown error.
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
class TestSecretStrIsAcceptedAsThePassword:
    """A caller who already holds the password as ``SecretStr`` must not have
    to unwrap it (today: ``TypeError: Object of type SecretStr is not JSON
    serializable`` at login)."""

    @pytest.mark.parametrize(
        "function", [fn for _, fn in _SECRET_SIGNATURES], ids=[n for n, _ in _SECRET_SIGNATURES]
    )
    def test_the_public_signature_admits_it(self, function: Callable[..., object]) -> None:
        annotation = inspect.signature(function).parameters["password"].annotation
        assert "SecretStr" in str(annotation), f"password is annotated {annotation!r}"

    async def test_a_session_sends_its_exact_plaintext(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session(flavour, password=SecretStr(PASSWORD))
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_a_session_does_not_wrap_it_twice(self, flavour: Flavour) -> None:
        session = make_session(flavour, password=SecretStr(PASSWORD))
        try:
            assert_not_double_wrapped(session)
            assert_not_held(session)
        finally:
            await close(session)

    async def test_a_with_client_session_sends_it(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        session = make_session_with_client(flavour, password=SecretStr(PASSWORD))
        mock_login(httpx_mock)
        try:
            await login(session)
            assert_not_double_wrapped(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_a_relogin_resends_it(self, flavour: Flavour, httpx_mock: HTTPXMock) -> None:
        session = make_session(flavour, password=SecretStr(PASSWORD))
        mock_login(httpx_mock)
        mock_login(httpx_mock, token="fresh")
        try:
            await login(session)
            expire(session)
            await ensure_token(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [PASSWORD, PASSWORD]

    async def test_an_empty_secret_behaves_like_an_empty_string(
        self, flavour: Flavour, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
    ) -> None:
        """``SecretStr("")`` falls back to ``APIC_PASSWORD``, exactly as ``""``."""
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        session = make_session(flavour, password=SecretStr(""))
        mock_login(httpx_mock)
        try:
            await login(session)
        finally:
            await close(session)
        assert sent_passwords(httpx_mock) == [ENV_PASSWORD]

    def test_an_empty_secret_without_the_environment_raises_key_error(
        self, flavour: Flavour
    ) -> None:
        with pytest.raises(KeyError, match="APIC_PASSWORD"):
            make_session(flavour, password=SecretStr(""))

    async def test_a_cert_session_accepts_it(self, flavour: Flavour, key_pem: Path) -> None:
        session = make_session(
            flavour, password=SecretStr(PASSWORD), private_key=key_pem, cert_dn=CERT_DN
        )
        try:
            await login(session)  # signed: no request
            assert_not_double_wrapped(session)
            assert_not_held(session)
        finally:
            await close(session)

    async def test_a_facade_sends_its_exact_plaintext(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade(flavour, password=SecretStr(PASSWORD))
        mock_login(httpx_mock)
        try:
            assert_not_double_wrapped(facade)
            await enter(facade)
            assert_not_held(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_connect_sends_its_exact_plaintext(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        mock_login(httpx_mock)
        facade = await connect(flavour, password=SecretStr(PASSWORD))
        try:
            assert_not_held(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]

    async def test_a_with_client_facade_sends_its_exact_plaintext(
        self, flavour: Flavour, httpx_mock: HTTPXMock
    ) -> None:
        facade = make_facade_with_client(flavour, password=SecretStr(PASSWORD))
        mock_login(httpx_mock)
        try:
            await enter(facade)
        finally:
            await close_facade(facade)
        assert sent_passwords(httpx_mock) == [PASSWORD]


# ── The façade hands its secret to the session, never a plain str ─────────────


class _SessionBuilt(Exception):
    """Stops a façade right after it built its session's arguments."""


@pytest.fixture
def received_passwords(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Spy on both session classes as the façade module sees them.

    Each construction records the ``password`` it was handed (``None`` when
    omitted) and then stops the façade with :class:`_SessionBuilt` — so the
    assertion is about what crossed the boundary, independent of whether the
    session would then cope with it.
    """
    received: list[object] = []

    def record(args: tuple[object, ...], kwargs: dict[str, object]) -> None:
        received.append(kwargs["password"] if "password" in kwargs else (args[2:3] or [None])[0])
        raise _SessionBuilt

    class SpySession(ApicSession):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            record(args, kwargs)

    class AsyncSpySession(AsyncApicSession):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            record(args, kwargs)

    monkeypatch.setattr("niwaki.facade.ApicSession", SpySession)
    monkeypatch.setattr("niwaki.facade.AsyncApicSession", AsyncSpySession)
    return received


class TestTheFacadeHandsItsSecretStraightToTheSession:
    """Unwrapping in the façade builds a plain ``kwargs["password"]`` — one
    more place (a frame local, a debugger's view of ``kwargs``) to find it."""

    @pytest.mark.parametrize("given", ["str", "secret"])
    @pytest.mark.parametrize("built_by", ["constructor", "with_client", "connect"])
    async def test_the_session_receives_a_secret(
        self, flavour: Flavour, received_passwords: list[object], given: str, built_by: str
    ) -> None:
        password: str | SecretStr = PASSWORD if given == "str" else SecretStr(PASSWORD)
        with pytest.raises(_SessionBuilt):
            match built_by:
                case "connect":
                    await connect(flavour, password=password)
                case "with_client":
                    await enter(make_facade_with_client(flavour, password=password))
                case _:
                    await enter(make_facade(flavour, password=password))
        assert len(received_passwords) == 1
        handed = received_passwords[0]
        assert isinstance(handed, SecretStr), f"the session was handed a {type(handed).__name__}"
        assert handed.get_secret_value() == PASSWORD

    async def test_no_password_is_handed_on_as_none(
        self, flavour: Flavour, received_passwords: list[object]
    ) -> None:
        """The environment fallback stays the session's job, at entry."""
        with pytest.raises(_SessionBuilt):
            await enter(make_facade(flavour, password=None))
        assert received_passwords == [None]
