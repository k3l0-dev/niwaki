"""Behavioural equivalence matrix for ``Query`` and ``AsyncQuery``.

The two builders share every accumulator through ``_QueryBase`` but each owns
its executors — ``fetch``, ``first``, ``one``, ``count``, ``exists``,
``stream``, ``execute_raw`` — written twice.  Their own test files mock the
session away, so neither proves what reaches the wire.

Here every scenario runs **both** builders against real sessions mocked by
``pytest-httpx``: the sync builder first, then the async one, each served the
same answers.  The two runs must emit the same requests (full URL, query string
included) and end the same way — the same value, or the same exception with the
same message.  ``pytest-httpx`` fails the test on any unexpected or unconsumed
request, so "no request at all" is asserted as strictly as "this request".

The one deliberate difference — how a query is *iterated* (``for`` versus
``async for``) — is tested as each side's own contract at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.exceptions._query import MultipleResultsError, NoResultError
from niwaki.models.base import ManagedObject
from niwaki.query import AsyncQuery, Query
from niwaki.query._base import _QueryBase
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload

USER = "admin"
PASSWORD = "secret"  # pragma: allowlist secret — documentation placeholder
BDS_URL = f"{HOST}/api/class/fvBD.json"
PROD_URL = f"{HOST}/api/mo/uni/tn-prod.json"

type Body = dict[str, Any] | tuple[int, dict[str, Any]]


# ── The harness ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Twins:
    """One logged-in session per side, sharing the same ``httpx_mock``."""

    sync: ApicSession
    async_: AsyncApicSession

    def query(self, cls: str = "fvBD") -> tuple[Query[ManagedObject], AsyncQuery[ManagedObject]]:
        """The same unshaped query, built once per side."""
        return Query(cls, self.sync), AsyncQuery(cls, self.async_)


@dataclass(frozen=True)
class Outcome:
    """What one run did on the wire and how it ended."""

    urls: list[str]
    value: object
    error: tuple[type[BaseException], str] | None


@pytest.fixture
async def twins(httpx_mock: HTTPXMock) -> AsyncIterator[Twins]:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t-sync"))
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload("t-async"))
    sync = ApicSession(HOST, USER, PASSWORD)
    sync.login()
    async_ = AsyncApicSession(HOST, USER, PASSWORD)
    await async_.login()
    yield Twins(sync, async_)
    sync.close()
    await async_.close()


def serve(httpx_mock: HTTPXMock, bodies: list[Body]) -> None:
    """Queue one run's worth of GET answers, in order."""
    for body in bodies:
        match body:
            case (int() as status, dict() as payload):
                httpx_mock.add_response(method="GET", status_code=status, json=payload)
            case dict():
                httpx_mock.add_response(method="GET", json=body)


def data_urls(httpx_mock: HTTPXMock, since: int) -> list[str]:
    return [str(r.url) for r in httpx_mock.get_requests()[since:]]


async def both(
    httpx_mock: HTTPXMock,
    bodies: list[Body],
    run_sync: Callable[[], object],
    run_async: Callable[[], Awaitable[object]],
) -> Outcome:
    """Run the sync scenario then the async one; they must be indistinguishable."""
    serve(httpx_mock, bodies)
    start = len(httpx_mock.get_requests())
    try:
        sync = Outcome([], run_sync(), None)
    except exceptions.NiwakiError as exc:
        sync = Outcome([], None, (type(exc), str(exc)))
    sync = Outcome(data_urls(httpx_mock, start), sync.value, sync.error)

    serve(httpx_mock, bodies)
    start = len(httpx_mock.get_requests())
    try:
        async_ = Outcome([], await run_async(), None)
    except exceptions.NiwakiError as exc:
        async_ = Outcome([], None, (type(exc), str(exc)))
    async_ = Outcome(data_urls(httpx_mock, start), async_.value, async_.error)

    assert async_.urls == sync.urls, "the twins emitted different requests"
    assert async_.error == sync.error, "the twins ended differently"
    assert async_.value == sync.value, "the twins returned different results"
    return sync


async def collect[T](items: AsyncIterator[T]) -> list[T]:
    return [item async for item in items]


def bd(name: str) -> dict[str, Any]:
    return {"fvBD": {"attributes": {"dn": f"uni/tn-prod/BD-{name}", "name": name}}}


def page(*names: str, total: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"imdata": [bd(n) for n in names]}
    if total is not None:
        body["totalCount"] = str(total)
    return body


def dns(value: object) -> list[str]:
    """The DNs of a list of managed objects, for a readable assertion."""
    assert isinstance(value, list)
    return [mo.dn for mo in value if isinstance(mo, ManagedObject)]


def shaped[Q: _QueryBase[ManagedObject]](q: Q) -> Q:
    """Scope, filter, order and page size — every accumulator that reaches the URL."""
    return q.under("uni/tn-prod").where(name="web").order_by("name").page_size(2)


SHAPED_QS = (
    "query-target=subtree&target-subtree-class=fvBD"
    "&query-target-filter=eq%28fvBD.name%2C%22web%22%29&order-by=fvBD.name%7Casc"
)


# ── fetch ─────────────────────────────────────────────────────────────────────


class TestFetch:
    async def test_it_paginates_a_shaped_query(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()
        q, aq = shaped(q), shaped(aq)

        outcome = await both(
            httpx_mock,
            [page("a", "b", total=3), page("c", total=3)],
            q.fetch,
            aq.fetch,
        )

        assert dns(outcome.value) == ["uni/tn-prod/BD-a", "uni/tn-prod/BD-b", "uni/tn-prod/BD-c"]
        assert outcome.urls == [
            f"{PROD_URL}?{SHAPED_QS}&page=0&page-size=2",
            f"{PROD_URL}?{SHAPED_QS}&page=1&page-size=2",
        ]

    async def test_an_empty_result_is_an_empty_list(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page(total=0)], q.fetch, aq.fetch)

        assert outcome.value == []
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=500"]

    async def test_a_slice_caps_the_page_size_server_side(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page("a", "b", "c", total=10)], q[:3].fetch, aq[:3].fetch)

        assert len(dns(outcome.value)) == 3
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=3"]

    async def test_a_slice_spanning_pages_stops_mid_page(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        q, aq = q.page_size(2)[:3], aq.page_size(2)[:3]

        outcome = await both(
            httpx_mock,
            [page("a", "b", total=10), page("c", "d", total=10)],
            q.fetch,
            aq.fetch,
        )

        assert dns(outcome.value) == ["uni/tn-prod/BD-a", "uni/tn-prod/BD-b", "uni/tn-prod/BD-c"]
        assert len(outcome.urls) == 2

    async def test_an_apic_error_surfaces_identically(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        missing = {"imdata": [{"error": {"attributes": {"code": "107", "text": "gone"}}}]}

        outcome = await both(httpx_mock, [(404, missing)], q.fetch, aq.fetch)

        assert outcome.error is not None
        assert outcome.error[0] is exceptions.NotFoundError


# ── first / one ───────────────────────────────────────────────────────────────


class TestFirst:
    async def test_it_asks_for_one_object(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()
        q, aq = shaped(q), shaped(aq)

        outcome = await both(httpx_mock, [page("a", total=4)], q.first, aq.first)

        assert isinstance(outcome.value, ManagedObject)
        assert outcome.value.dn == "uni/tn-prod/BD-a"
        assert outcome.urls == [f"{PROD_URL}?{SHAPED_QS}&page=0&page-size=1"]

    async def test_no_match_is_none(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page(total=0)], q.first, aq.first)

        assert outcome.value is None
        assert outcome.error is None

    async def test_a_zero_slice_sends_nothing(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [], q[:0].first, aq[:0].first)

        assert outcome.value is None
        assert outcome.urls == []


class TestOne:
    async def test_a_single_match_is_returned(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page("a", total=1)], q.one, aq.one)

        assert isinstance(outcome.value, ManagedObject)
        assert outcome.value.dn == "uni/tn-prod/BD-a"
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=2"]

    @pytest.mark.parametrize(
        ("names", "error"),
        [((), NoResultError), (("a", "b"), MultipleResultsError)],
        ids=["none", "several"],
    )
    async def test_anything_but_one_match_is_a_typed_error(
        self,
        names: tuple[str, ...],
        error: type[exceptions.NiwakiError],
        twins: Twins,
        httpx_mock: HTTPXMock,
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page(*names)], q.one, aq.one)

        assert outcome.error is not None
        assert outcome.error[0] is error

    async def test_a_one_slice_probes_for_one_object_only(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page("a", total=5)], q[:1].one, aq[:1].one)

        assert outcome.error is None
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=1"]

    async def test_a_zero_slice_is_no_result_without_a_request(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [], q[:0].one, aq[:0].one)

        assert outcome.error is not None
        assert outcome.error[0] is NoResultError
        assert outcome.urls == []


# ── count / exists ────────────────────────────────────────────────────────────


class TestCount:
    async def test_it_reads_total_count_from_a_one_object_page(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        q, aq = shaped(q), shaped(aq)

        outcome = await both(httpx_mock, [page("a", total=7)], q.count, aq.count)

        assert outcome.value == 7
        assert outcome.urls == [f"{PROD_URL}?{SHAPED_QS}&page=0&page-size=1"]

    async def test_a_slice_caps_the_count(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page("a", total=7)], q[:3].count, aq[:3].count)

        assert outcome.value == 3

    async def test_a_zero_slice_counts_zero_without_a_request(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [], q[:0].count, aq[:0].count)

        assert outcome.value == 0
        assert outcome.urls == []

    async def test_an_absent_total_count_is_zero(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page()], q.count, aq.count)

        assert outcome.value == 0


class TestExists:
    @pytest.mark.parametrize(("total", "expected"), [(2, True), (0, False)])
    async def test_it_is_a_count_above_zero(
        self, total: int, expected: bool, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page(total=total)], q.exists, aq.exists)

        assert outcome.value is expected
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=1"]

    async def test_a_zero_slice_is_false_without_a_request(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [], q[:0].exists, aq[:0].exists)

        assert outcome.value is False
        assert outcome.urls == []


# ── stream / execute_raw ──────────────────────────────────────────────────────


class TestStream:
    async def test_it_walks_every_page(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()
        q, aq = q.page_size(1), aq.page_size(1)

        outcome = await both(
            httpx_mock,
            [page("a", total=3), page("b", total=3), page("c", total=3)],
            lambda: list(q.stream()),
            lambda: collect(aq.stream()),
        )

        assert dns(outcome.value) == ["uni/tn-prod/BD-a", "uni/tn-prod/BD-b", "uni/tn-prod/BD-c"]
        assert [url.split("?")[1] for url in outcome.urls] == [
            "page=0&page-size=1",
            "page=1&page-size=1",
            "page=2&page-size=1",
        ]

    async def test_a_limit_reached_on_the_first_page_sends_nothing_more(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        q, aq = q[:2], aq[:2]

        outcome = await both(
            httpx_mock,
            [page("a", "b", total=50)],
            lambda: list(q.stream()),
            lambda: collect(aq.stream()),
        )

        assert len(dns(outcome.value)) == 2
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=2"]

    async def test_a_zero_slice_streams_nothing(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()
        q, aq = q[:0], aq[:0]

        outcome = await both(httpx_mock, [], lambda: list(q.stream()), lambda: collect(aq.stream()))

        assert outcome.value == []
        assert outcome.urls == []


class TestExecuteRaw:
    async def test_raw_params_run_through_the_paginated_pipeline(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        params = {"query-target": "subtree", "target-subtree-class": "fvBD"}

        outcome = await both(
            httpx_mock,
            [page("a", "b", total=2)],
            lambda: q.execute_raw("/api/mo/uni/tn-prod.json", params),
            lambda: aq.execute_raw("/api/mo/uni/tn-prod.json", params),
        )

        assert len(dns(outcome.value)) == 2
        assert outcome.urls == [
            f"{PROD_URL}?query-target=subtree&target-subtree-class=fvBD&page=0&page-size=500"
        ]

    async def test_an_explicit_page_is_passed_through_as_one_request(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()
        params = {"page": "4", "page-size": "10"}

        outcome = await both(
            httpx_mock,
            [page("z", total=99)],
            lambda: q.execute_raw("/api/class/fvBD.json", params),
            lambda: aq.execute_raw("/api/class/fvBD.json", params),
        )

        assert dns(outcome.value) == ["uni/tn-prod/BD-z"]
        assert outcome.urls == [f"{BDS_URL}?page=4&page-size=10"]


# ── slicing ───────────────────────────────────────────────────────────────────


class TestSlicing:
    async def test_a_re_slice_narrows(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(
            httpx_mock, [page("a", "b", total=9)], q[:5][:2].fetch, aq[:5][:2].fetch
        )

        assert len(dns(outcome.value)) == 2
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=2"]

    async def test_a_re_slice_never_widens(self, twins: Twins, httpx_mock: HTTPXMock) -> None:
        q, aq = twins.query()

        outcome = await both(
            httpx_mock, [page("a", "b", total=9)], q[:2][:5].fetch, aq[:2][:5].fetch
        )

        assert len(dns(outcome.value)) == 2
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=2"]

    async def test_an_open_slice_is_an_unlimited_copy(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        q, aq = twins.query()

        outcome = await both(httpx_mock, [page("a", total=1)], q[:].fetch, aq[:].fetch)

        assert len(dns(outcome.value)) == 1
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=500"]

    @pytest.mark.parametrize(
        ("index", "error"),
        [
            (slice(1, 3), ValueError),
            (slice(None, 3, 2), ValueError),
            (slice(None, -1), ValueError),
            (0, TypeError),
        ],
        ids=["offset", "step", "negative", "integer"],
    )
    def test_an_unsupported_index_is_refused_identically(
        self, index: Any, error: type[Exception], twins: Twins
    ) -> None:
        q, aq = twins.query()

        with pytest.raises(error) as sync_info:
            q[index]
        with pytest.raises(error) as async_info:
            aq[index]

        assert str(sync_info.value) == str(async_info.value)

    def test_a_slice_keeps_the_builder_kind(self, twins: Twins) -> None:
        q, aq = twins.query()

        assert type(q[:3]) is Query
        assert type(aq[:3]) is AsyncQuery


# ── iteration: the one deliberate difference ──────────────────────────────────


class TestIteration:
    async def test_iterating_the_query_is_streaming_it(
        self, twins: Twins, httpx_mock: HTTPXMock
    ) -> None:
        """``for obj in q`` and ``async for obj in aq`` are the same stream."""
        q, aq = twins.query()
        q, aq = q[:3], aq[:3]

        def sync_iterate() -> list[ManagedObject]:
            iterator: Iterator[ManagedObject] = iter(q)
            return list(iterator)

        async def async_iterate() -> list[ManagedObject]:
            return [obj async for obj in aq]

        outcome = await both(
            httpx_mock,
            [page("a", "b", "c", total=8)],
            sync_iterate,
            async_iterate,
        )

        assert len(dns(outcome.value)) == 3
        assert outcome.urls == [f"{BDS_URL}?page=0&page-size=3"]

    def test_each_builder_refuses_the_other_iteration_protocol(self, twins: Twins) -> None:
        """By design: a sync query has no ``__aiter__``; an async one refuses ``for``.

        Silently accepting the wrong protocol would either block an event loop
        or hand back an un-awaited coroutine, so each side fails loud instead.
        """
        q, aq = twins.query()

        assert not hasattr(q, "__aiter__")
        with pytest.raises(TypeError, match="not synchronously iterable"):
            iter(aq)
