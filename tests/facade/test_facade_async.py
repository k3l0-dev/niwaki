"""Tests for niwaki.facade — AsyncNiwaki + AsyncNiwakiNode.

All HTTP mocked via pytest-httpx.  All tests are async (asyncio_mode=auto).
Covers: async context manager, navigation, create/read/update/delete/diff_update,
children, query, gather, apply (Op), op_* builders.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.facade import AsyncNiwaki, AsyncNiwakiNode
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models.base import ManagedObject
from tests.conftest import HOST, LOGIN_URL, load_fixture, login_payload, ok

# ── Helpers ───────────────────────────────────────────────────────────────────


# ── Async context manager ─────────────────────────────────────────────────────


class TestSessionGuard:
    async def test_unentered_client_raises_auth_error(self) -> None:
        """Using AsyncNiwaki without entering the context manager is a clear error."""
        aci = AsyncNiwaki(HOST, "admin", "secret")
        with pytest.raises(exceptions.AuthError, match="not initialised"):
            aci.query("fvTenant")


class TestAsyncContextManager:
    async def test_aenter_returns_async_niwaki(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert isinstance(aci, AsyncNiwaki)

    async def test_aexit_closes_session(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            session = aci._active_session  # type: ignore[reportPrivateUsage]
        assert session.is_closed

    async def test_login_failure_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "401", "text": "bad creds"}}}]},
        )
        with pytest.raises(exceptions.LoginError):
            async with AsyncNiwaki(HOST, "admin", "wrong"):
                pass


# ── DN computation ────────────────────────────────────────────────────────────


class TestDnComputation:
    async def test_root_dn(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.root.dn == "uni"

    async def test_one_level(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.root.mo(fvTenant, name="prod").dn == "uni/tn-prod"

    async def test_two_levels(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            dn = aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").dn
            assert dn == "uni/tn-prod/BD-web"

    async def test_node_returns_async_niwaki_node(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            node = aci.root.mo(fvBD, name="web")
            assert isinstance(node, AsyncNiwakiNode)

    async def test_node_explicit_dn(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            node = aci.node("uni/tn-prod/BD-web", fvBD)
            assert node.dn == "uni/tn-prod/BD-web"
            assert node.cls is fvBD

    async def test_node_default_cls(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.node("uni/tn-prod").cls is ManagedObject


# ── Read ──────────────────────────────────────────────────────────────────────


class TestRead:
    async def test_returns_typed_instance(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
            json=load_fixture("fvBD_list"),
        )

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            bd = await aci.root.mo(fvTenant, name="Prod").mo(fvBD, name="Prod-BD").read()
            assert isinstance(bd, fvBD)
            assert bd.name == "Prod-BD"

    async def test_not_found_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=404, json=load_fixture("error_404"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(exceptions.NotFoundError):
                await aci.root.mo(fvTenant, name="missing").read()


# ── Delete ────────────────────────────────────────────────────────────────────


class TestDelete:
    async def test_sends_delete(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="DELETE", json=ok())

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            await aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").delete()

        del_reqs = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
        assert del_reqs[0].url.path == "/api/mo/uni/tn-prod/BD-web.json"

    async def test_not_found_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="DELETE", status_code=404, json=load_fixture("error_404"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(exceptions.NotFoundError):
                await aci.root.mo(fvTenant, name="gone").delete()


# ── Children via Query ────────────────────────────────────────────────────────


class TestChildren:
    async def test_returns_typed_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            bds = await aci.root.mo(fvTenant, name="Prod").query(fvBD).children().fetch()
            assert all(isinstance(bd, fvBD) for bd in bds)

    async def test_query_params_sent(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            await aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()

        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url = str(get_reqs[0].url)
        assert "query-target=children" in url
        assert "target-subtree-class=fvBD" in url


# ── Query ─────────────────────────────────────────────────────────────────────


class TestQuery:
    async def test_returns_typed_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            tenants = await aci.query(fvTenant).fetch()
            assert all(isinstance(t, fvTenant) for t in tenants)
            assert len(tenants) == 3

    async def test_empty_result(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=ok())

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert await aci.query(fvBD).fetch() == []


# ── Gather ────────────────────────────────────────────────────────────────────


class TestGather:
    async def test_runs_concurrently_and_returns_tuple(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            tenants, bds = await aci.gather(
                aci.query(fvTenant).fetch(),
                aci.query(fvBD).fetch(),
            )
            assert len(tenants) == 3
            assert len(bds) >= 1

    async def test_error_propagates_as_exception_group(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))
        httpx_mock.add_response(method="GET", status_code=403, json=load_fixture("error_403"))

        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(ExceptionGroup) as exc_info:
                await aci.gather(
                    aci.query(fvTenant).fetch(),
                    aci.query(fvBD).fetch(),
                )
            assert any(isinstance(e, exceptions.ForbiddenError) for e in exc_info.value.exceptions)


# ── Parité sync/async diff_update + children (P2.9) ──────────────────────────


class TestChildrenParity:
    """Verify async children query behaves identically to the sync counterpart."""

    async def test_returns_typed_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            result = await aci.root.mo(fvTenant, name="Prod").query(fvBD).children().fetch()
        assert all(isinstance(bd, fvBD) for bd in result)

    async def test_query_target_children_param_sent(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            await aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url = str(get_reqs[0].url)
        assert "query-target=children" in url
        assert "target-subtree-class=fvBD" in url

    async def test_empty_response_returns_empty_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            result = await aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        assert result == []


# ── RetryConfig passthrough ───────────────────────────────────────────────────


class TestAsyncRetryConfig:
    async def test_retry_config_accepted(self, httpx_mock: HTTPXMock) -> None:
        from niwaki.transport import RetryConfig

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        rc = RetryConfig(attempts=2)
        async with AsyncNiwaki(HOST, "admin", "secret", retry=rc) as aci:
            assert aci.retry is rc

    async def test_retry_config_none_uses_default(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.retry is None
