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


# ── aiter / slice / limit ─────────────────────────────────────────────────────


class TestAsyncIterAndSlice:
    @pytest.mark.anyio
    async def test_aiter_streams(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(pages=[[_fvTenant_item("a"), _fvTenant_item("b")]])
        got = [t.name async for t in AsyncQuery(fvTenant, session)]
        assert got == ["a", "b"]

    @pytest.mark.anyio
    async def test_slice_limits_results(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        page = [_fvTenant_item(str(i)) for i in range(5)]
        session = _make_async_session(pages=[page])
        got = [t async for t in AsyncQuery(fvTenant, session)[:2]]
        assert len(got) == 2

    @pytest.mark.anyio
    async def test_fetch_honors_limit(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        page = [_fvTenant_item(str(i)) for i in range(5)]
        session = _make_async_session(pages=[page])
        assert len(await AsyncQuery(fvTenant, session)[:2].fetch()) == 2


# ── one / exists / execute_raw ────────────────────────────────────────────────


class TestAsyncOne:
    @pytest.mark.anyio
    async def test_one_returns_single(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[_fvTenant_item("prod")])
        result = await AsyncQuery(fvTenant, session).one()
        assert result.name == "prod"

    @pytest.mark.anyio
    async def test_one_no_result_raises(self) -> None:
        from niwaki.exceptions import NoResultError
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[])
        with pytest.raises(NoResultError):
            await AsyncQuery(fvBD, session).one()

    @pytest.mark.anyio
    async def test_one_multiple_raises(self) -> None:
        from niwaki.exceptions import MultipleResultsError
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[_fvTenant_item("a"), _fvTenant_item("b")])
        with pytest.raises(MultipleResultsError):
            await AsyncQuery(fvTenant, session).one()

    @pytest.mark.anyio
    async def test_one_requests_page_size_2(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[_fvTenant_item("prod")])
        await AsyncQuery(fvTenant, session).one()
        assert session._get_imdata.call_args[0][1]["page-size"] == "2"


class TestAsyncExistsAndRaw:
    @pytest.mark.anyio
    async def test_exists_true(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "2", "imdata": []})
        assert await AsyncQuery(fvBD, session).exists() is True

    @pytest.mark.anyio
    async def test_exists_false(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "0", "imdata": []})
        assert await AsyncQuery(fvBD, session).exists() is False

    @pytest.mark.anyio
    async def test_execute_raw_parses_and_paginates(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(raw_items=[_fvTenant_item("prod")])
        results = await AsyncQuery(fvTenant, session).execute_raw("/api/class/fvTenant.json", {})
        assert len(results) == 1
        session._get_all_pages.assert_awaited_once()


# ── limit / count / sync-iteration guard (async parity) ───────────────────────


class TestAsyncLimitAndIter:
    @pytest.mark.anyio
    async def test_slice_zero_yields_nothing_without_request(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(pages=[[_fvTenant_item("a")]])
        got = [x async for x in AsyncQuery(fvBD, session)[:0]]
        assert got == []
        session._aiter_pages.assert_not_called()

    @pytest.mark.anyio
    async def test_slice_caps_page_size_server_side(self) -> None:
        from niwaki.models._generated.fv.fvTenant import fvTenant
        from niwaki.query import AsyncQuery

        session = _make_async_session(pages=[[_fvTenant_item("a")]])
        _ = [x async for x in AsyncQuery(fvTenant, session)[:3]]
        assert session._aiter_pages.call_args.kwargs["page_size"] == 3

    @pytest.mark.anyio
    async def test_count_honors_limit(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "10", "imdata": []})
        assert await AsyncQuery(fvBD, session)[:3].count() == 3

    @pytest.mark.anyio
    async def test_count_zero_limit_skips_request(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        session = _make_async_session(count_response={"totalCount": "5", "imdata": []})
        assert await AsyncQuery(fvBD, session)[:0].count() == 0
        session._request_checked.assert_not_called()

    def test_sync_iteration_rejected(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        with pytest.raises(TypeError, match="async for"):
            list(AsyncQuery(fvBD, MagicMock()))

    def test_bool_raises(self) -> None:
        from niwaki.models._generated.fv.fvBD import fvBD
        from niwaki.query import AsyncQuery

        with pytest.raises(TypeError, match="no boolean value"):
            bool(AsyncQuery(fvBD, MagicMock()))
