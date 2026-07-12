"""Tests for AsyncQuery (async) builder — URL/param construction and execution.

Mirrors test_builder.py for the async variant, focusing on the execution layer
(fetch, first, count, stream are all async).  Param-building logic is shared
via _QueryBase and tested exhaustively in test_builder.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fvTenant_item(name: str) -> dict:
    return {"fvTenant": {"attributes": {"name": name}}}


def _fvBD_item(name: str) -> dict:
    return {"fvBD": {"attributes": {"name": name}}}


async def _aiter(pages: list) -> Any:
    """Async generator that yields items from a list."""
    for page in pages:
        yield page


def _make_async_session(**kwargs: Any) -> MagicMock:
    """Return a mock AsyncApicSession with configurable async return values."""
    session = MagicMock()
    session._get_all_pages = AsyncMock(return_value=kwargs.get("raw_items", []))
    session._get_imdata = AsyncMock(return_value=kwargs.get("raw_items", []))
    # _aiter_pages is an async generator — use a wrapper
    pages = kwargs.get("pages", [])
    session._aiter_pages = MagicMock(return_value=_aiter(pages))
    # httpx.Response.json() is synchronous — use MagicMock, not AsyncMock
    resp_mock = MagicMock()
    resp_mock.json.return_value = kwargs.get("count_response", {"totalCount": "0", "imdata": []})
    session._request_checked = AsyncMock(return_value=resp_mock)
    return session


# ── fetch ─────────────────────────────────────────────────────────────────────


class TestAsyncFetch:
    @pytest.mark.anyio
    async def test_fetch_returns_typed_list(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        raw = [_fvTenant_item("prod"), _fvTenant_item("dev")]
        session = _make_async_session(raw_items=raw)
        result = await AsyncQuery(fvTenant, session).fetch()
        assert len(result) == 2
        assert all(isinstance(t, fvTenant) for t in result)

    @pytest.mark.anyio
    async def test_fetch_empty(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[])
        result = await AsyncQuery(fvBD, session).fetch()
        assert result == []

    @pytest.mark.anyio
    async def test_fetch_calls_get_all_pages(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session()
        await AsyncQuery(fvTenant, session).fetch()
        session._get_all_pages.assert_awaited_once()

    @pytest.mark.anyio
    async def test_fetch_scoped_uses_mo_path(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session()
        await AsyncQuery(fvBD, session, scope_dn="uni/tn-prod").fetch()
        call_path = session._get_all_pages.call_args[0][0]
        assert call_path == "/api/mo/uni/tn-prod.json"


# ── first ─────────────────────────────────────────────────────────────────────


class TestAsyncFirst:
    @pytest.mark.anyio
    async def test_first_returns_object(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        raw = [_fvTenant_item("prod")]
        session = _make_async_session(raw_items=raw)
        result = await AsyncQuery(fvTenant, session).first()
        assert isinstance(result, fvTenant)
        assert result.name == "prod"

    @pytest.mark.anyio
    async def test_first_empty_returns_none(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[])
        assert await AsyncQuery(fvBD, session).first() is None

    @pytest.mark.anyio
    async def test_first_uses_page_size_1(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session()
        await AsyncQuery(fvTenant, session).first()
        call_params = session._get_imdata.call_args[0][1]
        assert call_params["page"] == "0"
        assert call_params["page-size"] == "1"


# ── count ─────────────────────────────────────────────────────────────────────


class TestAsyncCount:
    @pytest.mark.anyio
    async def test_count_returns_integer(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "17", "imdata": []})
        n = await AsyncQuery(fvBD, session).count()
        assert n == 17

    @pytest.mark.anyio
    async def test_count_adds_count_only_param(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "3", "imdata": []})
        await AsyncQuery(fvBD, session).count()
        call_params = session._request_checked.call_args[0][1]
        assert call_params["page-size"] == "1"  # count = totalCount of a 1-object page


# ── stream ────────────────────────────────────────────────────────────────────


class TestAsyncStream:
    @pytest.mark.anyio
    async def test_stream_yields_objects(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        page1 = [_fvTenant_item("prod"), _fvTenant_item("dev")]
        page2 = [_fvTenant_item("infra")]
        session = _make_async_session(pages=[page1, page2])
        results = [obj async for obj in AsyncQuery(fvTenant, session).stream()]
        assert len(results) == 3
        assert all(isinstance(t, fvTenant) for t in results)

    @pytest.mark.anyio
    async def test_stream_empty(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(pages=[])
        results = [obj async for obj in AsyncQuery(fvBD, session).stream()]
        assert results == []
