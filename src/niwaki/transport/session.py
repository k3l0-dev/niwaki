"""
Synchronous APIC session — authentication, token management, HTTP transport.

Architecture:
- ``_http_transport()``: context manager that wraps all httpx errors into typed
  niwaki exceptions (ConnectionError, TimeoutError, TLSError).
- ``_request_with_retry()``: reads and writes share one stamina retry path.
- ``_ensure_token()``: proactive refresh strategy before each request.
- ``get()``: public entry point, composes the three layers above.
"""

from __future__ import annotations

import os
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

import httpx
import stamina

from niwaki import exceptions
from niwaki.models.base import ManagedObject
from niwaki.transport._config import RetryConfig
from niwaki.transport._errors import (
    _imdata_attributes,
    extract_apic_error,
    raise_for_apic_status,
)
from niwaki.transport._retry import (
    READ_RETRY_STATUSES,
    WRITE_RETRY_STATUSES,
    RetryableStatus,
    retry_after_seconds,
)
from niwaki.transport._signature import CertificateAuth, load_private_key
from niwaki.transport._subscription_socket import (
    _DEFAULT_MAX_PENDING,
    RawSubscription,
    SubscriptionInfo,
    SubscriptionSocket,
)
from niwaki.transport._token import TokenState
from niwaki.transport._token_cache import TokenCache
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


class ApicSession:
    """
    Synchronous APIC session with automatic token management.

    Handles login, proactive token refresh, and transparent re-authentication.
    Designed for use as a context manager or standalone.

    Authentication and token lifecycle::

        with ApicSession("https://apic.example.com", "admin", "pass") as s:
            imdata = s.get("/api/class/fvTenant.json")

        # Standalone usage:
        s = ApicSession(host="https://apic.example.com")
        s.login()
        imdata = s.get("/api/mo/uni/tn-Prod.json")
        s.close()

    Environment variable fallbacks (used when arguments are not provided):
        APIC_HOST     : Base URL of the APIC.
        APIC_USERNAME : APIC username.
        APIC_PASSWORD : APIC password.

    Args:
        host: Base URL of the APIC (e.g. ``"https://sandboxapicdc.cisco.com"``).
            Falls back to ``APIC_HOST`` environment variable if omitted.
        username: APIC username. Falls back to ``APIC_USERNAME`` if omitted.
        password: APIC password. Falls back to ``APIC_PASSWORD`` if omitted.
        verify_ssl: TLS verification — ``True`` verifies against the system CA
            store, a path to a PEM CA bundle verifies against a private CA,
            ``False`` disables verification. Keep ``False`` for APICs
            with self-signed certificates (not recommended in production).
            Default: ``True``.
        timeout: HTTP timeout in seconds (connect + read). Default: 30.
        refresh_threshold: Seconds before token expiry at which a proactive
            refresh is triggered. Default: 60.

    Raises:
        KeyError: If ``host``, ``username``, or ``password`` are omitted and
            the corresponding environment variables are not set.

    Note:
        This implementation is not thread-safe: only the token refresh is
        lock-protected, not the underlying client state. For concurrent
        usage, create one session per thread — or use
        :class:`~niwaki.transport.session_async.AsyncApicSession`.
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
        retry: RetryConfig = _DEFAULT_RETRY,
        client: httpx.Client | None = None,
        token_cache: TokenCache | None = None,
        private_key: str | Path | None = None,
        cert_dn: str | None = None,
    ) -> None:
        self._host = (host or os.environ["APIC_HOST"]).rstrip("/")
        self._username = username or os.environ["APIC_USERNAME"]
        # A signed session never sends a password, so requiring one would make
        # certificate auth impossible in the very place it is wanted: CI, where
        # there is deliberately no password to put in the environment.
        if private_key is not None:
            self._password = password or ""
        else:
            self._password = password or os.environ["APIC_PASSWORD"]
        # Pin the cookie jar entry to the session's own host so it overwrites
        # in place rather than accumulating a second, domain-less entry next
        # to the one httpx auto-stores from the APIC's own Set-Cookie header
        # (a duplicate the APIC accepts for ordinary auth but that silently
        # breaks its internal subscription<->WebSocket linkage).
        self._cookie_domain = urlsplit(self._host).hostname or ""
        self._refresh_threshold = timedelta(seconds=refresh_threshold)
        self._retry = retry
        self._token_state: TokenState | None = None
        self._token_cache = token_cache
        # Certificate auth signs every request instead of trading a password
        # for a token, so both halves are required together or neither.
        if (private_key is None) != (cert_dn is None):
            raise ValueError("private_key and cert_dn must be given together")
        self._cert_dn = cert_dn
        self._signing_key = load_private_key(private_key) if private_key is not None else None
        self._apic_version: str | None = None
        self._token_lock = threading.Lock()
        self._auth = (
            CertificateAuth(self._signing_key, cert_dn)
            if self._signing_key is not None and cert_dn is not None
            else None
        )
        self._client = (
            client
            if client is not None
            else (
                httpx.Client(
                    base_url=self._host,
                    # httpx 0.28 deprecates verify=<str>; build the SSL context here.
                    verify=(
                        ssl.create_default_context(cafile=verify_ssl)
                        if isinstance(verify_ssl, str)
                        else verify_ssl
                    ),
                    timeout=timeout,
                )
            )
        )
        if self._auth is not None:
            # Set here rather than at construction so an injected client is
            # signed too. Dropping the auth for a caller who supplied their own
            # client would authenticate silently as nobody.
            self._client.auth = self._auth

        # Reused for the subscription WebSocket (wss://), which needs a real
        # ssl.SSLContext rather than httpx's bool-or-path verify shorthand.
        self._ws_ssl_context = self._build_ws_ssl_context(verify_ssl)
        self._subscription_socket: SubscriptionSocket | None = None

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> ApicSession:
        """
        Authenticate the session and return ``self``.

        Returns:
            The authenticated session, ready for requests.

        Raises:
            LoginError: If credentials are rejected.
            ConnectionError: If the APIC host is unreachable.
        """
        self.login()
        return self

    def __exit__(self, *_: object) -> None:
        """Close the underlying HTTP client."""
        self.close()

    def close(self) -> None:
        """
        Close the httpx client and release network resources.

        Call explicitly when not using the session as a context manager.
        After ``close()``, any request will raise an httpx error. Also tears
        down the subscription WebSocket, if one was ever opened — every
        blocked subscription iterator wakes with a plain ``StopIteration``.
        """
        if self._subscription_socket is not None:
            self._subscription_socket.close()
            self._subscription_socket = None
        self._client.close()

    # ── Public state ──────────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        """``True`` after :meth:`close` has been called.

        Returns:
            Whether the underlying httpx client has been closed.
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
    def apic_version(self) -> str | None:
        """The controller's own firmware version, as it stated it at login.

        The APIC returns this in the login envelope (``"6.0(9c)"``), and again
        on every token refresh, so it stays current for a long-lived session.

        This is what answers "will this work against my fabric?".  Compare it
        against :func:`niwaki.catalog.schema_version` — the firmware the SDK's
        models, vocabulary and filter grammar were generated from.  A different
        version is not a failure: reads stay tolerant of classes the SDK does
        not know, and writes fail loudly rather than silently.  It is a reason
        to pilot rather than assume.

        Returns:
            The version string, or ``None`` before the first successful login,
            or when a controller answered without naming one.

        Example::

            if aci.apic_version != catalog.schema_version():
                log.warning("fabric %s, SDK built for %s",
                            aci.apic_version, catalog.schema_version())
        """
        return self._apic_version

    @classmethod
    def with_client(
        cls,
        client: httpx.Client,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> ApicSession:
        """Build a session that talks through a client you configured yourself.

        Everything the SDK does not model — an outbound proxy, mutual TLS, a
        pinned CA bundle, a custom transport, a timeout policy per route — is
        already expressible in ``httpx``.  Rather than mirror each of those as
        a parameter here, this hands the whole client over.

        The session owns the client once given: closing the session closes it.
        Construction is otherwise identical, which is the point — every
        validation, every attribute, every default is the same as the ordinary
        constructor, and a test compares the two shapes so they cannot drift.

        Args:
            client: A configured ``httpx.Client``.  Its ``base_url`` is used
                as-is; *host* still names the APIC for cookies and WebSocket
                URLs, so pass the same host you gave the client.
            host: APIC base URL, or ``None`` to read ``APIC_HOST``.
            username: Login name, or ``None`` to read ``APIC_USERNAME``.
            password: Password, or ``None`` to read ``APIC_PASSWORD``.
            **kwargs: Any other constructor argument (``max_concurrent``,
                ``retry``, ``refresh_threshold``, …), validated as usual.

        Returns:
            A session using *client* for every request.

        Raises:
            ValueError: Whatever the ordinary constructor would raise.

        Example::

            proxied = httpx.Client(base_url=host, proxy="http://proxy:3128")
            session = ApicSession.with_client(proxied, host, user, password)
        """
        return cls(host, username, password, client=client, **kwargs)

    @property
    def retry(self) -> RetryConfig:
        """Active retry policy for this session.

        Returns:
            The :class:`~niwaki.transport.RetryConfig` in use.
        """
        return self._retry

    # ── Public auth ───────────────────────────────────────────────────────────

    def _forget_cached_token(self) -> None:
        """Drop the cached entry before re-authenticating.

        Whatever made the controller reject this token — a revocation, an
        eviction, a restart — makes the cached copy worthless too.  Leaving it
        would hand the same dead token to the next process, and the one after.
        """
        if self._token_cache is not None:
            self._token_cache.clear(self._host, self._username)

    def login(self, *, use_cache: bool = True) -> None:
        """
        Authenticate the session against the APIC via ``/api/aaaLogin.json``.

        Submits credentials, stores the returned token with its TTL, and sets
        the ``APIC-cookie`` cookie on the underlying HTTP client. Subsequent
        requests are automatically authenticated.

        Raises:
            LoginError: The APIC rejected the credentials or the response is malformed.
            ConnectionError: The APIC host is unreachable.
            TimeoutError: The login request exceeded the configured timeout.
            TLSError: TLS verification failed (invalid certificate, etc.).

        Example::

            session = ApicSession("https://apic.example.com", "admin", "secret")
            session.login()
            # session._token_state now holds the token and its expiry
        """
        # A cached token turns a per-command CLI login into one login per
        # session lifetime: same audit trail entry, one round trip instead of
        # twenty. Absent, unreadable or near expiry, the cache simply says no.
        # A signed session has no token to obtain: every request carries its
        # own proof, so there is nothing to log in for and nothing to expire.
        if self._signing_key is not None:
            return

        # Re-authentication must never be served from the cache: login() is the
        # recovery primitive, so a cached entry would answer a 401 with the very
        # token the controller just rejected — for this process and every later
        # one, since nothing would ever refill it.
        if use_cache and self._token_cache is not None:
            cached = self._token_cache.read(self._host, self._username)
            if cached is not None:
                token, expires_at = cached
                self._token_state = TokenState(
                    token=token,
                    expires_at=expires_at,
                    refresh_threshold=self._refresh_threshold,
                )
                self._client.cookies.set(
                    "APIC-cookie", self._token_state.token, domain=self._cookie_domain
                )
                return

        payload: dict[str, Any] = {
            "aaaUser": {"attributes": {"name": self._username, "pwd": self._password}}
        }

        with self._http_transport():
            resp = self._client.post(self._LOGIN_PATH, json=payload)

        if resp.status_code != 200:
            raise exceptions.LoginError(
                f"Login rejected by APIC (HTTP {resp.status_code}): {extract_apic_error(resp)}"
            )

        self._token_state = self._parse_token_response(resp, threshold=self._refresh_threshold)
        self._capture_apic_version(resp)
        if self._token_cache is not None:
            self._token_cache.write(
                self._host,
                self._username,
                self._token_state.token,
                self._token_state.expires_at,
            )
        self._client.cookies.set("APIC-cookie", self._token_state.token, domain=self._cookie_domain)

    # ── Internal auth ─────────────────────────────────────────────────────────

    def _refresh_token(self) -> None:
        """
        Refresh the session token via ``/api/aaaRefresh.json``.

        Extends the current session without resubmitting credentials.
        Updates ``_token_state`` and the ``APIC-cookie`` cookie.

        Raises:
            AuthError: No active token (``login()`` has not been called yet).
            TokenRefreshError: The APIC rejected the refresh request.
            ConnectionError: The APIC host is unreachable.
            TimeoutError: The request exceeded the configured timeout.
        """
        if self._token_state is None:
            raise exceptions.AuthError("Cannot refresh: no active session. Call login() first.")

        with self._http_transport():
            resp = self._client.get(self._REFRESH_PATH)

        if resp.status_code != 200:
            raise exceptions.TokenRefreshError(
                f"Token refresh failed (HTTP {resp.status_code}): {extract_apic_error(resp)}"
            )

        self._token_state = self._parse_token_response(resp, threshold=self._refresh_threshold)
        self._capture_apic_version(resp)
        if self._token_cache is not None:
            self._token_cache.write(
                self._host,
                self._username,
                self._token_state.token,
                self._token_state.expires_at,
            )
        self._client.cookies.set("APIC-cookie", self._token_state.token, domain=self._cookie_domain)

    def _ensure_token(self) -> None:
        """
        Ensure the token is valid before issuing a request.

        Protected by a :class:`threading.Lock` so that when multiple threads
        share a session, only the first one performs the refresh; subsequent
        waiters see the fresh token after acquiring the lock.

        Proactive refresh strategy:

        1. No token → ``AuthError`` (``login()`` required).
        2. Token expired → direct re-login.
        3. Token within refresh threshold → refresh, with re-login as fallback.
        4. Token OK → no action.

        Raises:
            AuthError: ``login()`` has not been called yet.
            SessionExpiredError: The token is expired and re-authentication
                failed (credentials revoked or APIC unreachable).
        """
        with self._token_lock:
            if self._signing_key is not None:
                # A signed session holds no token: each request carries its own
                # proof, so there is nothing to check, refresh or re-obtain.
                return

            if self._token_state is None:
                raise exceptions.AuthError(
                    "Not authenticated. Call login() or use the context manager."
                )

            if self._token_state.is_expired():
                self._relogin(reason="token expired")
                return

            if self._token_state.needs_refresh():
                try:
                    self._refresh_token()
                except exceptions.TokenRefreshError:
                    self._relogin(reason="refresh failed")

    def _relogin(self, reason: str) -> None:
        """
        Attempt a full re-login, wrapping ``LoginError`` into ``SessionExpiredError``.

        Args:
            reason: Reason for re-login (included in the error message).

        Raises:
            SessionExpiredError: If ``login()`` fails.
        """
        try:
            self._forget_cached_token()
            self.login(use_cache=False)
        except exceptions.LoginError as exc:
            raise exceptions.SessionExpiredError(
                f"Session cannot be renewed ({reason}): {exc}"
            ) from exc

    # ── Public GET ────────────────────────────────────────────────────────────

    def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """
        Execute a GET against the APIC REST API and return the ``imdata`` list.

        Ensures token validity before the request. Automatically retries on
        transient network errors (3 attempts, exponential backoff). Handles
        mid-session 401s by re-authenticating and replaying the request once.

        Args:
            path: API path relative to the base URL
                (e.g. ``"/api/mo/uni/tn-MyTenant.json"``).
            **params: Optional query string parameters
                (e.g. ``**{"query-target": "children", "rsp-subtree": "full"}``).

        Returns:
            The ``imdata`` list from the APIC JSON response. Empty list if the
            APIC returns an empty object (``totalCount: "0"``).

        Raises:
            AuthError: Not authenticated and automatic re-auth is not possible.
            SessionExpiredError: Token expired and re-auth failed.
            NotFoundError: HTTP 404 — the MO does not exist.
            UnauthorizedError: HTTP 401 persisting after re-authentication.
            ForbiddenError: HTTP 403 — insufficient privileges.
            ServerError: HTTP 5xx — APIC server-side error.
            ConnectionError: Host unreachable after all retry attempts.
            TimeoutError: Timeout exceeded after all retry attempts.
            TLSError: TLS verification error.

        Example::

            with ApicSession("https://apic.example.com", "admin", "pass") as s:
                tenants = s.get("/api/class/fvTenant.json")
                for item in tenants:
                    print(item["fvTenant"]["attributes"]["name"])
        """
        return self._get_imdata(path, dict(params))

    def get_mo(
        self,
        dn: str,
        cls: type[_T] = ManagedObject,  # type: ignore[assignment]
    ) -> _T:
        """Fetch a single MO by DN, typed as *cls*.

        Part of the transport boundary (:class:`niwaki.transport._protocols.MoReader`).

        Args:
            dn: Full Distinguished Name of the object.
            cls: Model class used to deserialise the response.

        Returns:
            The typed instance.

        Raises:
            NotFoundError: No object exists at *dn*.
        """
        raw = self._get_imdata(f"/api/mo/{dn}.json", {})
        objects = parse_imdata({"imdata": raw})
        if not objects:
            raise exceptions.NotFoundError(404, f"MO not found at DN: {dn!r}")
        return cast(_T, objects[0])

    # ── Public subscribe ──────────────────────────────────────────────────────

    def subscribe(
        self,
        path: str,
        params: dict[str, str],
        *,
        refresh_timeout: int | None = None,
        max_pending: int = _DEFAULT_MAX_PENDING,
    ) -> RawSubscription:
        """
        Subscribe to push notifications for a query, over the session's shared WebSocket.

        Part of the transport boundary
        (:class:`niwaki.transport._protocols.MoSubscriber`). The APIC
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
            max_pending: Bound on buffered, not-yet-consumed events for this
                subscription. Past it, incoming events are dropped (other
                subscriptions are never affected) and the stream receives one
                ``SubscriptionOverflow`` marker per overload episode —
                reconcile with a fresh read, exactly like a gap.

        Returns:
            A :class:`~niwaki.transport._subscription_socket.RawSubscription`
            — ``.initial`` for the synchronous snapshot, then iterate for
            live push items.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            SubscribeRejectedError: The APIC rejected the subscribe request.

        Example::

            sub = session.subscribe("/api/class/fvBD.json", {})
            for item in sub:
                print(item)
        """
        self._ensure_token()
        if self._subscription_socket is None:
            self._subscription_socket = SubscriptionSocket(self)
        return self._subscription_socket.subscribe(
            path, params, refresh_timeout=refresh_timeout, max_pending=max_pending
        )

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List every subscription currently tracked on this session's socket.

        Returns an empty list if no subscription was ever opened — this never
        opens the WebSocket itself.

        Returns:
            One :class:`~niwaki.transport._subscription_socket.SubscriptionInfo`
            per tracked subscription.
        """
        if self._subscription_socket is None:
            return []
        return self._subscription_socket.list_subscriptions()

    def refresh_all_subscriptions(self) -> list[SubscriptionInfo]:
        """Force an immediate refresh of every tracked subscription, on demand.

        A no-op returning an empty list if no subscription was ever opened.
        See :class:`~niwaki.transport._subscription_socket.SubscriptionSocket`'s
        method of the same name for the escalation-safety semantics.

        Returns:
            The post-refresh snapshot of every subscription.
        """
        if self._subscription_socket is None:
            return []
        return self._subscription_socket.refresh_all_subscriptions()

    def close_all_subscriptions(self) -> None:
        """Stop every tracked subscription — the shared socket itself stays open.

        A no-op if no subscription was ever opened. Distinct from
        :meth:`close`, which tears down the whole socket; see
        :meth:`~niwaki.transport._subscription_socket.SubscriptionSocket.close_all_subscriptions`.
        """
        if self._subscription_socket is not None:
            self._subscription_socket.close_all_subscriptions()

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _request_checked(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """Execute an authenticated GET with retry and mid-session 401 handling.

        Shared core used by :meth:`_get_imdata` and :meth:`_get_all_pages`.
        Raises a typed exception for any non-2xx response before returning.

        Args:
            path: API path relative to base URL.
            params: Query string parameters dict.

        Returns:
            Validated ``httpx.Response`` (non-2xx raises immediately).

        Raises:
            See :meth:`get`.
        """
        self._ensure_token()

        with self._http_transport():
            resp = self._request_with_retry("GET", path, params=params)

        # Mid-session 401: token revoked server-side while our local state
        # considered it valid. Re-authenticate and replay exactly once.
        if resp.status_code == 401:
            self._forget_cached_token()
            self.login(use_cache=False)
            with self._http_transport():
                resp = self._client.get(path, params=params)

        raise_for_apic_status(resp)
        return resp

    def _get_imdata(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a GET and return the raw ``imdata`` list (single page).

        Handles token validity, retry, mid-session 401 re-auth, and HTTP error
        raising.  All public GET methods delegate here.

        Args:
            path: API path relative to base URL.
            params: Query string parameters dict.

        Returns:
            The ``imdata`` list from the APIC JSON response.

        Raises:
            See :meth:`get`.
        """
        return self._request_checked(path, params).json().get("imdata", [])  # type: ignore[no-any-return]

    def _iter_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield one page of raw APIC ``imdata`` items at a time.

        Fetches page 0 first, then subsequent pages until ``totalCount`` is
        satisfied.  Each ``yield`` hands back one page so that the caller can
        process objects incrementally without holding all results in memory.

        Callers must not include ``"page"`` in *params* — use
        :meth:`_get_all_pages` when manual pagination is needed.

        Args:
            path: API path relative to base URL.
            params: Base query string parameters.  Must not contain ``"page"``.
            page_size: Objects per page.  Default: 500.

        Yields:
            One page (``list[dict]``) per APIC response, in order.

        Raises:
            ServerError: Pagination guard exceeded
                (:data:`_MAX_PAGINATION_PAGES`).
            See :meth:`get` for transport / auth errors.
        """
        page_params = {**params, "page": "0", "page-size": str(page_size)}
        data: dict[str, Any] = self._request_checked(path, page_params).json()
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
                self._request_checked(path, page_params).json().get("imdata", [])
            )
            if not batch:
                break
            yield batch
            fetched += len(batch)
            page += 1

    def _get_all_pages(
        self, path: str, params: dict[str, Any], *, page_size: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch all pages of results, auto-paginating based on ``totalCount``.

        When ``page`` is already present in ``params``, the caller is managing
        pagination manually; this method delegates to :meth:`_get_imdata` and
        returns a single page unchanged.  Otherwise delegates to
        :meth:`_iter_pages` and flattens results into one list.

        Args:
            path: API path relative to base URL.
            params: Base query string parameters dict.  Must not contain
                ``"page"`` when auto-pagination is desired.
            page_size: Objects per page for auto-paginated requests.
                Default: 500.

        Returns:
            Complete flattened list of all ``imdata`` items across all pages.

        Raises:
            See :meth:`get`.

        Example::

            # Auto-paginate — transparently fetches all pages
            raw = session._get_all_pages("/api/class/fvBD.json", {})

            # Manual control — single page, no auto-pagination
            raw = session._get_all_pages(
                "/api/class/fvBD.json",
                query.page(0, 100),
            )
        """
        if "page" in params:
            return self._get_imdata(path, params)
        return [
            item for page in self._iter_pages(path, params, page_size=page_size) for item in page
        ]

    # ── Public write ─────────────────────────────────────────────────────────

    def post_mo(self, dn: str, payload: dict[str, Any]) -> None:
        """
        POST an APIC envelope to a Managed Object URL.

        Used for both create and update operations.  APIC applies upsert
        semantics — the object is created if absent or updated if it exists.
        Only the fields present in the payload are modified; unspecified fields
        retain their current values.

        Args:
            dn: Full Distinguished Name of the target object
                (e.g. ``"uni/tn-prod/BD-web"``).
            payload: APIC envelope dict as produced by
                :meth:`~niwaki.models.ManagedObject.to_apic`.

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            ForbiddenError: HTTP 403 — insufficient privileges.
            NotFoundError: HTTP 404 — invalid DN structure.
            ServerError: HTTP 5xx — APIC server-side error.
            ConnectionError: Network error after all retry attempts.
            TimeoutError: Timeout exceeded.
            TLSError: TLS verification error.

        Example::

            session.post_mo("uni/tn-prod/BD-web", bd.to_apic())
        """
        self._raw_write("POST", f"/api/mo/{dn}.json", json=payload)

    def delete_mo(self, dn: str) -> None:
        """
        DELETE a Managed Object by Distinguished Name.

        Permanently removes the object and all its children from the APIC.
        This operation is irreversible.

        Args:
            dn: Full Distinguished Name of the object to delete
                (e.g. ``"uni/tn-prod/BD-web"``).

        Raises:
            AuthError: Not authenticated.
            SessionExpiredError: Token expired and re-auth failed.
            NotFoundError: HTTP 404 — the object does not exist.
            ForbiddenError: HTTP 403 — insufficient privileges.
            ServerError: HTTP 5xx — APIC server-side error.
            ConnectionError: Network error after all retry attempts.
            TimeoutError: Timeout exceeded.
            TLSError: TLS verification error.

        Example::

            session.delete_mo("uni/tn-prod/BD-web")
        """
        self._raw_write("DELETE", f"/api/mo/{dn}.json")

    def _raw_write(self, method: str, path: str, **kwargs: Any) -> None:
        """
        Execute a mutating request (POST or DELETE) with auth and retry.

        Mirrors the auth/retry logic of :meth:`_get_imdata` for write operations.
        Mid-session 401s trigger a single re-login and replay.

        Args:
            method: HTTP method (``"POST"`` or ``"DELETE"``).
            path: API path relative to base URL.
            **kwargs: Forwarded to ``httpx.Client.request``
                (e.g. ``json=payload``).

        Raises:
            See :meth:`post_mo`.
        """
        self._ensure_token()

        with self._http_transport():
            resp = self._request_with_retry(
                method,
                path,
                retry_on=_WRITE_SAFE_RETRY,
                retry_statuses=WRITE_RETRY_STATUSES,
                **kwargs,
            )

        if resp.status_code == 401:
            self._forget_cached_token()
            self.login(use_cache=False)
            with self._http_transport():
                resp = self._client.request(method, path, **kwargs)

        raise_for_apic_status(resp)

    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        retry_on: type[Exception] | tuple[type[Exception], ...] = httpx.TransportError,
        retry_statuses: frozenset[int] = READ_RETRY_STATUSES,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Execute a request with stamina retry on transient network errors.

        Shared by reads and writes.  Only ``httpx.TransportError`` triggers a
        retry (attempts/backoff per the session :class:`RetryConfig`); HTTP
        errors (4xx, 5xx) are returned unchanged for the caller to handle.

        Args:
            method: HTTP method string.
            path: Relative API path.
            **kwargs: Forwarded to ``httpx.Client.request``
                (e.g. ``params=...``, ``json=...``).

        Returns:
            Raw httpx response (HTTP errors not checked here).
            Network errors that persist after all attempts propagate as
            ``httpx.TransportError``, caught by ``_http_transport``.
        """
        last: httpx.Response | None = None
        try:
            for attempt in stamina.retry_context(
                on=(retry_on, RetryableStatus)
                if isinstance(retry_on, type)
                else (*retry_on, RetryableStatus),
                attempts=self._retry.attempts,
                wait_initial=self._retry.wait_initial,
                wait_max=self._retry.wait_max,
                wait_jitter=self._retry.wait_jitter,
            ):
                with attempt:
                    resp = self._client.request(method, path, **kwargs)
                    if resp.status_code not in retry_statuses:
                        return resp
                    last = resp
                    delay = retry_after_seconds(
                        resp, ceiling=self._retry.retry_after_max, now=time.time()
                    )
                    if delay is not None:
                        # The controller named a delay; honour it before stamina
                        # adds its own backoff. Clamped, so one busy answer cannot
                        # park a push for as long as the controller fancies.
                        time.sleep(delay)
                    raise RetryableStatus(resp, delay)
        except RetryableStatus:
            # Stamina re-raises on the final attempt. Hand the response
            # back rather than an internal signal, so the caller's own
            # error mapping runs and the user sees "HTTP 503: ...".
            assert last is not None
            return last

        raise RuntimeError("unreachable")  # pragma: no cover

    @contextmanager  # pyright: ignore[reportDeprecated]
    def _http_transport(self) -> Iterator[None]:
        """
        Context manager that converts httpx errors into typed niwaki exceptions.

        Used as a wrapper around all httpx operations to ensure that only
        ``NiwakiError`` subclasses propagate to the caller.

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

        Mirrors the ``verify_ssl`` semantics already applied to the httpx
        client, translated to what ``websockets.sync.client.connect`` expects
        (a real context, not httpx's bool-or-path shorthand).

        Args:
            verify_ssl: Same argument as :class:`ApicSession`'s constructor.

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

    def _capture_apic_version(self, resp: httpx.Response) -> None:
        """Record the firmware the controller stated in a login or refresh reply.

        The APIC names its version in the same envelope as the token, and a
        refresh repeats it, so a long-lived session stays current for free.
        Absent from the reply, the previous value is kept rather than cleared:
        one silent answer must not erase what the fabric already told us.
        """
        if (
            version := _imdata_attributes(resp, "aaaLogin", "aaaRefresh").get("version")
        ) is not None:
            self._apic_version = str(version)

    @staticmethod
    def _parse_token_response(resp: httpx.Response, *, threshold: timedelta) -> TokenState:
        """
        Extract the token and TTL from an APIC login or refresh response.

        The APIC uses ``aaaLogin`` for login responses and ``aaaRefresh``
        for refresh responses. Both share the same attribute structure.

        Args:
            resp: Raw httpx response from ``/api/aaaLogin.json`` or
                ``/api/aaaRefresh.json``.
            threshold: Refresh threshold to pass to the constructed ``TokenState``.

        Returns:
            A freshly constructed ``TokenState`` with the computed expiry.

        Raises:
            LoginError: If the response structure is unexpected or malformed.
        """
        # login answers inside aaaLogin, refresh inside aaaRefresh — and the
        # controller is free to answer a refresh with either envelope.
        attrs = _imdata_attributes(resp, "aaaLogin", "aaaRefresh")
        try:
            token: str = attrs["token"]
            ttl: int = int(attrs["refreshTimeoutSeconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise exceptions.LoginError(f"Unexpected APIC response structure: {exc}") from exc

        return TokenState.from_apic_response(
            token=token,
            refresh_timeout_seconds=ttl,
            refresh_threshold=threshold,
        )
