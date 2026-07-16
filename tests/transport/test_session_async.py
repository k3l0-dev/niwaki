"""Tests for niwaki.transport.session_async — AsyncApicSession.

All HTTP mocked via pytest-httpx.  All tests are async (asyncio_mode=auto).
Covers: login, refresh, ensure_token lock, semaphore, GET, POST, DELETE,
retry on transient errors, 401 mid-session re-auth, and all HTTP error paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import stamina
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import load_fixture

# ── Constants ─────────────────────────────────────────────────────────────────

HOST = "https://apic.test"
LOGIN_URL = f"{HOST}/api/aaaLogin.json"
REFRESH_URL = f"{HOST}/api/aaaRefresh.json"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def real_retries():
    """Disable stamina testing mode so retry tests run all configured attempts."""
    stamina.set_testing(False)
    yield
    stamina.set_testing(True)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _login_resp(token: str = "tok", ttl: int = 600) -> dict[str, Any]:
    data = load_fixture("auth_login")
    data["imdata"][0]["aaaLogin"]["attributes"]["token"] = token
    data["imdata"][0]["aaaLogin"]["attributes"]["refreshTimeoutSeconds"] = str(ttl)
    return data


def _refresh_resp(token: str = "tok2") -> dict[str, Any]:
    data = load_fixture("auth_refresh")
    data["imdata"][0]["aaaRefresh"]["attributes"]["token"] = token
    data["imdata"][0]["aaaRefresh"]["attributes"]["refreshTimeoutSeconds"] = "600"
    return data


def _ok() -> dict[str, Any]:
    return {"totalCount": "0", "imdata": []}


# ── Login ─────────────────────────────────────────────────────────────────────


class TestLogin:
    async def test_success_stores_token(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1"))
        async with AsyncApicSession(HOST, "admin", "pass") as s:
            assert s.is_authenticated
            assert s._token_state.token == "t1"  # type: ignore[reportPrivateUsage]

    async def test_sets_apic_cookie(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("cookie_tok"))
        async with AsyncApicSession(HOST, "admin", "pass") as s:
            assert s._client.cookies.get("APIC-cookie") == "cookie_tok"  # type: ignore[reportPrivateUsage]

    async def test_failure_raises_login_error(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "401", "text": "bad creds"}}}]},
        )
        with pytest.raises(exceptions.LoginError, match="bad creds"):
            async with AsyncApicSession(HOST, "admin", "wrong"):
                pass

    async def test_malformed_response_raises_login_error(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json={"imdata": []})
        with pytest.raises(exceptions.LoginError, match="Unexpected APIC response"):
            async with AsyncApicSession(HOST, "admin", "pass"):
                pass

    async def test_connection_error_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("unreachable"), url=LOGIN_URL)
        with pytest.raises(exceptions.ConnectionError):
            async with AsyncApicSession(HOST, "admin", "pass"):
                pass

    async def test_timeout_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.TimeoutException("timed out"), url=LOGIN_URL)
        with pytest.raises(exceptions.TimeoutError):
            async with AsyncApicSession(HOST, "admin", "pass"):
                pass


# ── Token refresh ─────────────────────────────────────────────────────────────


class TestTokenRefresh:
    async def test_proactive_refresh_on_needs_refresh(self, httpx_mock: HTTPXMock) -> None:
        # ttl=30 < refresh_threshold=60 → needs_refresh() True immediately
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1", ttl=30))
        httpx_mock.add_response(method="GET", url=REFRESH_URL, json=_refresh_resp("t2"))
        httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass", refresh_threshold=60) as s:
            await s.get("/api/class/fvTenant.json")
            assert s.is_authenticated
            assert s._token_state.token == "t2"  # type: ignore[reportPrivateUsage]

    async def test_refresh_failure_triggers_relogin(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1", ttl=30))
        httpx_mock.add_response(method="GET", url=REFRESH_URL, status_code=500, json=_ok())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t3"))
        httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass", refresh_threshold=60) as s:
            await s.get("/api/class/fvTenant.json")
            assert s.is_authenticated
            assert s._token_state.token == "t3"  # type: ignore[reportPrivateUsage]

    async def test_lock_prevents_concurrent_double_refresh(self, httpx_mock: HTTPXMock) -> None:
        """Five coroutines hitting _ensure_token simultaneously must trigger only one refresh."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1", ttl=30))
        httpx_mock.add_response(method="GET", url=REFRESH_URL, json=_refresh_resp("t2"))

        s = AsyncApicSession(HOST, "admin", "pass", refresh_threshold=60)
        await s.login()

        # All 5 coroutines see needs_refresh=True; only the first should refresh.
        await asyncio.gather(*[s._ensure_token() for _ in range(5)])  # type: ignore[reportPrivateUsage]

        refresh_calls = [r for r in httpx_mock.get_requests() if "aaaRefresh" in str(r.url)]
        assert len(refresh_calls) == 1

        await s.close()

    async def test_refresh_without_login_raises_auth_error(self) -> None:
        session = AsyncApicSession(host=HOST, username="admin", password="secret")
        with pytest.raises(exceptions.AuthError, match="Call login"):
            await session._refresh_token()  # type: ignore[reportPrivateUsage]
        await session.close()

    async def test_failed_relogin_raises_session_expired(self, httpx_mock: HTTPXMock) -> None:
        """Expired token + rejected re-login → SessionExpiredError (not LoginError)."""
        import datetime

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        session = AsyncApicSession(HOST, "admin", "pass")
        await session.login()
        object.__setattr__(
            session._token_state,  # type: ignore[reportPrivateUsage]
            "expires_at",
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=1),
        )
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "401", "text": "bad"}}}]},
        )
        with pytest.raises(exceptions.SessionExpiredError):
            await session.get("/api/class/fvTenant.json")
        await session.close()

    async def test_expired_token_triggers_relogin(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1", ttl=0))
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t2"))
        httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass", refresh_threshold=0) as s:
            # Force expiry by manipulating expires_at
            import datetime

            assert s.is_authenticated
            object.__setattr__(
                s._token_state,  # type: ignore[reportPrivateUsage]
                "expires_at",
                datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=1),
            )
            await s.get("/api/class/fvTenant.json")
            assert s._token_state.token == "t2"  # type: ignore[reportPrivateUsage]


# ── GET ───────────────────────────────────────────────────────────────────────


class TestGet:
    async def test_returns_imdata(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            result = await s.get("/api/class/fvTenant.json")
            assert len(result) == 3
            assert result[0]["fvTenant"]["attributes"]["name"] in {"common", "mgmt", "Prod"}

    async def test_empty_response(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            result = await s.get("/api/class/fvBD.json")
            assert result == []

    async def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", status_code=404, json=load_fixture("error_404"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.NotFoundError):
                await s.get("/api/mo/uni/tn-missing.json")

    async def test_403_raises_forbidden(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", status_code=403, json=load_fixture("error_403"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.ForbiddenError):
                await s.get("/api/class/fvTenant.json")

    async def test_500_raises_server_error(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", status_code=500, json=load_fixture("error_500"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.ServerError):
                await s.get("/api/class/fvTenant.json")

    async def test_mid_session_401_relogs_and_retries(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1"))
        httpx_mock.add_response(method="GET", status_code=401, json=_ok())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t2"))
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            result = await s.get("/api/class/fvTenant.json")
            assert len(result) == 3
            assert s.is_authenticated
            assert s._token_state.token == "t2"  # type: ignore[reportPrivateUsage]

    async def test_retry_on_transport_error(
        self, httpx_mock: HTTPXMock, real_retries: None
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_exception(httpx.RemoteProtocolError("connection reset"))
        httpx_mock.add_exception(httpx.RemoteProtocolError("connection reset"))
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            result = await s.get("/api/class/fvTenant.json")
            assert len(result) == 3


# ── get_mo (typed single-MO read) ────────────────────────────────────────────


class TestGetMo:
    async def test_returns_typed_instance(self, httpx_mock: HTTPXMock) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
            json=load_fixture("fvBD_list"),
        )

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            bd = await s.get_mo("uni/tn-Prod/BD-Prod-BD", cls=fvBD)
            assert isinstance(bd, fvBD)
            assert bd.name == "Prod-BD"

    async def test_not_found_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.NotFoundError):
                await s.get_mo("uni/tn-missing")


# ── _get_all_pages (internal) ─────────────────────────────────────────────────


class TestGetClass:
    async def test_get_all_pages_returns_items(self, httpx_mock: HTTPXMock) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.utils.response import parse_imdata

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            raw = await s._get_all_pages("/api/class/fvTenant.json", {})  # type: ignore[reportPrivateUsage]
            tenants = parse_imdata({"imdata": raw})
            assert all(isinstance(t, fvTenant) for t in tenants)
            assert len(tenants) == 3

    async def test_autopagination(self, httpx_mock: HTTPXMock) -> None:
        """_get_all_pages fetches all pages when totalCount exceeds page size."""
        page0 = {
            "totalCount": "4",
            "imdata": [
                {"fvTenant": {"attributes": {"dn": f"uni/tn-T{i}", "name": f"T{i}"}}}
                for i in range(2)
            ],
        }
        page1 = {
            "totalCount": "4",
            "imdata": [
                {"fvTenant": {"attributes": {"dn": f"uni/tn-T{i}", "name": f"T{i}"}}}
                for i in range(2, 4)
            ],
        }
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=page0)
        httpx_mock.add_response(method="GET", json=page1)

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            items = await s._get_all_pages("/api/class/fvTenant.json", {}, page_size=2)  # type: ignore[reportPrivateUsage]
            assert len(items) == 4


# ── post_mo / delete_mo ───────────────────────────────────────────────────────


class TestWrite:
    async def test_post_mo_sends_correct_url(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="POST", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            await s.post_mo("uni/tn-prod", {"fvTenant": {"attributes": {"name": "prod"}}})

        write_reqs = [r for r in httpx_mock.get_requests() if "/aaaLogin" not in str(r.url)]
        assert write_reqs[0].url.path == "/api/mo/uni/tn-prod.json"

    async def test_post_mo_forbidden_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="POST", status_code=403, json=load_fixture("error_403"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.ForbiddenError):
                await s.post_mo("uni/tn-prod", {"fvTenant": {"attributes": {"name": "prod"}}})

    async def test_delete_mo_sends_delete(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="DELETE", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            await s.delete_mo("uni/tn-prod")

        delete_reqs = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
        assert len(delete_reqs) == 1
        assert delete_reqs[0].url.path == "/api/mo/uni/tn-prod.json"

    async def test_delete_mo_not_found_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="DELETE", status_code=404, json=load_fixture("error_404"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.NotFoundError):
                await s.delete_mo("uni/tn-gone")

    async def test_write_retry_on_pre_send_error(
        self, httpx_mock: HTTPXMock, real_retries: None
    ) -> None:
        # Writes retry only on pre-send errors (ConnectError): the request
        # provably never reached the server, so re-sending is safe (audit T2).
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        httpx_mock.add_response(method="POST", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            await s.post_mo("uni/tn-prod", {})  # should succeed on third attempt

    async def test_write_not_retried_on_read_timeout(
        self, httpx_mock: HTTPXMock, real_retries: None
    ) -> None:
        # A read/write timeout may mean the APIC already accepted the write, so
        # it must NOT be retried — the error propagates (audit T2).
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.TimeoutError):
                await s.post_mo("uni/tn-prod", {})

    async def test_mid_session_401_relogs_and_replays_write(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t1"))
        httpx_mock.add_response(method="POST", status_code=401, json=_ok())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp("t2"))
        httpx_mock.add_response(method="POST", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            await s.post_mo("uni/tn-prod", {})
            assert s.is_authenticated
            assert s._token_state.token == "t2"  # type: ignore[reportPrivateUsage]


# ── Semaphore ─────────────────────────────────────────────────────────────────


class TestSemaphore:
    async def test_max_concurrent_accepted_and_requests_complete(
        self, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        for _ in range(3):
            httpx_mock.add_response(method="GET", json=_ok())

        async with AsyncApicSession(HOST, "admin", "pass", max_concurrent=2) as s:
            results = await asyncio.gather(
                s.get("/api/class/fvTenant.json"),
                s.get("/api/class/fvBD.json"),
                s.get("/api/class/fvCtx.json"),
            )
            assert all(r == [] for r in results)

    async def test_no_authenticated_session_raises(self) -> None:
        s = AsyncApicSession(HOST, "admin", "pass")
        with pytest.raises(exceptions.AuthError):
            await s._ensure_token()  # type: ignore[reportPrivateUsage]
        await s.close()


# ── TLS / connection errors ───────────────────────────────────────────────────


# ── Pagination guard (P0.1) ───────────────────────────────────────────────────


class TestPaginationGuard:
    """Verify _get_all_pages() raises ServerError when the page limit is hit."""

    async def test_guard_raises_server_error_on_excessive_pages(
        self, httpx_mock: HTTPXMock
    ) -> None:
        from unittest.mock import patch

        from niwaki.transport import session_async as session_async_module

        with patch.object(session_async_module, "_MAX_PAGINATION_PAGES", 2):
            httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
            # page 0 (pre-loop) + pages 1 and 2 (≤ 2 in loop), page 3 triggers guard
            for _ in range(3):
                httpx_mock.add_response(
                    method="GET",
                    json={"totalCount": "999", "imdata": [{"fvBD": {"attributes": {"name": "b"}}}]},
                )

            async with AsyncApicSession(HOST, "admin", "pass") as s:
                with pytest.raises(exceptions.ServerError, match="Pagination guard"):
                    await s._get_all_pages("/api/class/fvBD.json", {})  # type: ignore[reportPrivateUsage]

    async def test_normal_pagination_unaffected(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(
            method="GET",
            json={"totalCount": "2", "imdata": [{"fvBD": {"attributes": {"name": "b0"}}}]},
        )
        httpx_mock.add_response(
            method="GET",
            json={"totalCount": "2", "imdata": [{"fvBD": {"attributes": {"name": "b1"}}}]},
        )

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            result = await s._get_all_pages("/api/class/fvBD.json", {}, page_size=1)  # type: ignore[reportPrivateUsage]
        assert len(result) == 2


class TestAIterPages:
    """_aiter_pages() yields raw imdata lists one page at a time."""

    async def test_single_page_yields_all_items(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(
            method="GET",
            json={
                "totalCount": "2",
                "imdata": [
                    {"fvTenant": {"attributes": {"name": "prod"}}},
                    {"fvTenant": {"attributes": {"name": "dev"}}},
                ],
            },
        )

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            pages = [page async for page in s._aiter_pages("/api/class/fvTenant.json", {})]  # type: ignore[reportPrivateUsage]
        assert len(pages) == 1
        assert len(pages[0]) == 2

    async def test_multi_page_yields_all_pages(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(
            method="GET",
            json={"totalCount": "2", "imdata": [{"fvTenant": {"attributes": {"name": "a"}}}]},
        )
        httpx_mock.add_response(
            method="GET",
            json={"totalCount": "2", "imdata": [{"fvTenant": {"attributes": {"name": "b"}}}]},
        )

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            pages = [p async for p in s._aiter_pages("/api/class/fvTenant.json", {}, page_size=1)]  # type: ignore[reportPrivateUsage]
        assert len(pages) == 2
        assert sum(len(p) for p in pages) == 2

    async def test_empty_result_yields_nothing(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            pages = [p async for p in s._aiter_pages("/api/class/fvTenant.json", {})]  # type: ignore[reportPrivateUsage]
        assert pages == []

    async def test_get_all_pages_and_aiter_pages_return_same_items(
        self, httpx_mock: HTTPXMock
    ) -> None:
        payload = {
            "totalCount": "2",
            "imdata": [
                {"fvTenant": {"attributes": {"name": "x"}}},
                {"fvTenant": {"attributes": {"name": "y"}}},
            ],
        }
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        httpx_mock.add_response(method="GET", json=payload)
        httpx_mock.add_response(method="GET", json=payload)

        async with AsyncApicSession(HOST, "admin", "pass") as s1:
            via_list = await s1._get_all_pages("/api/class/fvTenant.json", {})  # type: ignore[reportPrivateUsage]
        async with AsyncApicSession(HOST, "admin", "pass") as s2:
            via_iter: list = []
            async for page in s2._aiter_pages("/api/class/fvTenant.json", {}):  # type: ignore[reportPrivateUsage]
                via_iter.extend(page)
        assert via_list == via_iter


class TestTransportErrors:
    async def test_tls_error_raises(self, httpx_mock: HTTPXMock) -> None:
        import ssl

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login_resp())
        exc = httpx.ConnectError("TLS failure")
        exc.__cause__ = ssl.SSLError("cert verify failed")
        httpx_mock.add_exception(exc)

        async with AsyncApicSession(HOST, "admin", "pass") as s:
            with pytest.raises(exceptions.TLSError):
                await s.get("/api/class/fvTenant.json")
