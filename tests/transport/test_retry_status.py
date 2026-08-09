"""Retrying an APIC *status*, and the asymmetry between reads and writes.

The transport has always retried network failures. It never retried a status,
so a controller answering ``503`` because it was momentarily busy ended a
staged push mid-flight — the one failure mode most worth surviving.

What must not happen is a write being replayed after the APIC may already have
applied it. That is why the two sets are disjoint rather than one set shared:
a ``502`` or ``504`` means a gateway already forwarded the request, so a write
must not try again, while a read may.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki import exceptions
from niwaki.facade import Niwaki
from niwaki.transport import RetryConfig
from niwaki.transport._retry import (
    READ_RETRY_STATUSES,
    WRITE_RETRY_STATUSES,
    retry_after_seconds,
)
from tests.conftest import HOST, LOGIN_URL, login_payload, ok

FAST = RetryConfig(attempts=3, wait_initial=0.001, wait_max=0.002, wait_jitter=0.0)


@pytest.fixture(autouse=True)
def _let_retries_actually_happen() -> Iterator[None]:
    """Opt this module out of the suite-wide ``stamina.set_testing(True)``.

    That fixture caps stamina at a single attempt so unit tests never wait.
    It is the right default everywhere else, and it makes a retry test
    vacuous: without this, every assertion below would pass by never retrying
    at all.  Attempts are honoured here; the backoff stays at the millisecond
    ``FAST`` asks for.
    """
    import stamina

    stamina.set_testing(True, attempts=99)
    try:
        yield
    finally:
        stamina.set_testing(True)


def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {})


class TestTheSetsAreDisjointOnPurpose:
    def test_a_read_retries_the_three_busy_statuses(self) -> None:
        assert frozenset({502, 503, 504}) == READ_RETRY_STATUSES

    def test_a_write_retries_only_the_one_that_proves_nothing_happened(self) -> None:
        """502 and 504 mean a gateway already forwarded the request."""
        assert frozenset({503}) == WRITE_RETRY_STATUSES
        assert 502 not in WRITE_RETRY_STATUSES
        assert 504 not in WRITE_RETRY_STATUSES

    def test_429_is_absent_because_nothing_was_observed_emitting_it(self) -> None:
        """A retry set is a claim about a controller, not a wish."""
        assert 429 not in READ_RETRY_STATUSES | WRITE_RETRY_STATUSES


class TestRetryAfterParsing:
    def test_the_numeric_form(self) -> None:
        assert retry_after_seconds(_resp(503, {"retry-after": "5"}), ceiling=30.0) == 5.0

    def test_the_http_date_form(self) -> None:
        from email.utils import parsedate_to_datetime

        stamp = "Wed, 21 Oct 2026 07:28:00 GMT"
        target = parsedate_to_datetime(stamp).timestamp()
        got = retry_after_seconds(_resp(503, {"retry-after": stamp}), ceiling=30.0, now=target - 10)
        assert got is not None
        assert 9.0 < got <= 10.0

    def test_it_is_clamped_so_one_busy_answer_cannot_park_a_push(self) -> None:
        assert retry_after_seconds(_resp(503, {"retry-after": "3600"}), ceiling=30.0) == 30.0

    @pytest.mark.parametrize(
        "value",
        ["", "soon", "not-a-date", "-5", "0", "NaN"],
        ids=["empty", "word", "bad-date", "negative", "zero", "nan"],
    )
    def test_an_unusable_value_reads_as_absent_never_as_a_crash(self, value: str) -> None:
        """A header the SDK cannot parse must not explode inside the retry loop."""
        assert retry_after_seconds(_resp(503, {"retry-after": value}), ceiling=30.0) is None

    def test_no_header_at_all(self) -> None:
        assert retry_after_seconds(_resp(503), ceiling=30.0) is None


class TestReadsRetry:
    def test_a_503_is_retried_and_the_next_answer_is_used(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503)
        httpx_mock.add_response(method="GET", json=ok())

        with Niwaki(HOST, "admin", "secret", retry=FAST) as aci:
            assert aci.query("fvTenant").fetch() == []

        assert len([r for r in httpx_mock.get_requests() if r.method == "GET"]) == 2

    def test_exhausting_the_attempts_surfaces_the_status_not_an_internal_signal(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """The user must see ``HTTP 503``, never a retry mechanism's exception."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503, is_reusable=True)

        with (
            Niwaki(HOST, "admin", "secret", retry=FAST) as aci,
            pytest.raises(exceptions.ServerError) as excinfo,
        ):
            aci.query("fvTenant").fetch()

        assert excinfo.value.status_code == 503

    def test_a_500_is_not_retried(self, httpx_mock: HTTPXMock) -> None:
        """500 means the APIC processed it and failed — trying again changes nothing."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=500)

        with (
            Niwaki(HOST, "admin", "secret", retry=FAST) as aci,
            pytest.raises(exceptions.ServerError),
        ):
            aci.query("fvTenant").fetch()

        assert len([r for r in httpx_mock.get_requests() if r.method == "GET"]) == 1


class TestWritesAreStricter:
    def test_a_503_write_is_retried(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", status_code=503)
        httpx_mock.add_response(method="POST", json=ok())

        with Niwaki(HOST, "admin", "secret", retry=FAST) as aci:
            aci._sync_session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {}}})

        posts = [r for r in httpx_mock.get_requests() if r.url.path != "/api/aaaLogin.json"]
        assert len(posts) == 2

    @pytest.mark.parametrize("status", [502, 504])
    def test_a_gateway_status_is_never_replayed_on_a_write(
        self, status: int, httpx_mock: HTTPXMock
    ) -> None:
        """The gateway forwarded it; the APIC may already hold the object.

        Replaying would either double-apply the write or 404 against something
        the first attempt created. One attempt, then the error.
        """
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", status_code=status)

        with (
            Niwaki(HOST, "admin", "secret", retry=FAST) as aci,
            pytest.raises(exceptions.ServerError),
        ):
            aci._sync_session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {}}})

        posts = [r for r in httpx_mock.get_requests() if r.url.path != "/api/aaaLogin.json"]
        assert len(posts) == 1


class TestRetryAfterIsHonoured:
    def test_the_controller_s_delay_is_actually_waited(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503, headers={"retry-after": "0.25"})
        httpx_mock.add_response(method="GET", json=ok())

        started = time.monotonic()
        with Niwaki(HOST, "admin", "secret", retry=FAST) as aci:
            aci.query("fvTenant").fetch()
        elapsed = time.monotonic() - started

        # FAST's own backoff is a millisecond, so anything near a quarter second
        # can only have come from honouring the header.
        assert elapsed >= 0.25

    def test_a_huge_delay_is_clamped_by_retry_after_max(self, httpx_mock: HTTPXMock) -> None:
        """Without the ceiling this test would take an hour."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503, headers={"retry-after": "3600"})
        httpx_mock.add_response(method="GET", json=ok())

        capped = RetryConfig(
            attempts=3, wait_initial=0.001, wait_max=0.002, wait_jitter=0.0, retry_after_max=0.2
        )
        started = time.monotonic()
        with Niwaki(HOST, "admin", "secret", retry=capped) as aci:
            aci.query("fvTenant").fetch()
        elapsed = time.monotonic() - started

        assert 0.2 <= elapsed < 2.0


class TestTheAsyncTwinBehavesIdentically:
    """The two sessions use different stamina shapes — a loop and a decorator.

    That asymmetry is exactly where a fix lands on one side only, so the async
    path is asserted on its own rather than assumed to follow.
    """

    async def test_a_503_read_is_retried(self, httpx_mock: HTTPXMock) -> None:
        from niwaki.facade import AsyncNiwaki

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503)
        httpx_mock.add_response(method="GET", json=ok())

        async with AsyncNiwaki(HOST, "admin", "secret", retry=FAST) as aci:
            assert await aci.query("fvTenant").fetch() == []

        assert len([r for r in httpx_mock.get_requests() if r.method == "GET"]) == 2

    @pytest.mark.parametrize("status", [502, 504])
    async def test_a_gateway_status_is_never_replayed_on_an_async_write(
        self, status: int, httpx_mock: HTTPXMock
    ) -> None:
        from niwaki.facade import AsyncNiwaki

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", status_code=status)

        async with AsyncNiwaki(HOST, "admin", "secret", retry=FAST) as aci:
            session = aci._active_session  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(exceptions.ServerError):
                await session.post_mo("uni/tn-x", {"fvTenant": {"attributes": {}}})

        posts = [r for r in httpx_mock.get_requests() if r.url.path != "/api/aaaLogin.json"]
        assert len(posts) == 1

    async def test_exhaustion_surfaces_the_status_not_the_internal_signal(
        self, httpx_mock: HTTPXMock
    ) -> None:
        from niwaki.facade import AsyncNiwaki

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="GET", status_code=503, is_reusable=True)

        async with AsyncNiwaki(HOST, "admin", "secret", retry=FAST) as aci:
            with pytest.raises(exceptions.ServerError) as excinfo:
                await aci.query("fvTenant").fetch()

        assert excinfo.value.status_code == 503
