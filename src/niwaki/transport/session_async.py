"""
Asynchronous APIC session — authentication, token management, HTTP transport.

Architecture mirrors the synchronous :mod:`~niwaki.transport.session` module:
- ``_http_transport()``: async context manager wrapping httpx errors into typed
  niwaki exceptions.
- ``_request_with_retry()``: reads and writes share one stamina retry path.
- ``_ensure_token()``: proactive refresh guarded by an :class:`asyncio.Lock` to
  prevent concurrent refresh races when multiple coroutines share a session.
- ``_request_checked()``: composes the three layers above and gates all requests behind
  a :class:`asyncio.Semaphore` to honour APIC concurrent-connection limits.
"""

from __future__ import annotations

import asyncio
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar, cast

import httpx
import stamina

from niwaki import exceptions
from niwaki.models.base import ManagedObject
from niwaki.transport._config import RetryConfig
from niwaki.transport._errors import extract_apic_error, raise_for_apic_status
from niwaki.transport._subscription_socket import SubscriptionInfo
from niwaki.transport._subscription_socket_async import (
    AsyncRawSubscription,
    AsyncSubscriptionSocket,
)
from niwaki.transport._token import TokenState
from niwaki.utils.response import parse_imdata

_T = TypeVar("_T", bound=ManagedObject)

# Safety limit: 2000 pages x 500 objects = 1 000 000 objects per query.
# Exceeds any real ACI fabric; prevents runaway loops on corrupted totalCount.
_MAX_PAGINATION_PAGES: int = 2000
_DEFAULT_RETRY: RetryConfig = RetryConfig()
# Writes are only safe to retry on errors that provably occurred BEFORE the
# request reached the server (connection/pool).  A read/write timeout may mean
# the APIC accepted the write, so retrying could double-apply or 404 (audit T2).
_WRITE_SAFE_RETRY: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


class AsyncApicSession:
    """Asynchronous APIC session with automatic token management.

    Mirrors :class:`~niwaki.transport.session.ApicSession` for use in async
    contexts.  All public methods are coroutines.  Concurrent requests are
    rate-limited by a :class:`asyncio.Semaphore`; token refresh is protected
    by an :class:`asyncio.Lock` so exactly one refresh runs at a time even
    when many coroutines call ``_ensure_token`` simultaneously.

    Designed for use as an async context manager or standalone::

        async with AsyncApicSession("https://apic.example.com", "admin", "pass") as s:
            data = await s.get("/api/class/fvTenant.json")

        # Standalone:
        s = AsyncApicSession(host="https://apic.example.com")
        await s.login()
        data = await s.get("/api/class/fvTenant.json")
        await s.close()

    Environment variable fallbacks (used when arguments are not provided):
        APIC_HOST     : Base URL of the APIC.
        APIC_USERNAME : APIC username.
        APIC_PASSWORD : APIC password.

    Args:
        host: Base URL of the APIC (e.g. ``"https://sandboxapicdc.cisco.com"``).
            Falls back to ``APIC_HOST`` if omitted.
        username: APIC username. Falls back to ``APIC_USERNAME`` if omitted.
        password: APIC password. Falls back to ``APIC_PASSWORD`` if omitted.
        verify_ssl: TLS verification — ``True`` (system CA store), a path to
            a PEM CA bundle (private CA), or ``False``. Default: ``True``.
        timeout: HTTP timeout in seconds. Default: 30.
        refresh_threshold: Seconds before token expiry at which a proactive
            refresh is triggered. Default: 60.
        max_concurrent: Maximum number of simultaneous in-flight HTTP requests.
            APIC rejects excessive concurrent connections; this semaphore
            prevents that. Default: 10.

    Raises:
        KeyError: If any of host/username/password are omitted and the
            corresponding environment variables are not set.

    Note:
        Not safe for concurrent use from *multiple event loops*.  Each event
        loop should create its own session.
    """

    _LOGIN_PATH: str = "/api/aaaLogin.json"
    _REFRESH_PATH: str = "/api/aaaRefresh.json"

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
        retry: RetryConfig = _DEFAULT_RETRY,
    ) -> None:
        self._host = (host or os.environ["APIC_HOST"]).rstrip("/")
        self._username = username or os.environ["APIC_USERNAME"]
        self._password = password or os.environ["APIC_PASSWORD"]
        self._refresh_threshold = timedelta(seconds=refresh_threshold)
        self._retry = retry
        self._token_state: TokenState | None = None
        self._token_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = httpx.AsyncClient(
            base_url=self._host,
            # httpx 0.28 deprecates verify=<str>; build the SSL context here.
            verify=(
                ssl.create_default_context(cafile=verify_ssl)
                if isinstance(verify_ssl, str)
                else verify_ssl
            ),
            timeout=timeout,
        )
        # Reused for the subscription WebSocket (wss://), which needs a real
        # ssl.SSLContext rather than httpx's bool-or-path verify shorthand.
        self._ws_ssl_context = self._build_ws_ssl_context(verify_ssl)
        self._subscription_socket: AsyncSubscriptionSocket | None = None

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> AsyncApicSession:
        """Authenticate the session and return ``self``.

        Returns:
            The authenticated session, ready for requests.

        Raises:
            LoginError: If credentials are rejected.
            ConnectionError: If the APIC host is unreachable.
        """
        await self.login()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP client."""
        await self.close()

    async def close(self) -> None:
        """Close the async HTTP client and release network resources.

        Call explicitly when not using the session as an async context manager.
        After ``close()``, any request will raise an httpx error. Also tears
        down the subscription WebSocket, if one was ever opened — every
        blocked subscription iterator ends with a plain ``StopAsyncIteration``.
        """
        if self._subscription_socket is not None:
            await self._subscription_socket.aclose()
            self._subscription_socket = None
        await self._client.aclose()

    # ── Public state ──────────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        """``True`` after :meth:`close` has been called.

        Returns:
            Whether the underlying httpx async client has been closed.
        """
        return self._client.is_closed

    @property
    def is_authenticated(self) -> bool:
        """``True`` once :meth:`login` has succeeded and the token is valid.

        Returns:
            Whether the session holds a live authentication token.
        """
        return self._token_state is not None

    @property
    def retry(self) -> RetryConfig:
        """Active retry policy for this session.

        Returns:
            The :class:`~niwaki.transport.RetryConfig` in use.
        """
        return self._retry

    # ── Public auth ───────────────────────────────────────────────────────────

    async def login(self) -> None:
        """Authenticate against the APIC via ``/api/aaaLogin.json``.

        Submits credentials, stores the returned token with its TTL, and sets
        the ``APIC-cookie`` cookie on the underlying HTTP client.

        Raises:
            LoginError: The APIC rejected the credentials or the response is malformed.
            ConnectionError: The APIC host is unreachable.
            TimeoutError: The login request exceeded the configured timeout.
            TLSError: TLS verification failed.

        Example::

            session = AsyncApicSession("https://apic.example.com", "admin", "pass")
            await session.login()
        """
        payload: dict[str, Any] = {
            "aaaUser": {"attributes": {"name": self._username, "pwd": self._password}}
        }

        async with self._http_transport():
            resp = await self._client.post(self._LOGIN_PATH, json=payload)

        if resp.status_code != 200:
            raise exceptions.LoginError(
                f"Login rejected by APIC (HTTP {resp.status_code}): {extract_apic_error(resp)}"
            )

        self._token_state = self._parse_token_response(resp, threshold=self._refresh_threshold)
        self._client.cookies.set("APIC-cookie", self._token_state.token)

    # ── Internal auth ─────────────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        """Refresh the session token via ``/api/aaaRefresh.json``.

        Raises:
            AuthError: No active token (``login()`` not called yet).
            TokenRefreshError: The APIC rejected the refresh request.
        """
        if self._token_state is None:
            raise exceptions.AuthError("Cannot refresh: no active session. Call login() first.")

        async with self._http_transport():
            resp = await self._client.get(self._REFRESH_PATH)

        if resp.status_code != 200:
            raise exceptions.TokenRefreshError(
                f"Token refresh failed (HTTP {resp.status_code}): {extract_apic_error(resp)}"
            )

        self._token_state = self._parse_token_response(resp, threshold=self._refresh_threshold)
        self._client.cookies.set("APIC-cookie", self._token_state.token)

    async def _ensure_token(self) -> None:
        """Ensure the token is valid before issuing a request.

        Protected by an :class:`asyncio.Lock` so that when many coroutines
        call this concurrently, only the first one performs the refresh;
        subsequent waiters see the fresh token after acquiring the lock.

        Proactive refresh strategy:

        1. No token → ``AuthError``.
        2. Token expired → re-login.
        3. Token within refresh threshold → refresh, re-login as fallback.
        4. Token OK → no-op.

        Raises:
            AuthError: ``login()`` has not been called yet.
            SessionExpiredError: Token expired and re-auth failed.
        """
        async with self._token_lock:
            if self._token_state is None:
                raise exceptions.AuthError(
                    "Not authenticated. Call login() or use the context manager."
                )

            if self._token_state.is_expired():
                await self._relogin(reason="token expired")
                return

            if self._token_state.needs_refresh():
                try:
                    await self._refresh_token()
                except exceptions.TokenRefreshError:
                    await self._relogin(reason="refresh failed")

    async def _relogin(self, reason: str) -> None:
        """Attempt a full re-login, wrapping ``LoginError`` into ``SessionExpiredError``.

        Args:
            reason: Reason for re-login (included in the error message).

        Raises:
            SessionExpiredError: If ``login()`` fails.
        """
        try:
            await self.login()
        except exceptions.LoginError as exc:
            raise exceptions.SessionExpiredError(
                f"Session cannot be renewed ({reason}): {exc}"
            ) from exc

    async def _reactive_relogin(self, stale_token: str | None) -> None:
        """Serialise a re-login triggered by a mid-session 401.

        When many coroutines share a session and its token is revoked, they
        each receive a 401 at once.  Routing every one straight into
        :meth:`login` would stampede concurrent logins racing on
        ``_token_state`` and the cookie jar — the very race
        :meth:`_ensure_token` prevents on the proactive path.  This guard takes
        the token lock and re-logs in **only** when the live token still equals
        the one that failed; a coroutine that finds a newer token (another
        already re-authenticated) returns without a second login.

        Args:
            stale_token: The token in force when the failing request was sent.

        Raises:
            SessionExpiredError: The re-login attempt was rejected.
        """
        async with self._token_lock:
            current = self._token_state.token if self._token_state else None
            if current is not None and current != stale_token:
                return  # another coroutine already refreshed — its token is live
            await self._relogin(reason="mid-session 401")

    # ── Public GET ────────────────────────────────────────────────────────────

    async def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a GET against the APIC REST API and return the ``imdata`` list.

        Ensures token validity, applies the concurrency semaphore, and retries
        on transient network errors. Handles mid-session 401s by re-authenticating
        and replaying once.

        Args:
            path: API path relative to the base URL
                (e.g. ``"/api/class/fvTenant.json"``).
            **params: Optional query string parameters.

        Returns:
            The ``imdata`` list from the APIC JSON response.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            NotFoundError: HTTP 404.
            UnauthorizedError: HTTP 401 persisting after re-auth.
            ForbiddenError: HTTP 403.
            ServerError: HTTP 5xx.
            ConnectionError: Host unreachable after all retries.
            TimeoutError: Timeout exceeded after all retries.
            TLSError: TLS verification error.

        Example::

            async with AsyncApicSession("https://apic.example.com", "admin", "pass") as s:
                tenants = await s.get("/api/class/fvTenant.json")
        """
        return await self._get_imdata(path, dict(params))

    async def get_mo(
        self,
        dn: str,
        cls: type[_T] = ManagedObject,  # type: ignore[assignment]
    ) -> _T:
        """Fetch a single MO by DN, typed as *cls*.

        Part of the transport boundary (:class:`niwaki.transport._protocols.AsyncMoReader`).

        Args:
            dn: Full Distinguished Name of the object.
            cls: Model class used to deserialise the response.

        Returns:
            The typed instance.

        Raises:
            NotFoundError: No object exists at *dn*.
        """
        raw = await self._get_imdata(f"/api/mo/{dn}.json", {})
        objects = parse_imdata({"imdata": raw})
        if not objects:
            raise exceptions.NotFoundError(404, f"MO not found at DN: {dn!r}")
        return cast(_T, objects[0])

    # ── Public subscribe ──────────────────────────────────────────────────────

    async def subscribe(
        self, path: str, params: dict[str, str], *, refresh_timeout: int | None = None
    ) -> AsyncRawSubscription:
        """
        Subscribe to push notifications for a query, over the session's shared WebSocket.

        Part of the transport boundary
        (:class:`niwaki.transport._protocols.AsyncMoSubscriber`). Mirrors
        :meth:`niwaki.transport.session.ApicSession.subscribe`: the APIC
        multiplexes every subscription for a session over one WebSocket,
        opened lazily on the first call to this method; a refresh sweep and
        reconnect-and-resubscribe are handled automatically in the
        background — a caller never hand-rolls either.

        Args:
            path: API path relative to base URL, exactly as passed to
                :meth:`get` (e.g. ``"/api/class/fvBD.json"``).
            params: Query string parameters (filters/scoping). ``subscription``
                and ``refresh-timeout`` are added internally — do not include
                them here.
            refresh_timeout: Override the APIC's default 60 s subscription
                timeout. The subscription refreshes itself automatically on a
                schedule derived from this value regardless.

        Returns:
            A :class:`~niwaki.transport._subscription_socket_async.AsyncRawSubscription`
            — ``.initial`` for the synchronous snapshot, then iterate
            (``async for``) for live push items.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            SubscribeRejectedError: The APIC rejected the subscribe request.

        Example::

            sub = await session.subscribe("/api/class/fvBD.json", {})
            async for item in sub:
                print(item)
        """
        await self._ensure_token()
        if self._subscription_socket is None:
            self._subscription_socket = AsyncSubscriptionSocket(self)
        return await self._subscription_socket.subscribe(
            path, params, refresh_timeout=refresh_timeout
        )

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List every subscription currently tracked on this session's socket.

        Returns an empty list if no subscription was ever opened — this never
        opens the WebSocket itself. Synchronous (matches the sync
        :class:`~niwaki.transport.session.ApicSession` accessor and the
        underlying lock-free read) even on this async session.

        Returns:
            One :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`
            per tracked subscription.
        """
        if self._subscription_socket is None:
            return []
        return self._subscription_socket.list_subscriptions()

    async def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        A no-op returning an empty list if no subscription was ever opened.
        See :class:`~niwaki.transport._subscription_socket.SubscriptionSocket`'s
        method of the same name for the escalation-safety semantics.

        Returns:
            The post-refresh snapshot of every subscription.
        """
        if self._subscription_socket is None:
            return []
        return await self._subscription_socket.refresh_all_subscriptions()

    async def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open.

        A no-op if no subscription was ever opened. Distinct from
        :meth:`close`, which tears down the whole socket; see
        :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.close_all_subscriptions`.
        """
        if self._subscription_socket is not None:
            await self._subscription_socket.close_all_subscriptions()

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    async def _request_checked(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """Execute an authenticated GET with semaphore, retry, and 401 handling.

        Args:
            path: API path relative to base URL.
            params: Query string parameters dict.

        Returns:
            Validated ``httpx.Response`` (non-2xx raises immediately).

        Raises:
            See :meth:`get`.
        """
        await self._ensure_token()

        async with self._semaphore, self._http_transport():
            stale = self._token_state.token if self._token_state else None
            resp = await self._request_with_retry("GET", path, params=params)

        if resp.status_code == 401:
            await self._reactive_relogin(stale)
            async with self._semaphore, self._http_transport():
                resp = await self._client.get(path, params=params)

        raise_for_apic_status(resp)
        return resp

    async def _get_imdata(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a GET and return the raw ``imdata`` list (single page).

        Args:
            path: API path relative to base URL.
            params: Query string parameters dict.

        Returns:
            The ``imdata`` list from the APIC JSON response.
        """
        return (await self._request_checked(path, params)).json().get("imdata", [])  # type: ignore[no-any-return]

    async def _aiter_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield one page of raw APIC ``imdata`` items at a time (async).

        Async counterpart of :meth:`~niwaki.transport.session.ApicSession._iter_pages`.
        Fetches page 0 first, then subsequent pages until ``totalCount`` is
        satisfied.  Each ``yield`` hands back one page so callers can process
        objects incrementally without holding all results in memory.

        Args:
            path: API path relative to base URL.
            params: Base query string parameters.  Must not contain ``"page"``.
            page_size: Objects per page.  Default: 500.

        Yields:
            One page (``list[dict]``) per APIC response, in order.

        Raises:
            ServerError: Pagination guard exceeded.
            See :meth:`get` for transport / auth errors.
        """
        page_params = {**params, "page": "0", "page-size": str(page_size)}
        data: dict[str, Any] = (await self._request_checked(path, page_params)).json()
        # Treat an absent totalCount as "unknown" and page until an empty
        # page; never let a missing/zero totalCount stop after page 0 when
        # a full first page came back (audit T3).
        total_raw = data.get("totalCount")
        total = int(total_raw) if total_raw is not None else None
        first: list[dict[str, Any]] = list(data.get("imdata", []))
        if not first:
            return
        yield first

        fetched = len(first)
        page = 1
        while total is None or fetched < total:
            if page > _MAX_PAGINATION_PAGES:
                raise exceptions.ServerError(
                    0,
                    f"Pagination guard: fetched {page} pages but totalCount={total} "
                    "was not satisfied. Possible APIC response inconsistency.",
                )
            page_params = {**params, "page": str(page), "page-size": str(page_size)}
            batch: list[dict[str, Any]] = (
                (await self._request_checked(path, page_params)).json().get("imdata", [])
            )
            if not batch:
                break
            yield batch
            fetched += len(batch)
            page += 1

    async def _get_all_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch all pages of results, auto-paginating based on ``totalCount``.

        When ``page`` is already in ``params``, delegates to :meth:`_get_imdata`
        and returns a single page unchanged (manual pagination).  Otherwise
        delegates to :meth:`_aiter_pages` and flattens results into one list.

        Args:
            path: API path relative to base URL.
            params: Base query string parameters.
            page_size: Objects per page for auto-paginated requests.

        Returns:
            Complete flattened list of all ``imdata`` items across all pages.

        Raises:
            See :meth:`get`.
        """
        if "page" in params:
            return await self._get_imdata(path, params)
        result: list[dict[str, Any]] = []
        async for page in self._aiter_pages(path, params, page_size=page_size):
            result.extend(page)
        return result

    # ── Public write ──────────────────────────────────────────────────────────

    async def post_mo(self, dn: str, payload: dict[str, Any]) -> None:
        """POST an APIC envelope to a Managed Object URL.

        APIC applies upsert semantics — the object is created if absent or
        updated if it exists.

        Args:
            dn: Full Distinguished Name of the target object.
            payload: APIC envelope dict as produced by
                :meth:`~niwaki.models.ManagedObject.to_apic`.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            ForbiddenError: HTTP 403.
            NotFoundError: HTTP 404 — invalid DN structure.
            ServerError: HTTP 5xx.
            ConnectionError: Network error after all retries.
            TimeoutError: Timeout exceeded.
            TLSError: TLS verification error.

        Example::

            await session.post_mo("uni/tn-prod/BD-web", bd.to_apic())
        """
        await self._raw_write("POST", f"/api/mo/{dn}.json", json=payload)

    async def delete_mo(self, dn: str) -> None:
        """DELETE a Managed Object by Distinguished Name.

        Args:
            dn: Full Distinguished Name of the object to delete.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            NotFoundError: HTTP 404.
            ForbiddenError: HTTP 403.
            ServerError: HTTP 5xx.
            ConnectionError: Network error after all retries.
            TimeoutError: Timeout exceeded.
            TLSError: TLS verification error.

        Example::

            await session.delete_mo("uni/tn-prod/BD-web")
        """
        await self._raw_write("DELETE", f"/api/mo/{dn}.json")

    async def _raw_write(self, method: str, path: str, **kwargs: Any) -> None:
        """Execute a mutating request (POST or DELETE) with auth, semaphore, and retry.

        Args:
            method: HTTP method (``"POST"`` or ``"DELETE"``).
            path: API path relative to base URL.
            **kwargs: Forwarded to ``httpx.AsyncClient.request``.

        Raises:
            See :meth:`post_mo`.
        """
        await self._ensure_token()

        async with self._semaphore, self._http_transport():
            stale = self._token_state.token if self._token_state else None
            resp = await self._request_with_retry(
                method, path, retry_on=_WRITE_SAFE_RETRY, **kwargs
            )

        if resp.status_code == 401:
            await self._reactive_relogin(stale)
            async with self._semaphore, self._http_transport():
                resp = await self._client.request(method, path, **kwargs)

        raise_for_apic_status(resp)

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        retry_on: type[Exception] | tuple[type[Exception], ...] = httpx.TransportError,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute a request with stamina retry on transient network errors.

        Shared by reads and writes.  Uses a nested async function decorated
        with ``stamina.retry`` so the decorator handles async callables
        correctly.  Only ``httpx.TransportError`` triggers a retry; HTTP
        errors (4xx, 5xx) are returned unchanged for the caller to handle.

        Args:
            method: HTTP method string.
            path: Relative API path.
            **kwargs: Forwarded to ``httpx.AsyncClient.request``
                (e.g. ``params=...``, ``json=...``).

        Returns:
            Raw httpx response (HTTP errors not checked here).
        """

        @stamina.retry(
            on=retry_on,
            attempts=self._retry.attempts,
            wait_initial=self._retry.wait_initial,
            wait_max=self._retry.wait_max,
            wait_jitter=self._retry.wait_jitter,
        )
        async def _attempt() -> httpx.Response:
            return await self._client.request(method, path, **kwargs)

        return await _attempt()

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _http_transport(self) -> AsyncIterator[None]:
        """Async context manager converting httpx errors into typed niwaki exceptions.

        Raises:
            TimeoutError: On ``httpx.TimeoutException``.
            TLSError: On ``httpx.ConnectError`` caused by an SSL error.
            ConnectionError: On non-SSL ``httpx.ConnectError``.
            TransportError: On any other ``httpx.TransportError``.
        """
        try:
            yield
        except httpx.TimeoutException as exc:
            raise exceptions.TimeoutError(f"APIC request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, ssl.SSLError):
                raise exceptions.TLSError(
                    f"TLS/SSL error connecting to {self._host}: {cause}"
                ) from exc
            raise exceptions.ConnectionError(f"Cannot reach APIC at {self._host}: {exc}") from exc
        except httpx.TransportError as exc:
            raise exceptions.TransportError(str(exc)) from exc

    # ── Response parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _build_ws_ssl_context(verify_ssl: bool | str) -> ssl.SSLContext | None:
        """Build the ``ssl.SSLContext`` the subscription WebSocket connects with.

        Mirrors :meth:`niwaki.transport.session.ApicSession._build_ws_ssl_context`
        — translates ``verify_ssl`` to what ``websockets.asyncio.client.connect``
        expects (a real context, not httpx's bool-or-path verify shorthand).

        Args:
            verify_ssl: Same argument as this class's constructor.

        Returns:
            ``None`` for ``verify_ssl=True`` (``websockets`` builds its own
            default verifying context for a ``wss://`` URL); a permissive,
            non-verifying context for ``verify_ssl=False`` (self-signed lab
            certificates); a context pinned to the given CA bundle for a
            ``str`` path.
        """
        if isinstance(verify_ssl, str):
            return ssl.create_default_context(cafile=verify_ssl)
        if verify_ssl is False:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return None

    @staticmethod
    def _parse_token_response(resp: httpx.Response, *, threshold: timedelta) -> TokenState:
        """Extract the token and TTL from an APIC login or refresh response.

        Args:
            resp: Raw httpx response from ``/api/aaaLogin.json`` or
                ``/api/aaaRefresh.json``.
            threshold: Refresh threshold to pass to the constructed ``TokenState``.

        Returns:
            A freshly constructed ``TokenState``.

        Raises:
            LoginError: If the response structure is unexpected.
        """
        try:
            data: dict[str, Any] = resp.json()
            inner: dict[str, Any] = data["imdata"][0]
            login_or_refresh: dict[str, Any] = (
                inner.get("aaaLogin") or inner.get("aaaRefresh") or {}
            )  # pyright: ignore[reportUnknownVariableType]
            attrs: dict[str, Any] = login_or_refresh.get("attributes", {})
            token: str = attrs["token"]  # pyright: ignore[reportUnknownVariableType]
            ttl: int = int(attrs["refreshTimeoutSeconds"])  # pyright: ignore[reportUnknownVariableType]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise exceptions.LoginError(f"Unexpected APIC response structure: {exc}") from exc

        return TokenState.from_apic_response(
            token=token,
            refresh_timeout_seconds=ttl,
            refresh_threshold=threshold,
        )
