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
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, NamedTuple, overload

from niwaki.models.base import ManagedObject
from niwaki.transport._config import RetryConfig
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession

if TYPE_CHECKING:
    from niwaki.query._async_builder import AsyncQuery
    from niwaki.query._builder import Query

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

    from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, RS_TARGET_PROP

    parent_key = "_root" if parent_cls is ManagedObject else parent_cls.__name__
    children = CHILD_MAP.get(parent_key, {})

    if attr not in children:
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


def _make_jargon_navigator(
    node: NiwakiNode[Any] | AsyncNiwakiNode[Any],
    child_cls: type[ManagedObject],
    naming_props: list[str],
    is_rs_target: bool,
) -> Any:
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

        def _nav_rs_singleton(target_name: str = "", **kwargs: Any) -> Any:
            # D2 renames every tn*Name target prop to the Python field
            # "name", so one constructor shape covers all Rs classes.
            if target_name:
                kwargs = {"name": target_name, **kwargs}
            return node.mo(child_cls, **kwargs)

        return _nav_rs_singleton

    if not naming_props:

        def _nav_singleton() -> Any:
            return node.mo(child_cls)

        return _nav_singleton

    if naming_props == ["name"]:

        def _nav_name(name: str = "") -> Any:
            if name:
                return node.mo(child_cls, name=name)
            return node.query(child_cls)

        return _nav_name

    def _nav_kwargs(**naming_kwargs: Any) -> Any:
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

    def __getattr__(self, attr: str) -> Any:
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

    def mo[U: ManagedObject](self, cls: type[U], **naming_kwargs: Any) -> NiwakiNode[U]:
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
        instance = cls(**naming_kwargs)
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

            # Unregistered / read-only class
            nodes = aci.root.query("topSystem").naming_only().fetch()
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

    def mo[U: ManagedObject](self, cls: type[U], **naming_kwargs: Any) -> AsyncNiwakiNode[U]:
        """Descend to a child node, computing its DN automatically.

        Args:
            cls: ACI class of the child object.
            **naming_kwargs: Naming props required by ``cls``.

        Returns:
            A new :class:`AsyncNiwakiNode` scoped to the child DN.

        Raises:
            ValidationError: If ``naming_kwargs`` violate model constraints.
        """
        instance = cls(**naming_kwargs)
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


# ── AsyncNiwaki ────────────────────────────────────────────────────────────────


class AsyncNiwaki:
    """Async entry point for the ACI SDK.

    Mirrors :class:`Niwaki` for async code.  Use as an async context manager —
    authentication happens on ``__aenter__`` and the session is closed on
    ``__aexit__``.

    Args:
        host: Base URL of the APIC (e.g. ``"https://sandboxapicdc.cisco.com"``).
            Falls back to ``APIC_HOST`` environment variable if omitted.
        username: APIC username. Falls back to ``APIC_USERNAME`` if omitted.
        password: APIC password. Falls back to ``APIC_PASSWORD`` if omitted.
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
        password: str | None = None,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        max_concurrent: int = 10,
        retry: RetryConfig | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._refresh_threshold = refresh_threshold
        self._max_concurrent = max_concurrent
        self._retry = retry
        self._session: AsyncApicSession | None = None

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> AsyncNiwaki:
        """Authenticate an async session and return ``self``.

        Creates an :class:`~niwaki.transport.session_async.AsyncApicSession`,
        logs in, and returns this :class:`AsyncNiwaki` instance ready for use.

        Returns:
            This :class:`AsyncNiwaki` instance.

        Raises:
            LoginError: APIC rejected the credentials.
            ConnectionError: APIC host is unreachable.
            TimeoutError: Login request timed out.
        """
        kwargs: dict[str, Any] = dict(
            host=self._host,
            username=self._username,
            password=self._password,
            verify_ssl=self._verify_ssl,
            timeout=self._timeout,
            refresh_threshold=self._refresh_threshold,
            max_concurrent=self._max_concurrent,
        )
        if self._retry is not None:
            kwargs["retry"] = self._retry
        self._session = AsyncApicSession(**kwargs)
        await self._session.login()
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
    def root(self) -> AsyncNiwakiNode[ManagedObject]:
        """Entry point for the ACI object hierarchy.

        Returns:
            :class:`AsyncNiwakiNode` at DN ``"uni"``.

        Example::

            bd = await aci.root.tenant("prod").bd("web").read()
        """
        return AsyncNiwakiNode(self, "uni", ManagedObject)

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

    def __getattr__(self, attr: str) -> Any:
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

    async def gather(self, *coros: Coroutine[Any, Any, Any]) -> tuple[Any, ...]:
        """Run multiple coroutines concurrently under a structured TaskGroup.

        The APIC concurrency semaphore (``max_concurrent``) is already applied
        at the session level, so each coroutine naturally respects the limit.
        All errors from individual coroutines are collected and raised together
        as an :class:`ExceptionGroup` (Python 3.11+).

        Args:
            *coros: Coroutines to run concurrently (e.g. results of
                ``.read()``, ``aci.query(...).fetch()``, ``config.push(aci)``).

        Returns:
            Tuple of results in the same order as the input coroutines.

        Raises:
            ExceptionGroup: If one or more coroutines raise, all exceptions are
                grouped and raised together after all coroutines complete.

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


class Niwaki:
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
        password: APIC password. Falls back to ``APIC_PASSWORD`` if omitted.
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
        password: str | None = None,
        *,
        verify_ssl: bool | str = True,
        timeout: float = 30.0,
        refresh_threshold: int = 60,
        retry: RetryConfig | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._refresh_threshold = refresh_threshold
        self._retry = retry
        self._session: ApicSession | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def connect(
        cls,
        host: str,
        username: str,
        password: str,
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
            password: APIC password.
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
        """
        if self._session is None:
            kwargs: dict[str, Any] = dict(
                host=self._host,
                username=self._username,
                password=self._password,
                verify_ssl=self._verify_ssl,
                timeout=self._timeout,
                refresh_threshold=self._refresh_threshold,
            )
            if self._retry is not None:
                kwargs["retry"] = self._retry
            session = ApicSession(**kwargs)
            session.login()
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
    def root(self) -> NiwakiNode[ManagedObject]:
        """Entry point for the ACI object hierarchy.

        Returns a :class:`NiwakiNode` at DN ``"uni"`` — the root of all ACI
        objects.

        Returns:
            :class:`NiwakiNode` with ``dn = "uni"``.

        Example::

            bd = aci.root.tenant("prod").bd("web").read()
        """
        return NiwakiNode(self, "uni", ManagedObject)

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

    def __getattr__(self, attr: str) -> Any:
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
