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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, TypeVar, cast

import httpx
import stamina

from niwaki import exceptions
from niwaki.models.base import ManagedObject
from niwaki.transport._config import RetryConfig
from niwaki.transport._errors import extract_apic_error, raise_for_apic_status
from niwaki.transport._token import TokenState
from niwaki.utils.response import parse_imdata

_T = TypeVar("_T", bound=ManagedObject)

# Safety limit: 2000 pages x 500 objects = 1 000 000 objects per query.
# Exceeds any real ACI fabric; prevents runaway loops on corrupted totalCount.
_MAX_PAGINATION_PAGES: int = 2000
_DEFAULT_RETRY: RetryConfig = RetryConfig()


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
    ) -> None:
        self._host = (host or os.environ["APIC_HOST"]).rstrip("/")
        self._username = username or os.environ["APIC_USERNAME"]
        self._password = password or os.environ["APIC_PASSWORD"]
        self._refresh_threshold = timedelta(seconds=refresh_threshold)
        self._retry = retry
        self._token_state: TokenState | None = None
        self._token_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=self._host,
            # httpx 0.28 deprecates verify=<str>; build the SSL context here.
            verify=(
                ssl.create_default_context(cafile=verify_ssl)
                if isinstance(verify_ssl, str)
                else verify_ssl
            ),
            timeout=timeout,
        )

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
        After ``close()``, any request will raise an httpx error.
        """
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
    def retry(self) -> RetryConfig:
        """Active retry policy for this session.

        Returns:
            The :class:`~niwaki.RetryConfig` in use.
        """
        return self._retry

    # ── Public auth ───────────────────────────────────────────────────────────

    def login(self) -> None:
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
        self._client.cookies.set("APIC-cookie", self._token_state.token)

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
        self._client.cookies.set("APIC-cookie", self._token_state.token)

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
            self.login()
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
            self.login()
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
        total = int(data.get("totalCount", 0))
        first: list[dict[str, Any]] = list(data.get("imdata", []))
        if not first:
            return
        yield first

        fetched = len(first)
        page = 1
        while fetched < total:
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
                :meth:`~niwaki.models.base.ManagedObject.to_apic`.

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
            resp = self._request_with_retry(method, path, **kwargs)

        if resp.status_code == 401:
            self.login()
            with self._http_transport():
                resp = self._client.request(method, path, **kwargs)

        raise_for_apic_status(resp)

    def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
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
        for attempt in stamina.retry_context(
            on=httpx.TransportError,
            attempts=self._retry.attempts,
            wait_initial=self._retry.wait_initial,
            wait_max=self._retry.wait_max,
            wait_jitter=self._retry.wait_jitter,
        ):
            with attempt:
                return self._client.request(method, path, **kwargs)

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
        try:
            data: dict[str, Any] = resp.json()
            inner: dict[str, Any] = data["imdata"][0]
            # login → aaaLogin, refresh → aaaRefresh
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
