"""Niwaki — facade layer for the ACI SDK.

Provides two independent, symmetric entry points:

- :class:`Niwaki` — sync facade; use with ``with Niwaki(...) as aci``.
- :class:`AsyncNiwaki` — async facade; use with ``async with AsyncNiwaki(...) as aci``.

Each entry point manages its own session lifecycle.  They share the same
navigation and observation API through :class:`NiwakiNode` (sync) and
:class:`AsyncNiwakiNode` (async).

The facade **observes**: navigation, reads, queries, and deletion.  All
configuration goes through the design DSL (:mod:`niwaki.design`) — describe
the desired subtree with ``design()``/``tenant()``/``infra()``/``fabric()``
and ``push()`` it through a connected client.

Sync workflow::

    from niwaki import Niwaki

    with Niwaki("https://apic.example.com", "admin", "secret") as aci:
        bd = aci.root.tenant("prod").bd("web").read()
        stale = aci.query(fvBD).where(name="old").first()

Async workflow::

    from niwaki import AsyncNiwaki

    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bd = await aci.gather(
            aci.query(fvTenant).fetch(),
            aci.root.tenant("prod").bd("web").read(),
        )

Writing (design DSL)::

    from niwaki import Niwaki, tenant

    config = tenant("prod")
    config.bd("web").bind(vrf="prod")
    config.vrf("prod")
    with Niwaki("https://apic.example.com", "admin", "secret") as aci:
        config.push(aci)
"""

from __future__ import annotations

import asyncio
import builtins
import os
import warnings
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast, overload

import httpx
from pydantic import SecretStr

from niwaki.models.base import ManagedObject
from niwaki.transport._config import RetryConfig
from niwaki.transport._subscription_socket import SubscriptionInfo
from niwaki.transport._token_cache import TokenCache
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession

# Directory of the installed niwaki package — frames under it are skipped
# when attributing deprecation warnings, so they land on the caller's line.
# The trailing separator matters: CPython matches with a bare startswith, so
# without it a sibling package like site-packages/niwaki_toolkit would have
# its frames skipped too and the warning would land above the real caller.
_PKG_DIR = str(Path(__file__).resolve().parent) + os.sep

if TYPE_CHECKING:
    from niwaki.query._async_builder import AsyncQuery
    from niwaki.query._builder import Query

# Typed navigation exists for the type checker only: ``niwaki._generated_nav``
# is a stub-only package (``.pyi`` files, no ``.py`` module).  The checker sees
# each client derive from its typed root navigator; at runtime both bases are
# plain ``object`` and every navigation still resolves through ``__getattr__``.
if TYPE_CHECKING:
    from niwaki._generated_nav import AsyncRootNav as _AsyncRootNavBase
    from niwaki._generated_nav import AsyncRootNode, RootNode
    from niwaki._generated_nav import RootNav as _RootNavBase
else:
    _RootNavBase = object
    _AsyncRootNavBase = object

# A coroutine handed to AsyncNiwaki.gather: what it yields and is sent is the
# event loop's business, only its result type matters to the caller.
type _Co[T] = Coroutine[object, object, T]

# ── Private navigation helpers ─────────────────────────────────────────────────


class _JargonTarget(NamedTuple):
    """Navigation metadata resolved from a vocabulary attribute.

    Attributes:
        child_cls: Generated class the vocabulary name maps to.
        naming_props: Naming properties of that class.
        is_rs_target: ``True`` for Rs singletons carrying a target-name prop
            (exposed as the Python field ``name`` by the D2 renaming).
    """

    child_cls: type[ManagedObject]
    naming_props: list[str]
    is_rs_target: bool


# The write vocabulary of every demolished facade-write era and of the ORMs/
# cobra users arrive from — anything here gets pointed at the design DSL.
_WRITE_VERBS = frozenset(
    {"create", "add", "update", "modify", "push", "apply", "bind", "set", "commit", "save"}
)


def _navigate_jargon(parent_cls: type[ManagedObject], attr: str) -> _JargonTarget:
    """Resolve a vocabulary attribute to its child class and navigation metadata.

    Looks up *attr* in ``CHILD_MAP`` for *parent_cls*, imports the generated
    class module, and returns everything needed to build a navigator function.

    Args:
        parent_cls: The ACI class of the parent node (or
            :class:`~niwaki.models.base.ManagedObject` for the root).
        attr: Vocabulary method name to resolve (e.g. ``"tenant"``, ``"bd"``).

    Raises:
        AttributeError: *attr* is not a known child of *parent_cls*.
    """
    from importlib import import_module

    from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, NAV_DEPRECATED, RS_TARGET_PROP

    parent_key = "_root" if parent_cls is ManagedObject else parent_cls.__name__
    children = CHILD_MAP.get(parent_key, {})

    if attr not in children:
        deprecated_target = NAV_DEPRECATED.get(parent_key, {}).get(attr)
        if deprecated_target is not None:
            # A pre-1.5.0 navigation name: resolve identically, but tell the
            # caller its replacement.  skip_file_prefixes pins the warning on
            # the first frame outside niwaki, whatever the entry path — the
            # node __getattr__ and the Niwaki/AsyncNiwaki root proxies reach
            # here through different depths (and a warning attributed to a
            # library file is hidden by CPython's default filters).
            current = next((n for n, c in children.items() if c == deprecated_target), None)
            if current is None:
                raise AttributeError(
                    f"deprecated navigation name {attr!r} under {parent_key!r} has "
                    "no current replacement — the generated navigation tables are "
                    "out of sync; reinstall niwaki or regenerate _child_map.py"
                )
            warnings.warn(
                f"navigation name {attr!r} under {parent_key!r} is deprecated — "
                f"use {current!r} instead; deprecated names are removed no "
                "earlier than the next minor release",
                DeprecationWarning,
                skip_file_prefixes=(_PKG_DIR,),
            )
            attr = current
        elif attr in _WRITE_VERBS:
            # A write verb is the single most likely stumble on the three-
            # trades split (the facade observes; configuration is the design
            # DSL's job) — steer it there instead of deeper into the
            # observation surface.
            raise AttributeError(
                f"{parent_cls.__name__!r} node has no {attr!r}: the facade is "
                "read-only (navigate, read, query, delete). Configuration goes "
                "through the design DSL — e.g. "
                'tenant("prod").bd("web").push(aci); see niwaki.design.'
            )
        else:
            raise AttributeError(
                f"{parent_cls.__name__!r} node has no child accessor {attr!r}. "
                "Use .mo(ChildClass, ...) for unlisted classes."
            )

    child_aci_class = children[attr]
    pkg = CLASS_PKG[child_aci_class]
    mod = import_module(f"niwaki.models._generated.{pkg}.{child_aci_class}")
    child_cls: type[ManagedObject] = getattr(mod, child_aci_class)
    naming_props: list[str] = child_cls._naming_props  # pyright: ignore[reportPrivateUsage]

    return _JargonTarget(child_cls, naming_props, child_aci_class in RS_TARGET_PROP)


def _make_jargon_navigator[T: ManagedObject](
    node: NiwakiNode[T] | AsyncNiwakiNode[T],
    child_cls: type[ManagedObject],
    naming_props: list[str],
    is_rs_target: bool,
) -> Callable[..., object]:
    """Build the callable that navigation sugar returns for a vocabulary attribute.

    Dispatches to ``node.mo()`` regardless of whether *node* is a
    :class:`NiwakiNode` or :class:`AsyncNiwakiNode`, so both return the correct
    child type without code duplication.

    Args:
        node: Parent node instance.
        child_cls: Child ACI class to navigate into.
        naming_props: List of naming properties for *child_cls*.
        is_rs_target: ``True`` for Rs singletons carrying a target-name prop.

    Returns:
        A callable that accepts naming arguments and returns a child node.
    """
    if not naming_props and is_rs_target:

        def _nav_rs_singleton(target_name: str = "", **kwargs: object) -> object:
            # D2 renames every tn*Name target prop to the Python field
            # "name", so one constructor shape covers all Rs classes.
            if target_name:
                kwargs = {"name": target_name, **kwargs}
            return node.mo(child_cls, **kwargs)

        return _nav_rs_singleton

    if not naming_props:

        def _nav_singleton() -> object:
            return node.mo(child_cls)

        return _nav_singleton

    if naming_props == ["name"]:

        def _nav_name(name: str = "") -> object:
            if name:
                return node.mo(child_cls, name=name)
            return node.query(child_cls)

        return _nav_name

    def _nav_kwargs(**naming_kwargs: object) -> object:
        if naming_kwargs:
            return node.mo(child_cls, **naming_kwargs)
        return node.query(child_cls)

    return _nav_kwargs


# ── Shared helpers ────────────────────────────────────────────────────────────


class _JargonNavMixin[T: ManagedObject]:
    """Shared ``__getattr__`` logic for DN-scoped navigation nodes.

    Both :class:`NiwakiNode` and :class:`AsyncNiwakiNode` expose the same
    vocabulary navigation (e.g. ``.tenant("prod").bd("web")``).  This mixin
    centralises the implementation so that any change to the resolution logic
    applies to both node types automatically.

    The contract with the host class is declared below as an annotation
    (``_cls``); the host also provides the ``mo()`` method used by the
    navigators.
    """

    _cls: type[T]

    def __getattr__(self, attr: str) -> Any:  # Any: name-driven navigation, resolved at runtime
        """Resolve vocabulary attribute names to typed child navigators.

        Args:
            attr: Method name to resolve (e.g. ``"tenant"``, ``"bd"``).

        Returns:
            Callable that, when invoked with naming kwargs, returns a child
            node of the appropriate concrete type (sync or async).

        Raises:
            AttributeError: ``attr`` is not a known child of this node's class.
        """
        if attr.startswith("_"):
            raise AttributeError(attr)
        child_cls, naming_props, is_rs_target = _navigate_jargon(self._cls, attr)
        return _make_jargon_navigator(self, child_cls, naming_props, is_rs_target)  # type: ignore[arg-type]


# ── Subscription managers ───────────────────────────────────────────────────────


class SubscriptionManager:
    """Bulk introspection/management for a session's object-subscriptions (sync).

    Reached via :attr:`~niwaki.Niwaki.subscriptions`. Operates on whichever
    subscriptions are currently open on the session's shared WebSocket — every
    method is a safe no-op (empty list, or nothing to do) until at least one
    ``aci.query(...).subscribe()`` call has opened it.
    """

    def __init__(self, session: ApicSession) -> None:
        self._session = session

    def list(self) -> builtins.list[SubscriptionInfo]:
        """List every subscription currently tracked, or ``[]`` if none is open.

        Returns:
            One :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`
            per tracked subscription — ``.is_stale`` flags one with at least
            one recent refresh failure.

        Example::

            for info in aci.subscriptions.list():
                print(info.path, info.consecutive_refresh_failures, info.is_stale)
        """
        return self._session.list_subscriptions()

    def refresh_all(self) -> builtins.list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        A diagnostic tool, distinct from the automatic background refresh
        sweep — a failure here never triggers the automatic recovery
        escalation a missed *scheduled* refresh would.

        Returns:
            The post-refresh snapshot of every subscription.
        """
        return self._session.refresh_all_subscriptions()

    def close_all(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open.

        Every open :class:`~niwaki.query.Subscription`'s iterator ends with a
        plain ``StopIteration``. Distinct from closing the whole
        :class:`Niwaki` session: a later ``aci.query(...).subscribe()`` reuses
        the same still-open socket immediately, with no reconnect needed.
        """
        self._session.close_all_subscriptions()


class AsyncSubscriptionManager:
    """Async counterpart of :class:`~niwaki.facade.SubscriptionManager`. Reached via
    :attr:`~niwaki.AsyncNiwaki.subscriptions`."""

    def __init__(self, session: AsyncApicSession) -> None:
        self._session = session

    def list(self) -> builtins.list[SubscriptionInfo]:
        """List every subscription currently tracked, or ``[]`` if none is open.

        Synchronous even on the async facade — see
        :meth:`~niwaki.transport.AsyncApicSession.list_subscriptions`.

        Returns:
            One :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`
            per tracked subscription.
        """
        return self._session.list_subscriptions()

    async def refresh_all(self) -> builtins.list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        Returns:
            The post-refresh snapshot of every subscription.
        """
        return await self._session.refresh_all_subscriptions()

    async def close_all(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open."""
        await self._session.close_all_subscriptions()


# ── NiwakiNode ─────────────────────────────────────────────────────────────────


class NiwakiNode[T: ManagedObject](_JargonNavMixin[T]):
    """A DN-scoped handle for observing a single ACI object (sync).

    Created via :attr:`Niwaki.root` or :meth:`Niwaki.node`.  Navigate the ACI
    hierarchy with :meth:`mo` or the vocabulary accessors — each call descends one
    level and computes the child DN automatically from the parent DN and the
    child's RN.  Terminal operations are read-side (:meth:`read`,
    :meth:`query`) plus :meth:`delete`; configuration goes through
    :mod:`niwaki.design`.

    Args:
        niwaki: Parent :class:`Niwaki` instance.
        dn: Full Distinguished Name of this node (e.g. ``"uni/tn-prod/BD-web"``).
        cls: ACI class associated with this node.  Used to deserialise reads.
    """

    def __init__(self, niwaki: Niwaki, dn: str, cls: type[T]) -> None:
        self._niwaki = niwaki
        self._dn = dn
        self._cls = cls

    # ── Navigation ────────────────────────────────────────────────────────────

    @property
    def dn(self) -> str:
        """Full Distinguished Name of this node (read-only).

        Returns:
            DN string, e.g. ``"uni/tn-prod/BD-web"``.

        Example::

            node = aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web")
            node.dn  # → "uni/tn-prod/BD-web"
        """
        return self._dn

    @property
    def cls(self) -> type[T]:
        """ACI class associated with this node.

        Returns:
            The :class:`~niwaki.models.base.ManagedObject` subclass used to
            construct and deserialise objects at this DN.
        """
        return self._cls

    def mo[U: ManagedObject](self, cls: type[U], **naming_kwargs: object) -> NiwakiNode[U]:
        """Descend to a child node, computing its DN automatically.

        Creates a temporary instance of ``cls`` purely to derive the RN from
        ``_rn_format``.  The child DN is ``self.dn + "/" + child.rn``.

        Args:
            cls: ACI class of the child object.
            **naming_kwargs: Naming props required by ``cls``
                (e.g. ``name="web"``).

        Returns:
            A new :class:`NiwakiNode` scoped to the child DN.

        Raises:
            ValidationError: If ``naming_kwargs`` violate the model constraints
                for ``cls``.

        Example::

            aci.root \
               .mo(fvTenant, name="prod") \
               .mo(fvBD, name="web") \
               .mo(fvSubnet, ip="10.0.0.1/24")
        """
        instance = cls.model_validate(naming_kwargs)
        child_dn = f"{self._dn}/{instance.rn}"
        return NiwakiNode(self._niwaki, child_dn, cls)

    # ── Terminal operations (read-side + delete) ─────────────────────────────

    def read(self) -> T:
        """Fetch the ACI object at this DN from the APIC.

        Returns:
            Typed :class:`~niwaki.models.base.ManagedObject` instance.

        Raises:
            NotFoundError: The object does not exist on the APIC.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            bd = aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").read()
            print(bd.unicast_routing)
        """
        return self._niwaki._sync_session.get_mo(self._dn, cls=self._cls)  # pyright: ignore[reportPrivateUsage]

    def delete(self) -> None:
        """Delete the ACI object at this DN.

        Raises:
            NotFoundError: The object does not exist on the APIC.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.

        Example::

            aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").delete()
        """
        self._niwaki._sync_session.delete_mo(self._dn)  # pyright: ignore[reportPrivateUsage]

    @overload
    def query[U: ManagedObject](self, cls: type[U]) -> Query[U]: ...

    @overload
    def query(self, cls: str) -> Query[ManagedObject]: ...

    def query[U: ManagedObject](self, cls: type[U] | str) -> Query[U]:
        """Build a query scoped to this node's DN.

        Returns all instances of *cls* that are descendants of this node,
        using the APIC ``query-target=subtree&target-subtree-class=cls`` query
        mode.  Chain filter, scope, and enrichment methods before executing with
        :meth:`~niwaki.query.Query.fetch`, :meth:`~niwaki.query.Query.first`,
        :meth:`~niwaki.query.Query.count`, or :meth:`~niwaki.query.Query.stream`.

        This is also the entry point for querying unregistered classes (the
        full APIC class catalogue of ~15 000 classes): pass a plain string
        class name and the result will be a ``Query[ManagedObject]`` whose
        objects expose all APIC attributes via ``model_extra``.

        Args:
            cls: ACI class type (e.g. ``fvBD``) or plain string class name
                 (e.g. ``"topSystem"``).

        Returns:
            :class:`~niwaki.query.Query` scoped to this node's DN.

        Example::

            # All BDs under tenant "prod"
            bds = aci.root.tenant("prod").query(fvBD).fetch()

            # Count subnets in a specific BD
            n = aci.root.tenant("prod").bd("web").query(fvSubnet).count()

            # Unregistered / read-only class, scoped under this node's subtree
            faults = aci.root.tenant("prod").query("faultInst").fetch()
        """
        from niwaki.query._builder import Query

        return Query(cls, self._niwaki._sync_session, scope_dn=self._dn)  # pyright: ignore[reportPrivateUsage]


# ── AsyncNiwakiNode ────────────────────────────────────────────────────────────


class AsyncNiwakiNode[T: ManagedObject](_JargonNavMixin[T]):
    """A DN-scoped handle for observing a single ACI object (async).

    Mirrors :class:`NiwakiNode` for async contexts.  Terminal operations
    (:meth:`read`, :meth:`delete`) are coroutines; configuration goes through
    :mod:`niwaki.design`.

    Args:
        niwaki: Parent :class:`AsyncNiwaki` instance.
        dn: Full Distinguished Name of this node.
        cls: ACI class associated with this node.  Used to deserialise reads.
    """

    def __init__(self, niwaki: AsyncNiwaki, dn: str, cls: type[T]) -> None:
        self._niwaki = niwaki
        self._dn = dn
        self._cls = cls

    # ── Navigation ────────────────────────────────────────────────────────────

    @property
    def dn(self) -> str:
        """Full Distinguished Name of this node (read-only).

        Returns:
            DN string, e.g. ``"uni/tn-prod/BD-web"``.
        """
        return self._dn

    @property
    def cls(self) -> type[T]:
        """ACI class associated with this node.

        Returns:
            The :class:`~niwaki.models.base.ManagedObject` subclass used to
            construct and deserialise objects at this DN.
        """
        return self._cls

    def mo[U: ManagedObject](self, cls: type[U], **naming_kwargs: object) -> AsyncNiwakiNode[U]:
        """Descend to a child node, computing its DN automatically.

        Args:
            cls: ACI class of the child object.
            **naming_kwargs: Naming props required by ``cls``.

        Returns:
            A new :class:`AsyncNiwakiNode` scoped to the child DN.

        Raises:
            ValidationError: If ``naming_kwargs`` violate model constraints.
        """
        instance = cls.model_validate(naming_kwargs)
        child_dn = f"{self._dn}/{instance.rn}"
        return AsyncNiwakiNode(self._niwaki, child_dn, cls)

    # ── Terminal operations (read-side + delete) ─────────────────────────────

    async def read(self) -> T:
        """Fetch the ACI object at this DN from the APIC.

        Returns:
            Typed :class:`~niwaki.models.base.ManagedObject` instance.

        Raises:
            NotFoundError: The object does not exist.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.
        """
        return await self._niwaki._active_session.get_mo(self._dn, cls=self._cls)  # pyright: ignore[reportPrivateUsage]

    async def delete(self) -> None:
        """Delete the ACI object at this DN.

        Raises:
            NotFoundError: Object does not exist.
            ForbiddenError: Insufficient privileges.
            ServerError: APIC server-side error.
        """
        await self._niwaki._active_session.delete_mo(self._dn)  # pyright: ignore[reportPrivateUsage]

    @overload
    def query[U: ManagedObject](self, cls: type[U]) -> AsyncQuery[U]: ...

    @overload
    def query(self, cls: str) -> AsyncQuery[ManagedObject]: ...

    def query[U: ManagedObject](self, cls: type[U] | str) -> AsyncQuery[U]:
        """Build a query scoped to this node's DN (async variant).

        Mirrors :meth:`~niwaki.NiwakiNode.query` for async contexts.
        All accumulator methods are synchronous; only the execution methods
        (``fetch``, ``first``, ``count``, ``stream``) are coroutines.

        Args:
            cls: ACI class type or plain string class name.

        Returns:
            :class:`~niwaki.query.AsyncQuery` scoped to this node's DN.

        Example::

            bds = await aci.root.tenant("prod").query(fvBD).fetch()
            n   = await aci.root.tenant("prod").bd("web").query(fvSubnet).count()
        """
        from niwaki.query._async_builder import AsyncQuery

        return AsyncQuery(cls, self._niwaki._active_session, scope_dn=self._dn)  # pyright: ignore[reportPrivateUsage]


# ── Shared client construction ─────────────────────────────────────────────────


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
) -> dict[str, Any]:  # Any: heterogeneous options splatted into the session constructor
    """Validate a client's options and turn them into its session's keyword arguments.

    The one place where :class:`Niwaki` and :class:`AsyncNiwaki` check what
    they were given and decide what their session class receives.  Both
    constructors call it and keep the mapping; entry (``__enter__`` /
    ``__aenter__``, hence ``connect()`` and ``with_client()``) builds the
    session from a copy of it, plus the injected ``httpx`` client when there
    is one.  Validating here, from the constructors, makes a mistake fail
    where it was written rather than at the first login.

    The function is pure: it reads no environment variable and performs no
    I/O.  The environment fallbacks (``APIC_HOST``, ``APIC_USERNAME``,
    ``APIC_PASSWORD``) belong to the session and are resolved when it is
    built; a private key path is forwarded, never opened.

    Args:
        host: APIC base URL, or ``None`` for the session's ``APIC_HOST``
            fallback.  Passed through unchanged.
        username: Login name, or ``None`` for the session's
            ``APIC_USERNAME`` fallback.  Passed through unchanged.
        password: Password.  A ``str`` (even ``""``) is wrapped in a
            ``pydantic.SecretStr`` so it is never held in clear; a
            ``SecretStr`` is passed through as the same object; ``None``
            stays ``None`` (the session owns the environment fallback).
        verify_ssl: TLS verification — ``True``, a PEM CA bundle path, or
            ``False``.  Passed through unchanged.
        timeout: HTTP request timeout in seconds.  Passed through unchanged.
        refresh_threshold: Seconds before token expiry at which the session
            refreshes proactively.  Passed through unchanged.
        retry: Custom retry policy.  The key is present only when not
            ``None``, so the session keeps its own default otherwise.
        token_cache: Opt-in login token cache.  The key is present only when
            not ``None``.
        private_key: Path to the PEM private key of a certificate login.
            Must be given together with *cert_dn*; both keys are present only
            when the pair is.
        cert_dn: DN of the certificate registered on the APIC user.  Must be
            given together with *private_key*.
        max_concurrent: Async only — the bound on simultaneous in-flight
            requests.  The key is present only when given; the sync client
            never passes it.

    Returns:
        A fresh ``dict`` on every call, ready to splat into the session
        class.  It always holds ``host``, ``username``, ``password``,
        ``verify_ssl``, ``timeout`` and ``refresh_threshold``.  It never
        holds ``client``: an injected ``httpx`` client is attached by the
        caller at entry, after the mapping is built.

    Raises:
        ValueError: Exactly one of *private_key* / *cert_dn* is given (a lone
            ``cert_dn`` would otherwise fall back to password auth without a
            word), or *max_concurrent* is given and below 1 (a zero-permit
            semaphore is a wait for a slot never released).

    Example::

        options = _session_kwargs(
            host="https://apic.example.com",
            username="admin",
            password="secret",
            verify_ssl=True,
            timeout=30.0,
            refresh_threshold=60,
            retry=None,
            token_cache=None,
            private_key=None,
            cert_dn=None,
            max_concurrent=10,
        )
        options["password"]   # SecretStr('**********')
        "retry" in options    # False: the session keeps its default policy
        session = AsyncApicSession(**options)
    """
    if (private_key is None) != (cert_dn is None):
        raise ValueError("private_key and cert_dn must be given together")
    if max_concurrent is not None and max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    options: dict[str, object] = {
        "host": host,
        "username": username,
        "password": SecretStr(password) if isinstance(password, str) else password,
        "verify_ssl": verify_ssl,
        "timeout": timeout,
        "refresh_threshold": refresh_threshold,
    }
    if max_concurrent is not None:
        options["max_concurrent"] = max_concurrent
    if retry is not None:
        options["retry"] = retry
    if token_cache is not None:
        options["token_cache"] = token_cache
    if private_key is not None and cert_dn is not None:
        options["private_key"] = private_key
        options["cert_dn"] = cert_dn
    return options


# ── AsyncNiwaki ────────────────────────────────────────────────────────────────


class AsyncNiwaki(_AsyncRootNavBase):
    """Async entry point for the ACI SDK.

    Mirrors :class:`Niwaki` for async code.  Use as an async context manager —
    authentication happens on ``__aenter__`` and the session is closed on
    ``__aexit__``.

    Args:
        host: Base URL of the APIC (e.g. ``"https://sandboxapicdc.cisco.com"``).
            Falls back to ``APIC_HOST`` environment variable if omitted.
        username: APIC username. Falls back to ``APIC_USERNAME`` if omitted.
        password: APIC password, as a ``str`` or a ``pydantic.SecretStr``.
            Falls back to ``APIC_PASSWORD`` when omitted or empty, read when
            the session is built (on entry or ``connect()``), not here.  It is
            not stored as a plain attribute: the client keeps a ``SecretStr``,
            masked in ``repr``/``str``, and hands it to the session as is.
        verify_ssl: TLS verification — ``True`` (system CA store), a path to
            a PEM CA bundle (private/enterprise CA), or ``False`` (lab only).
            Default: ``True``.
        timeout: HTTP request timeout in seconds.  Default: 30.
        refresh_threshold: Seconds before token expiry at which a proactive
            refresh is triggered.  Default: 60.
        max_concurrent: Maximum simultaneous HTTP requests in flight.
            Default: 10.
        retry: Custom retry policy.  Defaults to 3 attempts with exponential
            back-off.  Construct with :class:`~niwaki.transport.RetryConfig`.

    Example::

        async with AsyncNiwaki("https://apic.example.com", "admin", "pass") as aci:
            tenants = await aci.query(fvTenant).fetch()
            bd = await aci.root.tenant("prod").bd("web").read()

        # Concurrent reads via gather()
        async with AsyncNiwaki("https://apic.example.com", "admin", "pass") as aci:
            tenants, bd = await aci.gather(
                aci.query(fvTenant).fetch(),
                aci.root.tenant("prod").bd("web").read(),
            )
    """

    def __init__(
        self,
        host: str | None = None,
        username: str | None = None,
        password: str | SecretStr | None = None,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        max_concurrent: int = 10,
        retry: RetryConfig | None = None,
        token_cache: TokenCache | None = None,
        private_key: str | Path | None = None,
        cert_dn: str | None = None,
    ) -> None:
        # Validated here, where a mistake is written; the password is held as a
        # SecretStr and the environment fallbacks are left to the session, so
        # nothing is read or opened before entry.
        self._session_options = _session_kwargs(
            host=host,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            timeout=timeout,
            refresh_threshold=refresh_threshold,
            retry=retry,
            token_cache=token_cache,
            private_key=private_key,
            cert_dn=cert_dn,
            max_concurrent=max_concurrent,
        )
        # The login name is not a secret: kept on the client for inspection.
        self._username = username
        self._retry = retry
        self._max_concurrent = max_concurrent
        self._injected_client: httpx.AsyncClient | None = None
        self._session: AsyncApicSession | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        host: str,
        username: str,
        password: str | SecretStr,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        max_concurrent: int = 10,
        retry: RetryConfig | None = None,
    ) -> AsyncNiwaki:
        """Create an AsyncNiwaki instance and authenticate immediately (async).

        The async twin of :meth:`Niwaki.connect` — for long-lived services
        that manage the lifecycle themselves rather than through ``async
        with``. The same session construction and login path as the context
        manager is used, so every constructor option behaves identically.
        Pair with :meth:`close` on shutdown.

        Args:
            host: Base URL of the APIC.
            username: APIC username.
            password: APIC password (``str`` or ``pydantic.SecretStr``).
            verify_ssl: TLS verification — ``True``, a PEM CA bundle path,
                or ``False``.  Default: ``True``.
            timeout: HTTP timeout in seconds.  Default: 30.
            refresh_threshold: Proactive refresh threshold in seconds.
                Default: 60.
            max_concurrent: Maximum simultaneous in-flight requests.
                Default: 10.
            retry: Custom retry policy.  Defaults to 3 attempts with
                exponential back-off.

        Returns:
            Authenticated :class:`AsyncNiwaki` instance.

        Raises:
            LoginError: APIC rejected the credentials.
            ConnectionError: APIC host is unreachable.
            TimeoutError: Login request timed out.
            TLSError: TLS certificate verification failed.

        Example::

            aci = await AsyncNiwaki.connect(
                "https://sandboxapicdc.cisco.com",
                "admin",
                "ciscopsdt",
                verify_ssl=False,
            )
            try:
                bds = await aci.query("fvBD").fetch()
            finally:
                await aci.close()
        """
        obj = cls(
            host,
            username,
            password,
            verify_ssl=verify_ssl,
            timeout=timeout,
            refresh_threshold=refresh_threshold,
            max_concurrent=max_concurrent,
            retry=retry,
        )
        return await obj.__aenter__()

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> AsyncNiwaki:
        """Authenticate an async session and return ``self``.

        If :meth:`connect` was used, the session is already authenticated and
        this is a no-op.  Otherwise an
        :class:`~niwaki.transport.session_async.AsyncApicSession` is created
        and logged in.  Re-entering never replaces a live session: doing so
        would strand its HTTP client and any subscription socket with no
        owner left to close them.

        Returns:
            This :class:`AsyncNiwaki` instance.

        Raises:
            LoginError: APIC rejected the credentials.
            ConnectionError: APIC host is unreachable.
            TimeoutError: Login request timed out.
        """
        if self._session is None:
            options = dict(self._session_options)
            if self._injected_client is not None:
                options["client"] = self._injected_client
            session = AsyncApicSession(**options)
            try:
                await session.login()
            except BaseException:
                # The session built its HTTP client eagerly; abandoning it on
                # a failed login leaks the connection pool with no handle left
                # to close it — a service looping on connect() against an
                # unreachable APIC leaks one pool per attempt.
                await session.close()
                raise
            self._session = session
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying async HTTP session."""
        await self.close()

    async def close(self) -> None:
        """Close the async HTTP session and release network resources.

        Called automatically on ``__aexit__``.  Safe to call explicitly when
        not using :class:`AsyncNiwaki` as an async context manager.
        """
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def retry(self) -> RetryConfig | None:
        """Custom retry policy passed at construction time, or ``None`` for the default.

        Returns:
            The :class:`~niwaki.transport.RetryConfig` override, or ``None``
            when the session default (3 attempts) is in use.
        """
        return self._retry

    @classmethod
    def with_client(
        cls,
        client: httpx.AsyncClient,
        host: str | None = None,
        username: str | None = None,
        password: str | SecretStr | None = None,
        **kwargs: Any,  # Any: forwarded verbatim to the constructor, which types each option
    ) -> AsyncNiwaki:
        """Build a client that talks through an ``httpx`` client you configured.

        Everything this SDK does not model — an outbound proxy, mutual TLS, a
        pinned CA bundle, a custom transport, per-route timeouts — is already
        expressible in ``httpx``.  Rather than mirror each as a parameter, this
        hands the whole client over; the returned object is identical in every
        other respect, and closing it closes the client you gave.

        Args:
            client: A configured ``httpx.AsyncClient``.  Pass the same host you
                gave it, since cookies and WebSocket URLs are derived from it.
            host: APIC base URL, or ``None`` to read ``APIC_HOST``.
            username: Login name, or ``None`` to read ``APIC_USERNAME``.
            password: Password (``str`` or ``pydantic.SecretStr``), or
                ``None`` to read ``APIC_PASSWORD``.
            **kwargs: Any other constructor argument, validated as usual.

        Returns:
            A client using *client* for every request.

        Example::

            behind_proxy = httpx.Client(base_url=host, proxy="http://proxy:3128")
            aci = Niwaki.with_client(behind_proxy, host, user, password)
        """
        self = cls(host, username, password, **kwargs)
        self._injected_client = client
        return self

    @property
    def apic_version(self) -> str | None:
        """The firmware the connected controller stated at login.

        Compare it against :func:`niwaki.catalog.schema_version` — the firmware
        this SDK's models and vocabulary were generated from — to know whether
        you are inside the envelope it was built for.  A mismatch is not a
        failure: reads stay tolerant, writes fail loudly.  It is a reason to
        pilot rather than assume.

        Returns:
            The version string, or ``None`` before the client has connected.
        """
        session = self._session
        return None if session is None else session.apic_version

    @property
    def max_concurrent(self) -> int:
        """Upper bound on simultaneous in-flight HTTP requests for this client.

        A staged push inherits this as its own fan-out bound, so
        ``push(mode="staged")`` runs at most this many operations of a wave at
        once.  ``push(..., max_concurrent=n)`` can lower it further but never
        raise it above this number — construct the client with a higher limit
        to go wider.

        Returns:
            The limit set at construction (default ``10``).
        """
        return self._max_concurrent

    @property
    def subscriptions(self) -> AsyncSubscriptionManager:
        """Bulk introspection/management for this session's object-subscriptions.

        Returns:
            An :class:`~niwaki.facade.AsyncSubscriptionManager` over the active session.

        Example::

            async with AsyncNiwaki(...) as aci:
                sub = await aci.query(fvBD).subscribe()
                for info in aci.subscriptions.list():
                    print(info.path, info.is_stale)
                await aci.subscriptions.close_all()
        """
        return AsyncSubscriptionManager(self._active_session)

    # ── Internal session guard ────────────────────────────────────────────────

    @property
    def _active_session(self) -> AsyncApicSession:
        """Return the live session or raise :exc:`~niwaki.exceptions.AuthError`.

        Raises:
            AuthError: Session not initialised — use
                ``async with AsyncNiwaki(...) as aci``.
        """
        from niwaki import exceptions

        if self._session is None:
            raise exceptions.AuthError(
                "Async session not initialised. Use 'async with AsyncNiwaki(...) as aci'."
            )
        return self._session

    # ── Navigation ────────────────────────────────────────────────────────────

    @property
    def root(self) -> AsyncRootNode:
        """Entry point for the ACI object hierarchy.

        Returns:
            :class:`AsyncNiwakiNode` at DN ``"uni"``.  Type checkers see the
            typed root node, whose navigation names are checked and complete.

        Example::

            bd = await aci.root.tenant("prod").bd("web").read()
        """
        return cast("AsyncRootNode", AsyncNiwakiNode(self, "uni", ManagedObject))

    def node[T: ManagedObject](
        self,
        dn: str,
        cls: type[T] = ManagedObject,  # type: ignore[assignment]
    ) -> AsyncNiwakiNode[T]:
        """Access a node by explicit DN.

        Args:
            dn: Full Distinguished Name (e.g. ``"uni/tn-prod/BD-web"``).
            cls: ACI class hint.  Defaults to
                :class:`~niwaki.models.base.ManagedObject`.

        Returns:
            :class:`AsyncNiwakiNode` scoped to the given DN.
        """
        return AsyncNiwakiNode(self, dn, cls)

    if not TYPE_CHECKING:
        # Hidden from the checker, which sees the typed root navigator (the
        # base class) instead: an unknown top-level name is a static error.
        def __getattr__(self, attr: str) -> Any:  # Any: name-driven navigation, resolved at runtime
            """Proxy vocabulary navigation to :attr:`root`.

            Args:
                attr: Jargon method name (e.g. ``"tenant"``).

            Returns:
                Whatever :meth:`AsyncNiwakiNode.__getattr__` returns for the root.

            Raises:
                AttributeError: ``attr`` is not a known top-level child.
            """
            if attr.startswith("_"):
                raise AttributeError(attr)
            return getattr(self.root, attr)

    # ── Queries ───────────────────────────────────────────────────────────────

    @overload
    def query[T: ManagedObject](self, cls: type[T]) -> AsyncQuery[T]: ...

    @overload
    def query(self, cls: str) -> AsyncQuery[ManagedObject]: ...

    def query[T: ManagedObject](self, cls: type[T] | str) -> AsyncQuery[T]:
        """Build a global class query for the entire ACI fabric (async variant).

        Mirrors :meth:`~niwaki.Niwaki.query` for async contexts.  The
        accumulator methods are synchronous; only the execution methods
        (``fetch``, ``first``, ``count``, ``stream``) are coroutines, which
        means they compose naturally with :meth:`gather`.

        Args:
            cls: ACI class type (e.g. ``fvBD``) or plain string class name.

        Returns:
            :class:`~niwaki.query.AsyncQuery` targeting the global class index.

        Example::

            async with AsyncNiwaki(...) as aci:
                # All tenants
                tenants = await aci.query(fvTenant).fetch()

                # Concurrent reads via gather()
                tenants, bds = await aci.gather(
                    aci.query(fvTenant).fetch(),
                    aci.root.tenant("prod").bd().fetch(),
                )

                # Async streaming
                async for bd in aci.query(fvBD).stream():
                    await process(bd)
        """
        from niwaki.query._async_builder import AsyncQuery

        return AsyncQuery(cls, self._active_session)

    # ── Concurrency helpers ───────────────────────────────────────────────────

    @overload
    async def gather[T1](self, c1: _Co[T1], /) -> tuple[T1]: ...

    @overload
    async def gather[T1, T2](self, c1: _Co[T1], c2: _Co[T2], /) -> tuple[T1, T2]: ...

    @overload
    async def gather[T1, T2, T3](
        self, c1: _Co[T1], c2: _Co[T2], c3: _Co[T3], /
    ) -> tuple[T1, T2, T3]: ...

    @overload
    async def gather[T1, T2, T3, T4](
        self, c1: _Co[T1], c2: _Co[T2], c3: _Co[T3], c4: _Co[T4], /
    ) -> tuple[T1, T2, T3, T4]: ...

    @overload
    async def gather[T1, T2, T3, T4, T5](
        self, c1: _Co[T1], c2: _Co[T2], c3: _Co[T3], c4: _Co[T4], c5: _Co[T5], /
    ) -> tuple[T1, T2, T3, T4, T5]: ...

    @overload
    async def gather(
        self, *coros: _Co[object]
    ) -> tuple[Any, ...]: ...  # Any: heterogeneous arity beyond the typed overloads

    async def gather(self, *coros: _Co[object]) -> tuple[object, ...]:
        """Run multiple coroutines concurrently under a structured TaskGroup.

        The APIC concurrency semaphore (``max_concurrent``) is already applied
        at the session level, so each coroutine naturally respects the limit.
        On the first coroutine that raises, ``TaskGroup`` **cancels the others**
        and raises an :class:`ExceptionGroup` (Python 3.11+).  Do not use
        ``gather`` for concurrent *writes*: a failure cancels the in-flight
        siblings, which can leave a second design partially applied.

        Args:
            *coros: Coroutines to run concurrently (e.g. results of
                ``.read()``, ``aci.query(...).fetch()``, ``config.push(aci)``).

        Returns:
            Tuple of results in the same order as the input coroutines.  A
            type checker sees each position's own result type for up to five
            coroutines; beyond that, a ``tuple[Any, ...]``.

        Raises:
            ExceptionGroup: If one or more coroutines raise.  Remaining siblings
                are cancelled (``TaskGroup`` semantics), not awaited.

        Example::

            tenants, bd = await aci.gather(
                aci.query(fvTenant).fetch(),
                aci.root.tenant("prod").bd("web").read(),
            )
        """
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(c) for c in coros]
        return tuple(t.result() for t in tasks)


# ── Niwaki ─────────────────────────────────────────────────────────────────────


class Niwaki(_RootNavBase):
    """Sync entry point for the ACI SDK.

    Use as a sync context manager — authentication happens on ``__enter__``
    and the session is closed on ``__exit__``.  For async code use
    :class:`AsyncNiwaki` instead.

    Use as a **context manager** (recommended)::

        with Niwaki("https://apic.example.com", "admin", "secret") as aci:
            bds = aci.query(fvBD).fetch()

    Use :meth:`connect` for explicit lifecycle management::

        aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
        try:
            bd = aci.root.tenant("prod").bd("web").read()
        finally:
            aci.close()

    Args:
        host: Base URL of the APIC (e.g. ``"https://sandboxapicdc.cisco.com"``).
            Falls back to ``APIC_HOST`` environment variable if omitted.
        username: APIC username. Falls back to ``APIC_USERNAME`` if omitted.
        password: APIC password, as a ``str`` or a ``pydantic.SecretStr``.
            Falls back to ``APIC_PASSWORD`` when omitted or empty, read when
            the session is built (on entry or ``connect()``), not here.  It is
            not stored as a plain attribute: the client keeps a ``SecretStr``,
            masked in ``repr``/``str``, and hands it to the session as is.
        verify_ssl: TLS verification — ``True`` (system CA store), a path to
            a PEM CA bundle (private/enterprise CA), or ``False`` (lab only).
            Default: ``True``.
        timeout: HTTP request timeout in seconds.  Default: 30.
        refresh_threshold: Seconds before token expiry at which a proactive
            refresh is triggered.  Default: 60.
        retry: Custom retry policy.  Defaults to 3 attempts with exponential
            back-off.  Construct with :class:`~niwaki.transport.RetryConfig`.
    """

    def __init__(
        self,
        host: str | None = None,
        username: str | None = None,
        password: str | SecretStr | None = None,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        retry: RetryConfig | None = None,
        token_cache: TokenCache | None = None,
        private_key: str | Path | None = None,
        cert_dn: str | None = None,
    ) -> None:
        # Validated here, where a mistake is written; the password is held as a
        # SecretStr and the environment fallbacks are left to the session, so
        # nothing is read or opened before entry.
        self._session_options = _session_kwargs(
            host=host,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            timeout=timeout,
            refresh_threshold=refresh_threshold,
            retry=retry,
            token_cache=token_cache,
            private_key=private_key,
            cert_dn=cert_dn,
        )
        # The login name is not a secret: kept on the client for inspection.
        self._username = username
        self._retry = retry
        self._injected_client: httpx.Client | None = None
        self._session: ApicSession | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def connect(
        cls,
        host: str,
        username: str,
        password: str | SecretStr,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        retry: RetryConfig | None = None,
    ) -> Niwaki:
        """Create a Niwaki instance and authenticate immediately (sync).

        Equivalent to entering ``Niwaki(...)`` as a sync context manager but
        without the ``with`` statement — the same session construction and
        login path is used, so every constructor option (including *retry*)
        behaves identically.

        Args:
            host: Base URL of the APIC.
            username: APIC username.
            password: APIC password (``str`` or ``pydantic.SecretStr``).
            verify_ssl: TLS verification — ``True``, a PEM CA bundle path,
                or ``False``.  Default: ``True``.
            timeout: HTTP timeout in seconds.  Default: 30.
            refresh_threshold: Proactive refresh threshold in seconds.
                Default: 60.
            retry: Custom retry policy.  Defaults to 3 attempts with
                exponential back-off.

        Returns:
            Authenticated :class:`Niwaki` instance.

        Raises:
            LoginError: APIC rejected the credentials.
            ConnectionError: APIC host is unreachable.
            TimeoutError: Login request timed out.
            TLSError: TLS certificate verification failed.

        Example::

            aci = Niwaki.connect(
                "https://sandboxapicdc.cisco.com",
                "admin",
                "ciscopsdt",
                verify_ssl=False,
            )
        """
        obj = cls(
            host,
            username,
            password,
            verify_ssl=verify_ssl,
            timeout=timeout,
            refresh_threshold=refresh_threshold,
            retry=retry,
        )
        return obj.__enter__()

    # ── Sync context manager ──────────────────────────────────────────────────

    def __enter__(self) -> Niwaki:
        """Authenticate and return ``self`` for sync usage.

        If :meth:`connect` was used, the session is already authenticated and
        this is a no-op.  If ``Niwaki(...)`` was used directly, a new
        :class:`~niwaki.transport.session.ApicSession` is created and logged in.

        Returns:
            This :class:`Niwaki` instance.

        Raises:
            LoginError: APIC rejected the credentials.
            ConnectionError: APIC host is unreachable.
            TimeoutError: Login request timed out.
        """
        if self._session is None:
            options = dict(self._session_options)
            if self._injected_client is not None:
                options["client"] = self._injected_client
            session = ApicSession(**options)
            try:
                session.login()
            except BaseException:
                # The session built its HTTP client eagerly; abandoning it on
                # a failed login leaks the connection pool with no handle left
                # to close it — a service looping on connect() against an
                # unreachable APIC leaks one pool per attempt.
                session.close()
                raise
            self._session = session
        return self

    def __exit__(self, *_: object) -> None:
        """Close the underlying sync HTTP session."""
        self.close()

    def close(self) -> None:
        """Close the sync HTTP session and release network resources.

        Called automatically on ``__exit__``.  Safe to call explicitly when
        not using :class:`Niwaki` as a context manager.  After ``close()``,
        any further operation raises :exc:`~niwaki.exceptions.AuthError`
        (mirrors :meth:`AsyncNiwaki.close`).
        """
        if self._session is not None:
            self._session.close()
            self._session = None

    @property
    def retry(self) -> RetryConfig | None:
        """Custom retry policy passed at construction time, or ``None`` for the default.

        Returns:
            The :class:`~niwaki.transport.RetryConfig` override, or ``None``
            when the session default (3 attempts) is in use.
        """
        return self._retry

    @classmethod
    def with_client(
        cls,
        client: httpx.Client,
        host: str | None = None,
        username: str | None = None,
        password: str | SecretStr | None = None,
        **kwargs: Any,  # Any: forwarded verbatim to the constructor, which types each option
    ) -> Niwaki:
        """Build a client that talks through an ``httpx`` client you configured.

        Everything this SDK does not model — an outbound proxy, mutual TLS, a
        pinned CA bundle, a custom transport, per-route timeouts — is already
        expressible in ``httpx``.  Rather than mirror each as a parameter, this
        hands the whole client over; the returned object is identical in every
        other respect, and closing it closes the client you gave.

        Args:
            client: A configured ``httpx.Client``.  Pass the same host you
                gave it, since cookies and WebSocket URLs are derived from it.
            host: APIC base URL, or ``None`` to read ``APIC_HOST``.
            username: Login name, or ``None`` to read ``APIC_USERNAME``.
            password: Password (``str`` or ``pydantic.SecretStr``), or
                ``None`` to read ``APIC_PASSWORD``.
            **kwargs: Any other constructor argument, validated as usual.

        Returns:
            A client using *client* for every request.

        Example::

            behind_proxy = httpx.Client(base_url=host, proxy="http://proxy:3128")
            aci = Niwaki.with_client(behind_proxy, host, user, password)
        """
        self = cls(host, username, password, **kwargs)
        self._injected_client = client
        return self

    @property
    def apic_version(self) -> str | None:
        """The firmware the connected controller stated at login.

        Compare it against :func:`niwaki.catalog.schema_version` — the firmware
        this SDK's models and vocabulary were generated from — to know whether
        you are inside the envelope it was built for.  A mismatch is not a
        failure: reads stay tolerant, writes fail loudly.  It is a reason to
        pilot rather than assume.

        Returns:
            The version string, or ``None`` before the client has connected.
        """
        session = self._session
        return None if session is None else session.apic_version

    @property
    def subscriptions(self) -> SubscriptionManager:
        """Bulk introspection/management for this session's object-subscriptions.

        Returns:
            A :class:`~niwaki.facade.SubscriptionManager` over the active session.

        Example::

            with Niwaki(...) as aci:
                sub = aci.query(fvBD).subscribe()
                for info in aci.subscriptions.list():
                    print(info.path, info.is_stale)
                aci.subscriptions.close_all()
        """
        return SubscriptionManager(self._sync_session)

    # ── Sync navigation (delegated to NiwakiNode) ─────────────────────────────

    @property
    def _sync_session(self) -> ApicSession:
        """Return the active sync session or raise ``AuthError``.

        Raises:
            AuthError: Session not initialised — use :meth:`connect` or enter
                a sync context manager.
        """
        from niwaki import exceptions

        if self._session is None:
            raise exceptions.AuthError(
                "Sync session not initialised. "
                "Use Niwaki.connect() or enter a sync context manager "
                "(with Niwaki(...) as aci)."
            )
        return self._session

    @property
    def root(self) -> RootNode:
        """Entry point for the ACI object hierarchy.

        Returns a :class:`NiwakiNode` at DN ``"uni"`` — the root of all ACI
        objects.

        Returns:
            :class:`NiwakiNode` with ``dn = "uni"``.  Type checkers see the
            typed root node, whose navigation names are checked and complete.

        Example::

            bd = aci.root.tenant("prod").bd("web").read()
        """
        return cast("RootNode", NiwakiNode(self, "uni", ManagedObject))

    def node[T: ManagedObject](
        self,
        dn: str,
        cls: type[T] = ManagedObject,  # type: ignore[assignment]
    ) -> NiwakiNode[T]:
        """Access a node by explicit DN.

        Args:
            dn: Full Distinguished Name (e.g. ``"uni/tn-prod/BD-web"``).
            cls: ACI class hint.  Defaults to
                :class:`~niwaki.models.base.ManagedObject`.

        Returns:
            :class:`NiwakiNode` scoped to the given DN.

        Example::

            bd = aci.node("uni/tn-prod/BD-web", fvBD).read()
        """
        return NiwakiNode(self, dn, cls)

    if not TYPE_CHECKING:
        # Hidden from the checker, which sees the typed root navigator (the
        # base class) instead: an unknown top-level name is a static error.
        def __getattr__(self, attr: str) -> Any:  # Any: name-driven navigation, resolved at runtime
            """Proxy vocabulary navigation to :attr:`root`.

            Allows top-level shortcuts without an explicit ``.root``::

                bd = aci.tenant("prod").bd("web").read()

            Args:
                attr: Jargon method name.

            Raises:
                AttributeError: ``attr`` is not a known top-level child.
            """
            if attr.startswith("_"):
                raise AttributeError(attr)
            return getattr(self.root, attr)

    @overload
    def query[T: ManagedObject](self, cls: type[T]) -> Query[T]: ...

    @overload
    def query(self, cls: str) -> Query[ManagedObject]: ...

    def query[T: ManagedObject](self, cls: type[T] | str) -> Query[T]:
        """Build a global class query for the entire ACI fabric.

        Returns a :class:`~niwaki.query.Query` builder targeting every instance
        of *cls* across the fabric (``/api/class/{cls}.json``).  Add filters,
        scope constraints, or response enrichment before executing.

        Also accepts a plain string class name for querying any of the ~15 000
        ACI classes — including read-only operational and stats classes that are
        not in the generated set.  Attributes on unregistered classes are
        accessible via ``obj.model_extra["attr"]`` or directly as
        ``obj.attr_name`` (Pydantic ``extra="allow"``).

        Args:
            cls: ACI class type (e.g. ``fvBD``) or plain string class name
                 (e.g. ``"topSystem"``).

        Returns:
            :class:`~niwaki.query.Query` targeting the global class index.

        Example::

            with Niwaki(...) as aci:
                # All tenants
                tenants = aci.query(fvTenant).fetch()

                # Filtered
                bds = aci.query(fvBD).where(arpFlood=True).fetch()

                # Scoped to a DN
                bds = aci.query(fvBD).under("uni/tn-prod").fetch()

                # Count only
                n = aci.query(fvBD).count()

                # Unregistered / read-only class
                nodes = aci.query("topSystem").naming_only().fetch()
        """
        from niwaki.query._builder import Query

        return Query(cls, self._sync_session)
