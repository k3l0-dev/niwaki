"""``push(max_concurrent=...)`` — the throttle, and what it can and cannot do.

The bound is a *throttle down*. A push cannot make its session hand out more
slots than the session owns, so the effective limit is the smaller of the two.
Getting that backwards would ship a knob that silently does nothing when someone
raises it, which is worse than no knob at all.

The other property guarded here is the one that is easy to lose by accident:
omitting the argument must reproduce earlier releases byte for byte. A constant
default in the engine would look correct in every test written against a default
client, and quietly throttle anyone who had raised their client's limit.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import tenant
from niwaki.facade import AsyncNiwaki, Niwaki
from tests.conftest import HOST, LOGIN_URL, login_payload, ok


def _wide_design(width: int) -> Any:
    """A design whose widest wave is *width* siblings — bridge domains."""
    cfg = tenant("prod")
    for i in range(width):
        cfg.bd(f"bd{i:03d}")
    return cfg


class _InFlightCounter:
    """Counts genuinely simultaneous HTTP requests, through a mock callback."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.current = 0
        self.peak = 0
        self.peak_tasks = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("aaaLogin.json"):
            return httpx.Response(200, json=login_payload())
        self.current += 1
        self.peak = max(self.peak, self.current)
        self.peak_tasks = max(self.peak_tasks, len(asyncio.all_tasks()))
        await asyncio.sleep(self.delay)
        self.current -= 1
        return httpx.Response(200, json=ok())


async def _push_counting(
    httpx_mock: HTTPXMock, width: int, *, client_limit: int, push_limit: int | None
) -> int:
    """Push a *width*-wide design and return the peak simultaneous requests."""
    counter = _InFlightCounter()
    httpx_mock.add_callback(counter, is_reusable=True)

    async with AsyncNiwaki(HOST, "admin", "secret", max_concurrent=client_limit) as aci:
        design = _wide_design(width)
        if push_limit is None:
            await design.push(aci, mode="staged")
        else:
            await design.push(aci, mode="staged", max_concurrent=push_limit)
    return counter.peak


async def _push_counting_tasks(
    httpx_mock: HTTPXMock, width: int, *, client_limit: int, push_limit: int | None
) -> int:
    """Same push, but return the peak number of live asyncio tasks."""
    counter = _InFlightCounter()
    httpx_mock.add_callback(counter, is_reusable=True)
    async with AsyncNiwaki(HOST, "admin", "secret", max_concurrent=client_limit) as aci:
        design = _wide_design(width)
        if push_limit is None:
            await design.push(aci, mode="staged")
        else:
            await design.push(aci, mode="staged", max_concurrent=push_limit)
    return counter.peak_tasks


class TestThrottle:
    async def test_the_push_limit_caps_requests_in_flight(self, httpx_mock: HTTPXMock) -> None:
        peak = await _push_counting(httpx_mock, 30, client_limit=10, push_limit=3)
        assert peak == 3

    async def test_it_throttles_down_but_never_up(self, httpx_mock: HTTPXMock) -> None:
        """Asking for more than the client allows gives the client's limit.

        A caller who reads ``max_concurrent=50`` and expects fifty would
        otherwise be silently wrong; the docstring says so, and this proves the
        code agrees with the docstring.
        """
        peak = await _push_counting(httpx_mock, 30, client_limit=4, push_limit=50)
        assert peak == 4

    async def test_it_does_not_spawn_workers_the_session_cannot_feed(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """The ``min`` is what makes the throttle real, and only tasks show it.

        Taking the caller's number verbatim looks identical on the wire — the
        session's own semaphore caps requests either way — so an HTTP-level
        assertion cannot tell the two apart. The difference is the workers the
        engine builds: without the ``min`` it spawns fifty for a session that
        can feed four, and forty-six of them sit blocked on a semaphore. That
        is precisely the waste this lot exists to remove.
        """
        tasks = await _push_counting_tasks(httpx_mock, 40, client_limit=4, push_limit=50)
        assert tasks <= 4 + 5, f"{tasks} live tasks for a session that allows 4"

    async def test_omitting_it_inherits_the_client_limit(self, httpx_mock: HTTPXMock) -> None:
        """The regression a hardcoded engine default would have shipped.

        A client deliberately built with a raised limit must keep it when no
        push-level bound is given. With a constant in the engine this would be
        ten, throttling an existing call with no opt-in and no way to tell.
        """
        peak = await _push_counting(httpx_mock, 60, client_limit=25, push_limit=None)
        assert peak == 25

    async def test_a_default_client_is_unchanged(self, httpx_mock: HTTPXMock) -> None:
        peak = await _push_counting(httpx_mock, 30, client_limit=10, push_limit=None)
        assert peak == 10


class TestValidation:
    @pytest.mark.parametrize("bad", [0, -1, -100])
    async def test_below_one_raises_before_any_request(
        self, bad: int, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
                await _wide_design(4).push(aci, mode="staged", max_concurrent=bad)
        # Only the login went out — validation happens before the dispatch.
        assert [r.url.path for r in httpx_mock.get_requests()] == ["/api/aaaLogin.json"]

    def test_a_sync_client_rejects_the_same_value(self, httpx_mock: HTTPXMock) -> None:
        """The check sits before the sync/async split, so both fail alike."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        with (
            Niwaki(HOST, "admin", "secret") as aci,
            pytest.raises(ValueError, match="max_concurrent must be >= 1"),
        ):
            _wide_design(4).push(aci, mode="staged", max_concurrent=0)


class TestSyncIsInert:
    def test_a_sync_push_accepts_it_and_writes_everything(self, httpx_mock: HTTPXMock) -> None:
        """The sync engine is serial, so the knob is accepted and does nothing.

        Rejecting it there would force callers to branch on client kind for a
        parameter that is meaningful to only one of them.
        """
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok(), is_reusable=True)

        with Niwaki(HOST, "admin", "secret") as aci:
            report = _wide_design(5).push(aci, mode="staged", max_concurrent=2)

        assert report.request_count == 6  # the tenant plus five BDs
        assert len(report.dns) == 6


class TestClientLimitValidation:
    """The client's own limit is validated where it is set, not where it bites."""

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_client_limit_is_refused_at_construction(self, bad: int) -> None:
        """Otherwise it deadlocks on the first write, or worse, writes nothing.

        A zero-permit semaphore is not a throttle: it is a wait for a slot that
        is never released.  Catching it at construction turns a hang — or a
        silently empty push — into a message naming the value.
        """
        from niwaki.transport.session_async import AsyncApicSession

        with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
            AsyncApicSession(HOST, "admin", "secret", max_concurrent=bad)

    def test_the_client_refuses_it_before_any_connection(self) -> None:
        """End to end: the empty-but-green report must be unreachable.

        The value never reaches the engine because it never reaches a client —
        construction fails, so there is no session, no login, and no push.
        """
        with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
            AsyncNiwaki(HOST, "admin", "secret", max_concurrent=0)
