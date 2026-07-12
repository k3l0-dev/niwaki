"""Tests for ApicSession._get_all_pages() transparent auto-pagination."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from niwaki.transport.session import ApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload


@pytest.fixture
def session(httpx_mock: HTTPXMock) -> ApicSession:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    s = ApicSession(host=HOST, username="admin", password="secret")
    s.login()
    return s


class TestGetAllPages:
    """Unit tests for _get_all_pages transparent auto-pagination."""

    def _tenant_page(self, names: list[str], total: int) -> dict[str, Any]:
        return {
            "totalCount": str(total),
            "imdata": [
                {"fvTenant": {"attributes": {"name": name, "dn": f"uni/tn-{name}"}}}
                for name in names
            ],
        }

    def test_single_page_complete_returns_all(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a", "b"], total=2))
        result = session._get_all_pages("/api/class/fvTenant.json", {})  # type: ignore[reportPrivateUsage]
        assert len(result) == 2

    def test_multi_page_fetches_all(self, session: ApicSession, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a", "b"], total=3))
        httpx_mock.add_response(method="GET", json=self._tenant_page(["c"], total=3))
        result = session._get_all_pages("/api/class/fvTenant.json", {}, page_size=2)  # type: ignore[reportPrivateUsage]
        assert len(result) == 3
        names = [item["fvTenant"]["attributes"]["name"] for item in result]
        assert names == ["a", "b", "c"]

    def test_multi_page_sends_correct_page_numbers(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a", "b"], total=3))
        httpx_mock.add_response(method="GET", json=self._tenant_page(["c"], total=3))
        session._get_all_pages("/api/class/fvTenant.json", {}, page_size=2)  # type: ignore[reportPrivateUsage]
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert "page=0" in str(get_reqs[0].url)
        assert "page=1" in str(get_reqs[1].url)

    def test_auto_pagination_adds_page_size(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a"], total=1))
        session._get_all_pages("/api/class/fvTenant.json", {}, page_size=42)  # type: ignore[reportPrivateUsage]
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert "page-size=42" in str(get_reqs[0].url)

    def test_empty_result_returns_empty_list(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json={"totalCount": "0", "imdata": []})
        result = session._get_all_pages("/api/class/fvTenant.json", {})  # type: ignore[reportPrivateUsage]
        assert result == []

    def test_manual_page_bypasses_autopagination(
        self, session: ApicSession, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a", "b"], total=99))
        result = session._get_all_pages("/api/class/fvTenant.json", {"page": "0", "page-size": "2"})  # type: ignore[reportPrivateUsage]
        assert len(result) == 2
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert len(get_reqs) == 1

    def test_extra_params_preserved(self, session: ApicSession, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a"], total=1))
        session._get_all_pages("/api/class/fvTenant.json", {"query-target": "subtree"})  # type: ignore[reportPrivateUsage]
        get_reqs = [r for r in httpx_mock.get_requests() if r.method == "GET"]
        assert "query-target=subtree" in str(get_reqs[0].url)

    def test_guard_against_empty_batch(self, session: ApicSession, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="GET", json=self._tenant_page(["a", "b"], total=5))
        httpx_mock.add_response(method="GET", json={"totalCount": "5", "imdata": []})
        result = session._get_all_pages("/api/class/fvTenant.json", {}, page_size=2)  # type: ignore[reportPrivateUsage]
        assert len(result) == 2
