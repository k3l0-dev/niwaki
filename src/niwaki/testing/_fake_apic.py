"""The in-memory APIC: an httpx handler that answers like a controller.

See :class:`FakeApic` for the contract.  The request flow is deliberately
short: log the request, answer the session endpoints inline, route ``/api/mo``
and ``/api/class``, refuse loudly whatever the fake cannot answer faithfully,
and apply injected failures before any write so a failed POST changes
nothing.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import httpx

from niwaki.testing._filter_eval import Predicate, compile_filter
from niwaki.testing._semantics import (
    DN,
    Prop,
    Refusal,
    configurable_props,
    expand_class,
    naming_props,
    prop,
)
from niwaki.testing._store import (
    UNIVERSE,
    Delete,
    Store,
    StoredObject,
    Write,
    plan_envelope,
    snapshot_writes,
)
from niwaki.types import JsonObject, JsonValue

if TYPE_CHECKING:
    from niwaki.facade import AsyncNiwaki, Niwaki
    from niwaki.snapshot import Snapshot

_LOGIN = "/api/aaaLogin.json"
_REFRESH = "/api/aaaRefresh.json"
_LOGOUT = "/api/aaaLogout.json"
_MO = "/api/mo/"
_CLASS = "/api/class/"
_SUFFIX = ".json"

#: The ``code`` of an error the fake raises itself (as opposed to an injected one).
_REFUSAL_CODE = "400"
#: Token lifetime announced at login — far above the SDK's 60 s refresh threshold.
_TOKEN_TTL = "600"
_USERNAME = "admin"
#: Placeholder credentials: every login succeeds, nothing is ever checked.
_PASSWORD = "fake-password"

_GET_PARAMS = frozenset(
    {
        "query-target",
        "target-subtree-class",
        "query-target-filter",
        "rsp-subtree",
        "rsp-subtree-class",
        "rsp-subtree-filter",
        "rsp-subtree-include",
        "rsp-prop-include",
        "order-by",
        "page",
        "page-size",
    }
)
_QUERY_TARGETS = frozenset({"self", "children", "subtree"})
_RSP_SUBTREES = frozenset({"no", "children", "full"})
_PROP_INCLUDES = frozenset({"all", "naming-only", "config-only"})
#: Facet → the class of the child objects that answer it.
_FACETS = {"faults": "faultInst", "health": "healthInst"}
_METHODS = frozenset({"GET", "POST", "DELETE"})


@dataclass(slots=True)
class _Failure:
    """One pending injected failure (see :meth:`FakeApic.fail`)."""

    dn: str
    code: str
    text: str
    status: int
    method: str | None
    remaining: int | None


@dataclass(frozen=True, slots=True)
class _Query:
    """A validated GET: everything checked before a single object is read."""

    target: str
    classes: frozenset[str] | None
    where: Predicate | None
    rsp_subtree: str
    rsp_classes: frozenset[str] | None
    rsp_where: Predicate | None
    facets: tuple[str, ...]
    required: bool
    prop_include: str
    order: tuple[tuple[Prop, bool], ...]
    page: int | None
    page_size: int | None


class FakeApic:
    """An in-memory APIC that answers the SDK's HTTP traffic — tests without a fabric.

    The fake is an ``httpx`` handler over a DN-indexed object store.  Writes
    (POST, DELETE) change the store; reads (GET) answer from it with the
    APIC's own query grammar — scoping, response subtrees, property
    selection, ordering, paging and **evaluated** filters.  Anything it
    cannot answer faithfully is refused with an APIC error whose text starts
    with ``"FakeApic:"``: never a silent, wrong answer.

    Wire it in through a ready client (:meth:`client`, :meth:`async_client`),
    through :attr:`transport` for an ``httpx`` client of your own, or through
    :meth:`handle` as a callback for an HTTP-mocking library.

    It mimics what the APIC does to what it is given: a bitmask comes back in
    a different order than written, a named number is stored by its name
    (port ``80`` → ``"http"``), and every returned object carries its ``dn``.

    Like every controller it always holds the policy universe, a ``polUni``
    at ``uni`` that cannot be deleted.

    What it is **not**: no RBAC (every login succeeds), no WebSocket
    subscriptions, no policy validation and no containment check (a design
    the controller would refuse, an object under a parent that cannot hold
    it, is stored), and no fault raising — a fault exists only when you seed
    one, and a failure only when you inject it with :meth:`fail`.

    Args:
        host: The base URL the fake answers as (its clients connect to it).
            The default is a reserved, never-resolvable name.

    Example::

        from niwaki.design import tenant
        from niwaki.testing import FakeApic

        fake = FakeApic()
        with fake.client() as aci:
            tenant("prod").bd("web").push(aci)
            assert aci.tenant("prod").bd("web").read().name == "web"
        assert fake.get("uni/tn-prod/BD-web") is not None
    """

    def __init__(self, *, host: str = "https://fake-apic.invalid") -> None:
        self._host = host
        self._store = Store()
        self._requests: list[httpx.Request] = []
        self._failures: list[_Failure] = []
        self._lock = threading.RLock()
        self._tokens = 0
        self._transport = httpx.MockTransport(self.handle)

    # ── Wiring ────────────────────────────────────────────────────────────────

    @property
    def host(self) -> str:
        """The base URL this fake answers as (``https://fake-apic.invalid`` by default)."""
        return self._host

    @property
    def transport(self) -> httpx.MockTransport:
        """An ``httpx`` transport answered by this fake, for a client you build yourself.

        Example::

            http = httpx.Client(base_url=fake.host, transport=fake.transport)
            with Niwaki.with_client(http, fake.host, "ops", "anything") as aci:
                ...
        """
        return self._transport

    @property
    def requests(self) -> tuple[httpx.Request, ...]:
        """Every request received, in order — login, refresh and refused ones included.

        A snapshot of the log at the moment you read it.  Seeding is not a
        request and never appears here.

        Example::

            posts = [r for r in fake.requests if r.method == "POST"]
        """
        with self._lock:
            return tuple(self._requests)

    def client(self, **kwargs: object) -> Niwaki:
        """A :class:`~niwaki.Niwaki` client wired to this fake, not yet connected.

        Entering it logs in with placeholder credentials the fake answers
        inline.  The store belongs to the fake, not to a client: several
        clients, one after the other or side by side, see the same objects.

        Args:
            **kwargs: Relayed untouched to the client constructor (``retry``,
                ``refresh_threshold``, …) — so an option the sync client does
                not take (``max_concurrent``) is refused exactly as
                :class:`~niwaki.Niwaki` refuses it.

        Returns:
            The client, inert until entered.

        Raises:
            TypeError: An option the client does not accept.

        Example::

            with fake.client() as aci:
                tenant("prod").bd("web").push(aci)
        """
        from niwaki.facade import Niwaki

        # The constructor validates the options and opens nothing: let it refuse
        # them before an httpx client exists, so a refusal leaves none behind.
        # The options are relayed untyped (``object``); the constructor types them.
        Niwaki(self._host, _USERNAME, _PASSWORD, **kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        http = httpx.Client(base_url=self._host, transport=self._transport)
        try:
            return Niwaki.with_client(http, self._host, _USERNAME, _PASSWORD, **kwargs)
        except BaseException:
            http.close()
            raise

    def async_client(self, **kwargs: object) -> AsyncNiwaki:
        """An :class:`~niwaki.AsyncNiwaki` client wired to this fake, not yet connected.

        Args:
            **kwargs: Relayed untouched to the client constructor
                (``max_concurrent``, ``retry``, …).

        Returns:
            The client, inert until entered with ``async with``.

        Raises:
            TypeError: An option the client does not accept.
            ValueError: An option the client refuses (``max_concurrent=0``).

        Example::

            async with fake.async_client() as aci:
                await tenant("prod").bd("web").push(aci)
        """
        from niwaki.facade import AsyncNiwaki

        # Validated first, with no httpx client yet: an AsyncClient cannot be
        # closed from synchronous code, so a refusal must happen before one exists.
        # The options are relayed untyped (``object``); the constructor types them.
        AsyncNiwaki(self._host, _USERNAME, _PASSWORD, **kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        http = httpx.AsyncClient(base_url=self._host, transport=self._transport)
        return AsyncNiwaki.with_client(http, self._host, _USERNAME, _PASSWORD, **kwargs)

    # ── Store views ───────────────────────────────────────────────────────────

    def get(self, dn: str) -> StoredObject | None:
        """The object stored at *dn*, as a detached read-only view.

        Args:
            dn: A Distinguished Name.

        Returns:
            A :class:`~niwaki.testing.StoredObject`, or ``None`` when nothing is
            stored there.

        Example::

            bd = fake.get("uni/tn-prod/BD-web")
            assert bd is not None and bd.attributes["arpFlood"] == "yes"
        """
        with self._lock:
            return self._store.view(dn)

    def objects(self, aci_class: str | None = None) -> tuple[StoredObject, ...]:
        """Every stored object (or every object of one exact class), in DN order.

        Args:
            aci_class: A wire class name to keep, or ``None`` for everything.

        Returns:
            Detached read-only views, sorted by DN.

        Example::

            assert [o.dn for o in fake.objects("fvBD")] == ["uni/tn-prod/BD-web"]
        """
        with self._lock:
            views = (self._store.view(dn) for dn in self._store.dns())
            return tuple(
                view
                for view in views
                if view is not None and (aci_class is None or view.aci_class == aci_class)
            )

    # ── Seeding ───────────────────────────────────────────────────────────────

    def seed(self, payload: JsonObject) -> None:
        """Load objects into the store, exactly as a POST would — without a request.

        A top-level object lands at its explicit ``dn`` attribute, else at its
        own RN from the root (``polUni`` → ``uni``, ``fabricTopology`` →
        ``topology``); children nest under it.  The APIC's storage mimicry
        applies (named numbers, bitmask order).  Seeding is not logged in
        :attr:`requests` and never triggers an injected failure.

        Args:
            payload: An APIC envelope, ``{"<class>": {"attributes": {...},
                "children": [...]}}``.  Seed a ``faultInst`` under an object to
                give it a fault.

        Raises:
            ValueError: The envelope is malformed, names an unknown class or
                property, or places an object in contradiction with itself (an
                explicit ``dn`` whose RN the naming properties do not compute,
                a child ``dn`` not directly under its parent).  Nothing is
                stored then: a seed is atomic.

        Example::

            fake.seed({"fvTenant": {"attributes": {"dn": "uni/tn-prod", "name": "prod"}}})
        """
        with self._lock:
            try:
                writes = plan_envelope(self._store, payload, None)
            except Refusal as refusal:
                raise ValueError(f"FakeApic: {refusal}") from None
            self._store.apply(writes)

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, *, host: str = "https://fake-apic.invalid") -> Self:
        """A fresh fake holding the objects a :mod:`niwaki.snapshot` captured.

        Every captured object is rebuilt at its DN (the snapshot's scope plus
        the RN chain of its tree), with its captured attributes.  A
        ``snapshot.take`` against the new fake, at the same scope, serialises
        to the same bytes — so a capture of a real fabric becomes a test
        fixture.

        Args:
            snapshot: The capture to replay.  An empty capture gives an empty
                fake.
            host: The base URL the new fake answers as.

        Returns:
            A new, independent fake (an empty request log).

        Raises:
            ValueError: The snapshot tree is malformed.

        Example::

            captured = snapshot.take(aci, "uni/tn-prod")
            replayed = FakeApic.from_snapshot(captured)
        """
        fake = cls(host=host)
        if snapshot.tree is not None:
            try:
                writes = snapshot_writes(snapshot.scope, snapshot.tree)
            except Refusal as refusal:
                raise ValueError(f"FakeApic: {refusal}") from None
            fake._store.apply(writes)
        return fake

    # ── Fault injection ───────────────────────────────────────────────────────

    def fail(
        self,
        dn: str,
        *,
        code: str = "400",
        text: str = "FakeApic: injected failure",
        status: int = 400,
        method: str | None = None,
        times: int | None = None,
    ) -> None:
        """Make the next requests that target *dn* answer an APIC error.

        A request targets *dn* when it is a GET or a DELETE of
        ``/api/mo/<dn>.json``, or a POST that would write *dn* — posted at
        *dn*, or *dn* materialised anywhere inside the posted envelope.  A
        class query or a read of an ancestor's subtree does not target it.
        A failed write changes nothing: an APIC POST is atomic.

        Through the SDK the answer surfaces as its typed error —
        :class:`~niwaki.exceptions.APIError` (or the subclass the HTTP
        status selects) with ``apic_code`` and ``apic_message`` set.

        Args:
            dn: The Distinguished Name to fail.
            code: The APIC error ``code`` to answer.
            text: The APIC error ``text`` to answer.
            status: The HTTP status (400 to 599).
            method: Narrow to one HTTP verb (``"GET"``, ``"POST"``,
                ``"DELETE"``), or ``None`` for all three.
            times: How many matching requests fail before the failure is
                spent, or ``None`` to fail every one until
                :meth:`clear_failures`.

        Raises:
            ValueError: *status* is not an error status, *method* is not a
                verb the fake serves, or *times* is below 1.

        Example::

            fake.fail("uni/tn-prod/BD-web", code="103", text="boom", method="POST")
        """
        if not 400 <= status <= 599:
            raise ValueError(f"status must be an HTTP error status (400-599), got {status}")
        verb = method.upper() if method is not None else None
        if verb is not None and verb not in _METHODS:
            raise ValueError(f"method must be one of {sorted(_METHODS)}, got {method!r}")
        if times is not None and times < 1:
            raise ValueError(f"times must be >= 1 (or None for always), got {times}")
        with self._lock:
            self._failures.append(_Failure(dn, code, text, status, verb, times))

    def clear_failures(self) -> None:
        """Drop every pending injected failure."""
        with self._lock:
            self._failures.clear()

    # ── HTTP entry point ──────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request — the handler behind :attr:`transport`.

        Host-agnostic: only the path, method, query string and body are read,
        so it can serve as the callback of an HTTP-mocking library for code
        that names its own APIC host.  Every request is logged in
        :attr:`requests`, whatever the answer.

        Args:
            request: The ``httpx`` request (its body already read).

        Returns:
            The APIC answer: an ``imdata`` envelope, or an APIC error envelope
            (HTTP 400 and a text starting with ``"FakeApic:"`` for a request
            the fake refuses; the injected status for an injected failure).

        Example::

            response = fake.handle(httpx.Request("GET", "https://apic/api/class/fvBD.json"))
        """
        with self._lock:
            self._requests.append(request)
            try:
                return self._dispatch(request)
            except Refusal as refusal:
                return _error(400, _REFUSAL_CODE, f"FakeApic: {refusal}")

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if path in (_LOGIN, _REFRESH, _LOGOUT):
            return self._session(method, path)
        if method not in _METHODS:
            raise Refusal(f"unsupported method {method} on {path}")
        if path.startswith(_MO) and path.endswith(_SUFFIX):
            dn = path.removeprefix(_MO).removesuffix(_SUFFIX)
            scope: tuple[str, str] = ("mo", dn)
        elif path.startswith(_CLASS) and path.endswith(_SUFFIX):
            scope = ("class", path.removeprefix(_CLASS).removesuffix(_SUFFIX))
        else:
            raise Refusal(f"unsupported path {path}")
        if not scope[1]:
            raise Refusal(f"unsupported path {path}")
        params = _params(request)
        if method == "GET":
            query = _parse_query(scope, params)
            if scope[0] == "mo" and (failed := self._failure("GET", {scope[1]})) is not None:
                return failed
            return self._get(scope, query)
        if scope[0] != "mo":
            raise Refusal(f"unsupported method {method} on a class URL ({path})")
        if params:
            raise Refusal(f"unexpected query parameter(s) {sorted(params)} on a {method}")
        dn = scope[1]
        if method == "DELETE":
            if dn == UNIVERSE:
                raise Refusal("the policy universe (uni) cannot be deleted")
            writes: list[Write] = [Delete(dn)]
            targets = {dn}
        else:
            try:
                body: object = json.loads(request.content)
            except ValueError:
                raise Refusal(f"the POST body to {path} is not JSON") from None
            writes = plan_envelope(self._store, body, dn)
            targets = {dn, *(write.dn for write in writes)}
        if (failed := self._failure(method, targets)) is not None:
            return failed
        self._store.apply(writes)
        return _ok([])

    def _session(self, method: str, path: str) -> httpx.Response:
        """The session endpoints, answered inline (no RBAC: every login succeeds)."""
        if path == _LOGOUT:
            if method != "POST":
                raise Refusal(f"unsupported method {method} on {path}")
            return _ok([])
        expected = "POST" if path == _LOGIN else "GET"
        if method != expected:
            raise Refusal(f"unsupported method {method} on {path}")
        from niwaki import catalog

        self._tokens += 1
        key = "aaaLogin" if path == _LOGIN else "aaaRefresh"
        attributes: JsonObject = {
            "token": f"fake-apic-token-{self._tokens}",
            "refreshTimeoutSeconds": _TOKEN_TTL,
            "maximumLifetimeSeconds": "86400",
            "userName": _USERNAME,
            "version": catalog.schema_version(),
        }
        return _ok([{key: {"attributes": attributes}}])

    def _failure(self, method: str, targets: set[str]) -> httpx.Response | None:
        """Consume the first pending failure matching the request, if any."""
        for failure in self._failures:
            if failure.dn in targets and failure.method in (None, method):
                if failure.remaining is not None:
                    failure.remaining -= 1
                    if failure.remaining == 0:
                        self._failures.remove(failure)
                return _error(failure.status, failure.code, failure.text)
        return None

    # ── GET ───────────────────────────────────────────────────────────────────

    def _get(self, scope: tuple[str, str], query: _Query) -> httpx.Response:
        store = self._store
        kind, name = scope
        if kind == "class":
            wanted = expand_class(name)
            dns = [dn for dn in store.dns() if store.class_of(dn) in wanted]
        elif query.target == "self":
            dns = [name] if name in store else []
        elif query.target == "children":
            dns = store.children(name)
        else:
            dns = store.subtree(name)
        if query.classes is not None:
            dns = [dn for dn in dns if store.class_of(dn) in query.classes]
        if query.where is not None:
            dns = [dn for dn in dns if query.where(self._subject(dn))]
        rendered = [(dn, self._render(dn, query, top=True)) for dn in dns]
        if query.required:
            rendered = [(dn, item) for dn, item in rendered if _has_children(item)]
        for target, _ in query.order:
            self._check_sortable(target, [dn for dn, _ in rendered])
        for target, descending in reversed(query.order):
            rendered.sort(
                key=lambda row: target.sort_key(self._value(row[0], target)), reverse=descending
            )
        total = len(rendered)
        items = [item for _, item in rendered]
        if query.page_size is not None:
            start = (query.page or 0) * query.page_size
            items = items[start : start + query.page_size]
        return _ok(items, total=total)

    def _check_sortable(self, target: Prop, dns: list[str]) -> None:
        """Refuse an ``order-by`` key met by an object of a class it does not qualify."""
        applies = expand_class(target.aci_class)
        for dn in dns:
            aci_class = self._store.class_of(dn)
            if aci_class not in applies:
                raise Refusal(
                    f"order-by {target.qualified} met by the {aci_class} {dn}: "
                    "the fake does not know how the APIC orders it"
                )

    def _subject(self, dn: str) -> tuple[str, str, Mapping[str, str]]:
        entry = self._store.entry(dn)
        assert entry is not None
        return dn, entry[0], entry[1]

    def _value(self, dn: str, target: Prop) -> str:
        if target.wire == DN:
            return dn
        entry = self._store.entry(dn)
        assert entry is not None
        return entry[1].get(target.wire, target.default)

    def _render(self, dn: str, query: _Query, *, top: bool) -> JsonObject:
        """One object as an APIC envelope item, with its response subtree."""
        aci_class, attributes = self._subject(dn)[1:]
        children: list[JsonValue] = []
        if top and query.rsp_subtree != "no":
            children.extend(self._embedded(dn, query, full=query.rsp_subtree == "full"))
        if top and query.facets:
            present = {_dn_of(child) for child in children}
            for facet in query.facets:
                for child in self._store.children(dn):
                    if self._store.class_of(child) == _FACETS[facet] and child not in present:
                        children.append(self._leaf(child, query))
        return _item(
            aci_class, self._project(dn, aci_class, attributes, query.prop_include), children
        )

    def _embedded(self, dn: str, query: _Query, *, full: bool) -> list[JsonValue]:
        """The children ``rsp-subtree`` embeds under *dn* (recursively for ``full``)."""
        out: list[JsonValue] = []
        for child in self._store.children(dn):
            matches = self._selected(child, query)
            if not full:
                if matches:
                    out.append(self._leaf(child, query))
                continue
            nested = self._embedded(child, query, full=True)
            if matches or nested:
                aci_class, attributes = self._subject(child)[1:]
                projected = self._project(child, aci_class, attributes, query.prop_include)
                out.append(_item(aci_class, projected, nested))
        return out

    def _selected(self, dn: str, query: _Query) -> bool:
        """Whether an embedded child passes ``rsp-subtree-class`` / ``rsp-subtree-filter``."""
        if query.rsp_classes is not None and self._store.class_of(dn) not in query.rsp_classes:
            return False
        return query.rsp_where is None or query.rsp_where(self._subject(dn))

    def _leaf(self, dn: str, query: _Query) -> JsonObject:
        aci_class, attributes = self._subject(dn)[1:]
        return _item(aci_class, self._project(dn, aci_class, attributes, query.prop_include), [])

    @staticmethod
    def _project(
        dn: str, aci_class: str, attributes: Mapping[str, str], prop_include: str
    ) -> JsonObject:
        """``rsp-prop-include``: all stored attributes, the naming ones or the configurable ones."""
        keep: Callable[[str], bool]
        if prop_include == "naming-only":
            naming = naming_props(aci_class)
            keep = naming.__contains__
        elif (
            prop_include == "config-only"
            and (configurable := configurable_props(aci_class)) is not None
        ):
            keep = configurable.__contains__
        else:
            keep = _always
        projected: JsonObject = {name: value for name, value in attributes.items() if keep(name)}
        projected[DN] = dn
        return projected


# ── Request parsing ───────────────────────────────────────────────────────────


def _params(request: httpx.Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in request.url.params.multi_items():
        if key in params:
            raise Refusal(f"query parameter {key!r} is given twice")
        params[key] = value
    return params


def _parse_query(scope: tuple[str, str], params: Mapping[str, str]) -> _Query:
    """Validate every GET parameter before anything is read (fail loud)."""
    if "subscription" in params:
        raise Refusal("subscriptions are not supported (the fake has no WebSocket)")
    unknown = sorted(set(params) - _GET_PARAMS)
    if unknown:
        raise Refusal(f"unsupported query parameter(s) {', '.join(unknown)}")
    kind, name = scope
    target = _choice(params, "query-target", "self", _QUERY_TARGETS)
    if kind == "class":
        if target != "self":
            raise Refusal(f"query-target={target} is not supported on a class query")
        if "target-subtree-class" in params:
            raise Refusal("target-subtree-class is not supported on a class query")
        returned: frozenset[str] | None = expand_class(name)
    else:
        returned = _classes(params.get("target-subtree-class"))
    rsp_subtree = _choice(params, "rsp-subtree", "no", _RSP_SUBTREES)
    prop_include = _choice(params, "rsp-prop-include", "all", _PROP_INCLUDES)
    facets: list[str] = []
    required = False
    for facet in _csv(params.get("rsp-subtree-include", "")):
        if facet == "required":
            required = True
        elif facet in _FACETS:
            facets.append(facet)
        else:
            raise Refusal(f"rsp-subtree-include={facet} cannot be answered by the fake")
    # Response-subtree parameters with nothing embedded would be silently ignored:
    # refuse them, before compiling a filter, so the refusal names the parameter.
    if rsp_subtree == "no":
        for key in ("rsp-subtree-class", "rsp-subtree-filter"):
            if key in params:
                raise Refusal(f"{key} without rsp-subtree=children|full embeds nothing")
        if required and not facets:
            raise Refusal(
                "rsp-subtree-include=required with nothing to require "
                "(no rsp-subtree, no faults/health facet)"
            )
    rsp_classes = _classes(params.get("rsp-subtree-class"))
    return _Query(
        target=target,
        classes=returned if kind == "mo" else None,
        where=_filter(params.get("query-target-filter"), returned),
        rsp_subtree=rsp_subtree,
        rsp_classes=rsp_classes,
        rsp_where=_filter(params.get("rsp-subtree-filter"), rsp_classes),
        facets=tuple(facets),
        required=required,
        prop_include=prop_include,
        order=_order(params.get("order-by"), returned),
        page=_integer(params, "page", minimum=0),
        page_size=_page_size(params),
    )


def _choice(params: Mapping[str, str], key: str, default: str, allowed: frozenset[str]) -> str:
    value = params.get(key, default)
    if value not in allowed:
        raise Refusal(f"{key}={value} is not one of {', '.join(sorted(allowed))}")
    return value


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _classes(value: str | None) -> frozenset[str] | None:
    if value is None:
        return None
    names = _csv(value)
    if not names:
        raise Refusal("an empty class list")
    return frozenset(cls for name in names for cls in expand_class(name))


def _filter(value: str | None, scope: frozenset[str] | None) -> Predicate | None:
    return None if value is None else compile_filter(value, scope)


def _order(value: str | None, returned: frozenset[str] | None) -> tuple[tuple[Prop, bool], ...]:
    """Parse ``order-by``: each key an orderable property of a class the query returns."""
    if value is None:
        return ()
    keys: list[tuple[Prop, bool]] = []
    for part in _csv(value):
        qualified, _, direction = part.partition("|")
        direction = direction or "asc"
        if direction not in ("asc", "desc"):
            raise Refusal(f"order-by direction {direction!r} is not asc or desc")
        aci_class, dot, wire = qualified.partition(".")
        if not dot or not aci_class or not wire:
            raise Refusal(f"order-by property {qualified!r} must be qualified as class.prop")
        target = prop(aci_class, wire)
        if returned is not None and not expand_class(aci_class) & returned:
            raise Refusal(f"order-by {qualified}: the query cannot return a {aci_class}")
        target.check_orderable(sorting=True)
        keys.append((target, direction == "desc"))
    if not keys:
        raise Refusal("an empty order-by")
    return tuple(keys)


def _integer(params: Mapping[str, str], key: str, *, minimum: int) -> int | None:
    raw = params.get(key)
    if raw is None:
        return None
    try:
        number = int(raw)
    except ValueError:
        raise Refusal(f"{key}={raw} is not an integer") from None
    if number < minimum:
        raise Refusal(f"{key}={raw} is below {minimum}")
    return number


def _page_size(params: Mapping[str, str]) -> int | None:
    size = _integer(params, "page-size", minimum=1)
    if size is None and "page" in params:
        raise Refusal("page without page-size")
    return size


# ── Envelopes ─────────────────────────────────────────────────────────────────


def _always(_: str) -> bool:
    return True


def _item(aci_class: str, attributes: JsonObject, children: list[JsonValue]) -> JsonObject:
    body: JsonObject = {"attributes": attributes}
    if children:
        body["children"] = children
    return {aci_class: body}


def _dn_of(item: JsonValue) -> str | None:
    if not isinstance(item, dict):
        return None
    for body in item.values():
        if isinstance(body, dict) and isinstance(attributes := body.get("attributes"), dict):
            dn = attributes.get(DN)
            return dn if isinstance(dn, str) else None
    return None


def _has_children(item: JsonObject) -> bool:
    return any(isinstance(body, dict) and bool(body.get("children")) for body in item.values())


def _ok(items: list[JsonObject], *, total: int | None = None) -> httpx.Response:
    imdata: list[JsonValue] = list(items)
    count = len(items) if total is None else total
    return httpx.Response(200, json={"totalCount": str(count), "imdata": imdata})


def _error(status: int, code: str, text: str) -> httpx.Response:
    item: JsonObject = {"error": {"attributes": {"code": code, "text": text}}}
    return httpx.Response(status, json={"totalCount": "1", "imdata": [item]})
