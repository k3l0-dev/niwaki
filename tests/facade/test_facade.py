"""Tests for niwaki.facade — Niwaki + NiwakiNode.

All HTTP mocked via pytest-httpx.
Covers: connect, context manager, root, node, mo DN chaining,
create/read/update/delete/diff_update/children, query.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.facade import Niwaki
from niwaki.models._generated.fv.fvBD import fvBD
from niwaki.models._generated.fv.fvCtx import fvCtx
from niwaki.models._generated.fv.fvTenant import fvTenant
from niwaki.models.base import ManagedObject
from tests.conftest import HOST, LOGIN_URL, load_fixture, login_payload, ok

# ── Helpers ───────────────────────────────────────────────────────────────────


# ── Niwaki.connect ────────────────────────────────────────────────────────────


class TestConnect:
    def test_login_called(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("tok"))
        aci = Niwaki.connect(HOST, "admin", "secret")
        assert aci._sync_session.is_authenticated  # type: ignore[reportPrivateUsage]
        aci.close()

    def test_login_failure_raises(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=LOGIN_URL,
            status_code=401,
            json={"imdata": [{"error": {"attributes": {"code": "401", "text": "bad creds"}}}]},
        )
        with pytest.raises(exceptions.LoginError):
            Niwaki.connect(HOST, "admin", "wrong")

    def test_retry_propagated_to_session(self, httpx_mock: HTTPXMock) -> None:
        """connect() honours a custom retry policy (same path as __enter__)."""
        from niwaki.transport import RetryConfig

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        aci = Niwaki.connect(HOST, "admin", "secret", retry=RetryConfig(attempts=5))
        assert aci.retry is not None
        assert aci.retry.attempts == 5
        assert aci._sync_session.retry.attempts == 5  # type: ignore[reportPrivateUsage]
        aci.close()


class TestClose:
    def test_reuse_after_close_raises_auth_error(self, httpx_mock: HTTPXMock) -> None:
        """close() resets the session — mirror of AsyncNiwaki.close()."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        aci = Niwaki.connect(HOST, "admin", "secret")
        aci.close()
        with pytest.raises(exceptions.AuthError, match="not initialised"):
            aci.query("fvTenant")

    def test_close_twice_is_safe(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        aci = Niwaki.connect(HOST, "admin", "secret")
        aci.close()
        aci.close()  # no-op, no error


class TestContextManager:
    def test_enter_returns_self(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        with Niwaki.connect(HOST, "admin", "secret") as aci:
            assert isinstance(aci, Niwaki)

    def test_exit_closes_session(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        with Niwaki.connect(HOST, "admin", "secret") as aci:
            session = aci._sync_session  # type: ignore[reportPrivateUsage]
        assert session.is_closed


# ── DN computation ────────────────────────────────────────────────────────────


class TestDnComputation:
    def test_root_dn(self, aci: Niwaki) -> None:
        assert aci.root.dn == "uni"

    def test_one_level(self, aci: Niwaki) -> None:
        assert aci.root.mo(fvTenant, name="prod").dn == "uni/tn-prod"

    def test_two_levels(self, aci: Niwaki) -> None:
        dn = aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").dn
        assert dn == "uni/tn-prod/BD-web"

    def test_three_levels(self, aci: Niwaki) -> None:
        dn = aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").mo(fvCtx, name="ctx").dn
        assert dn == "uni/tn-prod/BD-web/ctx-ctx"

    def test_node_explicit_dn(self, aci: Niwaki) -> None:
        assert aci.node("uni/tn-prod/BD-web", fvBD).dn == "uni/tn-prod/BD-web"

    def test_node_default_cls_is_managed_object(self, aci: Niwaki) -> None:
        node = aci.node("uni/tn-prod/BD-web")
        assert node.cls is ManagedObject


# ── Read ──────────────────────────────────────────────────────────────────────


class TestRead:
    def test_returns_typed_instance(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
            json=load_fixture("fvBD_list"),
        )
        bd = aci.root.mo(fvTenant, name="Prod").mo(fvBD, name="Prod-BD").read()
        assert isinstance(bd, fvBD)
        assert bd.name == "Prod-BD"

    def test_not_found_raises(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            status_code=404,
            json=load_fixture("error_404"),
        )
        with pytest.raises(exceptions.NotFoundError):
            aci.root.mo(fvTenant, name="missing").read()

    def test_node_explicit_dn_reads(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{HOST}/api/mo/uni/tn-Prod/BD-Prod-BD.json",
            json=load_fixture("fvBD_list"),
        )
        bd = aci.node("uni/tn-Prod/BD-Prod-BD", fvBD).read()
        assert isinstance(bd, fvBD)


# ── Delete ────────────────────────────────────────────────────────────────────


class TestDelete:
    def test_sends_delete_to_correct_url(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="DELETE", json=ok())
        aci.root.mo(fvTenant, name="prod").mo(fvBD, name="web").delete()
        reqs = [r for r in httpx_mock.get_requests() if r.url.path != "/api/aaaLogin.json"]
        assert reqs[0].method == "DELETE"
        assert reqs[0].url.path == "/api/mo/uni/tn-prod/BD-web.json"

    def test_not_found_raises(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="DELETE",
            status_code=404,
            json=load_fixture("error_404"),
        )
        with pytest.raises(exceptions.NotFoundError):
            aci.root.mo(fvTenant, name="gone").delete()

    def test_forbidden_raises(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            method="DELETE",
            status_code=403,
            json=load_fixture("error_403"),
        )
        with pytest.raises(exceptions.ForbiddenError):
            aci.root.mo(fvTenant, name="prod").delete()


# ── Children via Query ────────────────────────────────────────────────────────


class TestChildren:
    """Direct children queries via .query(cls).children().fetch()."""

    def test_returns_typed_list(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        result = aci.root.mo(fvTenant, name="Prod").query(fvBD).children().fetch()
        assert all(isinstance(bd, fvBD) for bd in result)

    def test_query_target_children_param_sent(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url = str(get_reqs[0].url)
        assert "query-target=children" in url
        assert "target-subtree-class=fvBD" in url

    def test_path_uses_parent_dn(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert "/api/mo/uni/tn-prod.json" in str(get_reqs[0].url)

    def test_autopagination_params_added(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url = str(get_reqs[0].url)
        assert "page=0" in url
        assert "page-size=500" in url

    def test_custom_page_size(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvBD_list"))
        aci.root.mo(fvTenant, name="prod").query(fvBD).children().page_size(50).fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url = str(get_reqs[0].url)
        assert "page=0" in url
        assert "page-size=50" in url

    def test_empty_response(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        result = aci.root.mo(fvTenant, name="prod").query(fvBD).children().fetch()
        assert result == []


# ── Query ─────────────────────────────────────────────────────────────────────


class TestQuery:
    def test_returns_typed_list(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))
        result = aci.query(fvTenant).fetch()
        assert all(isinstance(t, fvTenant) for t in result)
        assert len(result) == 3

    def test_autopagination_params_added(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))
        aci.query(fvTenant).fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url_str = str(get_reqs[0].url)
        assert "page=0" in url_str
        assert "page-size=500" in url_str

    def test_custom_page_size_respected(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=load_fixture("fvTenant_list"))
        aci.query(fvTenant).page_size(50).fetch()
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        url_str = str(get_reqs[0].url)
        assert "page=0" in url_str
        assert "page-size=50" in url_str

    def test_empty_result(self, aci: Niwaki, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        assert aci.query(fvBD).fetch() == []


# ── RetryConfig passthrough ───────────────────────────────────────────────────


class TestRetryConfig:
    def test_retry_config_accepted(self, httpx_mock: HTTPXMock) -> None:
        from niwaki.transport import RetryConfig

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        rc = RetryConfig(attempts=1)
        aci = Niwaki(HOST, "admin", "secret", retry=rc)
        with aci:
            assert aci.retry is rc

    def test_retry_config_none_uses_default(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        aci = Niwaki(HOST, "admin", "secret")
        with aci:
            assert aci.retry is None
