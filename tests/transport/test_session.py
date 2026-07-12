"""
Unit tests for ``niwaki.transport.session.ApicSession``.

All HTTP requests are mocked via ``pytest-httpx``.
Fixture payloads are loaded from ``tests/fixtures/`` — they reflect the
complete attribute set returned by a real APIC (v6.0(9c)).
stamina is in testing mode (delays disabled) via ``tests/conftest.py``.

Covers:
- login(): success, invalid credentials, network error, timeout, malformed response.
- _refresh_token(): success, failure → re-login fallback.
- _ensure_token(): no token, token needing refresh, expired token.
- get(): success, 404, 403, 500, mid-session 401 with re-auth, network retry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.transport._token import TokenState
from niwaki.transport.session import ApicSession
from tests.conftest import HOST, LOGIN_URL, load_fixture, login_payload

# ── Constants ─────────────────────────────────────────────────────────────────

USERNAME = "admin"
PASSWORD = "secret"  # pragma: allowlist secret — documentation placeholder

REFRESH_URL = f"{HOST}/api/aaaRefresh.json"
GET_URL = f"{HOST}/api/class/fvTenant.json"


# ── Payload helpers ───────────────────────────────────────────────────────────


def _refresh_payload(token: str = "FIXTURE_TOKEN_REFRESH", ttl: int = 600) -> dict[str, Any]:
    """
    Return a realistic aaaRefresh response based on the fixture file.

    Overrides ``token`` and ``refreshTimeoutSeconds``.
    """
    data = load_fixture("auth_refresh")
    attrs: dict[str, Any] = data["imdata"][0]["aaaRefresh"]["attributes"]
    attrs["token"] = token
    attrs["refreshTimeoutSeconds"] = str(ttl)
    return data


def _error_payload(code: str = "401", text: str = "Unauthorized") -> dict[str, Any]:
    """Return an APIC error payload with the given code and text."""
    return {"imdata": [{"error": {"attributes": {"code": code, "text": text}}}]}


def _tenants_payload() -> dict[str, Any]:
    """
    Return a realistic fvTenant list response based on the fixture file.

    Contains the standard system tenants (common, mgmt) plus a Prod tenant,
    with the full attribute set as returned by a real APIC.
    """
    return load_fixture("fvTenant_list")


# ── Session fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def session() -> ApicSession:
    """Unauthenticated session pointing at the fictional HOST."""
    return ApicSession(host=HOST, username=USERNAME, password=PASSWORD)


@pytest.fixture
def logged_session(session: ApicSession, httpx_mock: HTTPXMock) -> ApicSession:
    """Already-authenticated session (login mocked with realistic APIC payload)."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    session.login()
    return session


# ── Tests: login() ────────────────────────────────────────────────────────────


class TestLogin:
    def test_success_stores_token(self, session: ApicSession, httpx_mock: HTTPXMock) -> None:
        """Successful login stores the token and sets the APIC cookie."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("my-token"))

        session.login()

        assert session.is_authenticated
        assert session._token_state.token == "my-token"  # type: ignore[reportPrivateUsage]
        assert session._client.cookies.get("APIC-cookie") == "my-token"  # type: ignore[reportPrivateUsage]

    def test_success_sets_expiry(self, session: ApicSession, httpx_mock: HTTPXMock) -> None:
        """After login, the token has a future expiry based on the APIC TTL."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload(ttl=600))

        before = datetime.now(tz=UTC)
        session.login()

        assert session.is_authenticated
        assert session._token_state.expires_at > before  # type: ignore[reportPrivateUsage]

    def test_invalid_credentials_raises_login_error(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """An APIC 401 raises LoginError with the APIC message."""
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json=_error_payload(
                text="Username or password is incorrect - FAILED local authentication"
            ),
        )

        with pytest.raises(
            exceptions.LoginError,
            match="Username or password is incorrect",
        ):
            session.login()

    def test_malformed_response_raises_login_error(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A 200 response with unexpected payload raises LoginError."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json={"wrong": "structure"})

        with pytest.raises(exceptions.LoginError):
            session.login()

    def test_network_error_raises_connection_error(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A network error during login raises ConnectionError."""
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))

        with pytest.raises(exceptions.ConnectionError):
            session.login()

    def test_timeout_raises_timeout_error(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A timeout during login raises TimeoutError."""
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

        with pytest.raises(exceptions.TimeoutError):
            session.login()


# ── Tests: _refresh_token() ───────────────────────────────────────────────────


class TestRefreshToken:
    def test_success_updates_token(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """Successful refresh updates the token and the cookie."""
        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, json=_refresh_payload("refreshed-tok")
        )

        logged_session._refresh_token()  # type: ignore[reportPrivateUsage]

        assert logged_session.is_authenticated
        assert logged_session._token_state.token == "refreshed-tok"  # type: ignore[reportPrivateUsage]
        assert logged_session._client.cookies.get("APIC-cookie") == "refreshed-tok"  # type: ignore[reportPrivateUsage]

    def test_failure_raises_token_refresh_error(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A non-200 refresh response raises TokenRefreshError."""
        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, status_code=401, json=_error_payload()
        )

        with pytest.raises(exceptions.TokenRefreshError):
            logged_session._refresh_token()  # type: ignore[reportPrivateUsage]

    def test_without_login_raises_auth_error(self, session: ApicSession) -> None:
        """Calling _refresh_token() without a prior login raises AuthError."""
        with pytest.raises(exceptions.AuthError):
            session._refresh_token()  # type: ignore[reportPrivateUsage]


# ── Tests: _ensure_token() ────────────────────────────────────────────────────


class TestEnsureToken:
    def test_no_token_raises_auth_error(self, session: ApicSession) -> None:
        """Without login, _ensure_token() raises AuthError."""
        with pytest.raises(exceptions.AuthError, match="Not authenticated"):
            session._ensure_token()  # type: ignore[reportPrivateUsage]

    def test_valid_token_does_nothing(self, logged_session: ApicSession) -> None:
        """A valid token far from expiry does not trigger a refresh."""
        assert logged_session.is_authenticated
        original_token = logged_session._token_state.token  # type: ignore[reportPrivateUsage]

        logged_session._ensure_token()  # type: ignore[reportPrivateUsage]

        assert logged_session._token_state.token == original_token  # type: ignore[reportPrivateUsage]

    def test_token_needing_refresh_triggers_refresh(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A token within the refresh threshold triggers _refresh_token()."""
        assert logged_session.is_authenticated
        # Force the token into the refresh window (expires in 30 s, threshold 60 s)
        logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
            token=logged_session._token_state.token,  # type: ignore[reportPrivateUsage]
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
            refresh_threshold=timedelta(seconds=60),
        )

        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, json=_refresh_payload("proactively-refreshed")
        )

        logged_session._ensure_token()  # type: ignore[reportPrivateUsage]

        assert logged_session._token_state.token == "proactively-refreshed"  # type: ignore[reportPrivateUsage]

    def test_refresh_failure_falls_back_to_relogin(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """If refresh fails, _ensure_token() attempts a re-login."""
        assert logged_session.is_authenticated
        logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
            token=logged_session._token_state.token,  # type: ignore[reportPrivateUsage]
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
            refresh_threshold=timedelta(seconds=60),
        )

        httpx_mock.add_response(
            method="GET", url=REFRESH_URL, status_code=401, json=_error_payload()
        )
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("relogged"))

        logged_session._ensure_token()  # type: ignore[reportPrivateUsage]

        assert logged_session._token_state.token == "relogged"  # type: ignore[reportPrivateUsage]

    def test_expired_token_triggers_relogin(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """An expired token triggers a direct re-login (bypassing refresh)."""
        assert logged_session.is_authenticated
        logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
            token="expired",
            expires_at=datetime.now(tz=UTC) - timedelta(seconds=10),
        )

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("fresh"))

        logged_session._ensure_token()  # type: ignore[reportPrivateUsage]

        assert logged_session._token_state.token == "fresh"  # type: ignore[reportPrivateUsage]

    def test_expired_token_relogin_failure_raises_session_expired(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """If re-login fails on an expired token, SessionExpiredError is raised."""
        assert logged_session.is_authenticated
        logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
            token="expired",
            expires_at=datetime.now(tz=UTC) - timedelta(seconds=10),
        )

        httpx_mock.add_response(
            method="POST", url=LOGIN_URL, status_code=401, json=_error_payload()
        )

        with pytest.raises(exceptions.SessionExpiredError):
            logged_session._ensure_token()  # type: ignore[reportPrivateUsage]


# ── Tests: get() ──────────────────────────────────────────────────────────────


class TestGet:
    def test_success_returns_imdata(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A successful GET returns the full imdata list from the fixture."""
        httpx_mock.add_response(method="GET", url=GET_URL, json=_tenants_payload())

        result = logged_session.get("/api/class/fvTenant.json")

        # Fixture contains 3 tenants: common, mgmt, Prod
        assert len(result) == 3
        names = {item["fvTenant"]["attributes"]["name"] for item in result}
        assert names == {"common", "mgmt", "Prod"}

    def test_success_returns_full_mo_attributes(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """Returned MO dicts contain the full attribute set from the APIC schema."""
        httpx_mock.add_response(method="GET", url=GET_URL, json=_tenants_payload())

        result = logged_session.get("/api/class/fvTenant.json")

        # Verify key fvTenant schema attributes are present (dn, rn, modTs, lcOwn, uid…)
        common = next(
            item["fvTenant"]["attributes"]
            for item in result
            if item["fvTenant"]["attributes"]["name"] == "common"
        )
        assert common["dn"] == "uni/tn-common"
        assert common["rn"] == "tn-common"
        assert "modTs" in common
        assert common["lcOwn"] == "local"
        assert common["userdom"] == "all"

    def test_not_found_raises_not_found_error(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """HTTP 404 raises NotFoundError."""
        httpx_mock.add_response(
            method="GET",
            url=GET_URL,
            status_code=404,
            json=load_fixture("error_404"),
        )

        with pytest.raises(exceptions.NotFoundError) as exc_info:
            logged_session.get("/api/class/fvTenant.json")

        assert exc_info.value.status_code == 404

    def test_forbidden_raises_forbidden_error(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """HTTP 403 raises ForbiddenError."""
        httpx_mock.add_response(
            method="GET",
            url=GET_URL,
            status_code=403,
            json=load_fixture("error_403"),
        )

        with pytest.raises(exceptions.ForbiddenError):
            logged_session.get("/api/class/fvTenant.json")

    def test_server_error_raises_server_error(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """HTTP 500 raises ServerError."""
        httpx_mock.add_response(
            method="GET",
            url=GET_URL,
            status_code=500,
            json=load_fixture("error_500"),
        )

        with pytest.raises(exceptions.ServerError):
            logged_session.get("/api/class/fvTenant.json")

    def test_401_triggers_reauth_then_retry(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A mid-session 401 triggers a re-login then replays the request."""
        # First request → 401
        httpx_mock.add_response(method="GET", url=GET_URL, status_code=401, json=_error_payload())
        # Re-login
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("new-tok"))
        # Retry → success
        httpx_mock.add_response(method="GET", url=GET_URL, json=_tenants_payload())

        result = logged_session.get("/api/class/fvTenant.json")

        assert len(result) == 3

    def test_query_params_are_forwarded(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """Query string parameters are correctly forwarded to the APIC."""
        httpx_mock.add_response(method="GET", json={"imdata": []})

        logged_session.get("/api/class/fvTenant.json", **{"query-target": "children"})

        # get_requests() returns [POST login, GET class] — filter to the GET
        get_requests = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert len(get_requests) == 1
        assert "query-target=children" in str(get_requests[0].url)

    def test_network_error_after_retries_raises_connection_error(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A persistent network error raises ConnectionError (stamina testing = 1 attempt)."""
        # stamina.set_testing(True) removes delays AND reduces to 1 attempt
        httpx_mock.add_exception(httpx.ConnectError("refused"))

        with pytest.raises(exceptions.ConnectionError):
            logged_session.get("/api/class/fvTenant.json")

    def test_not_authenticated_raises_auth_error(self, session: ApicSession) -> None:
        """Calling get() without prior login raises AuthError."""
        with pytest.raises(exceptions.AuthError):
            session.get("/api/class/fvTenant.json")

    def test_empty_response_returns_empty_list(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """An APIC response with empty imdata returns an empty list."""
        httpx_mock.add_response(method="GET", url=GET_URL, json={"totalCount": "0", "imdata": []})

        result = logged_session.get("/api/class/fvTenant.json")

        assert result == []


# ── Tests: context manager ────────────────────────────────────────────────────


class TestWrite:
    """post_mo / delete_mo — direct transport coverage (not via the facade)."""

    def test_post_mo_sends_payload(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST", url=f"{HOST}/api/mo/uni/tn-x.json", json={"imdata": []}
        )
        logged_session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {"name": "x"}}})
        posts = [r for r in httpx_mock.get_requests() if "tn-x" in str(r.url)]
        assert len(posts) == 1

    def test_write_401_triggers_reauth_then_retry(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A mid-session 401 on a write triggers re-login then a single replay."""
        url = f"{HOST}/api/mo/uni/tn-x.json"
        httpx_mock.add_response(method="POST", url=url, status_code=401, json=_error_payload())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("new-tok"))
        httpx_mock.add_response(method="POST", url=url, json={"imdata": []})

        logged_session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {"name": "x"}}})

        replays = [r for r in httpx_mock.get_requests() if "tn-x" in str(r.url)]
        assert len(replays) == 2

    def test_delete_mo(self, logged_session: ApicSession, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="DELETE", url=f"{HOST}/api/mo/uni/tn-x.json", json={"imdata": []}
        )
        logged_session.delete_mo("uni/tn-x")

    def test_get_mo_empty_imdata_raises_not_found(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A 200 response with empty imdata is a missing MO, not a silent None."""
        httpx_mock.add_response(
            method="GET", url=f"{HOST}/api/mo/uni/tn-x.json", json={"imdata": []}
        )
        with pytest.raises(exceptions.NotFoundError, match="MO not found"):
            logged_session.get_mo("uni/tn-x")


class TestContextManager:
    def test_enter_calls_login(self, httpx_mock: HTTPXMock) -> None:
        """__enter__ calls login() automatically."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())

        with ApicSession(host=HOST, username=USERNAME, password=PASSWORD) as s:
            assert s.is_authenticated

    def test_exit_closes_client(self, httpx_mock: HTTPXMock) -> None:
        """__exit__ closes the httpx client."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())

        with ApicSession(host=HOST, username=USERNAME, password=PASSWORD) as s:
            pass

        assert s.is_closed


# ── Tests: _get_all_pages() pagination guard (P0.1) ──────────────────────────


class TestPaginationGuard:
    """Verify that _get_all_pages() raises ServerError when the page limit is hit."""

    def test_guard_raises_server_error_on_excessive_pages(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """When totalCount is never satisfied after _MAX_PAGINATION_PAGES pages, ServerError."""
        from unittest.mock import patch

        from niwaki.transport import session as session_module

        # Patch the constant to 2 so we can trigger it with 3 small pages
        with patch.object(session_module, "_MAX_PAGINATION_PAGES", 2):
            # page 0 is fetched before the loop; loop runs pages 1 and 2 (≤ 2),
            # then page 3 triggers the guard (no fetch at that point).
            for _ in range(3):  # pages 0, 1, 2
                httpx_mock.add_response(
                    method="GET",
                    json={
                        "totalCount": "999",
                        "imdata": [{"fvBD": {"attributes": {"name": "bd"}}}],
                    },
                )

            with pytest.raises(exceptions.ServerError, match="Pagination guard"):
                logged_session._get_all_pages("/api/class/fvBD.json", {})  # type: ignore[reportPrivateUsage]

    def test_normal_pagination_unaffected(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """A query that completes within the page limit should not raise."""
        # Page 0: totalCount=2, return 1 item
        httpx_mock.add_response(
            method="GET",
            json={
                "totalCount": "2",
                "imdata": [{"fvBD": {"attributes": {"name": "bd0"}}}],
            },
        )
        # Page 1: return remaining 1 item
        httpx_mock.add_response(
            method="GET",
            json={
                "totalCount": "2",
                "imdata": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            },
        )
        result = logged_session._get_all_pages("/api/class/fvBD.json", {}, page_size=1)  # type: ignore[reportPrivateUsage]
        assert len(result) == 2


class TestEnsureTokenLock:
    """Verify the threading.Lock exists and is used by _ensure_token()."""

    def test_token_lock_exists(self, session: ApicSession) -> None:
        import threading

        lock_type = type(threading.Lock())
        assert isinstance(session._token_lock, lock_type)  # type: ignore[reportPrivateUsage]

    def test_concurrent_refresh_triggered_once(
        self, logged_session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        """When multiple threads enter _ensure_token() simultaneously with a
        near-expiry token, only one refresh should be performed."""
        import threading
        from datetime import UTC, datetime, timedelta

        from niwaki.transport._token import TokenState

        # Set token to need refresh
        logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
            token="old",
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            refresh_threshold=timedelta(seconds=60),
        )

        refresh_count = 0

        def _counting_refresh() -> None:
            nonlocal refresh_count
            refresh_count += 1
            # Simulate a fresh token state
            logged_session._token_state = TokenState(  # type: ignore[reportPrivateUsage]
                token="new",
                expires_at=datetime.now(UTC) + timedelta(seconds=600),
                refresh_threshold=timedelta(seconds=60),
            )

        logged_session._refresh_token = _counting_refresh  # type: ignore[method-assign]

        barrier = threading.Barrier(5)

        def _worker() -> None:
            barrier.wait()  # all threads start at the same time
            logged_session._ensure_token()  # type: ignore[reportPrivateUsage]

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Lock ensures refresh is called exactly once even with 5 concurrent threads
        assert refresh_count == 1
