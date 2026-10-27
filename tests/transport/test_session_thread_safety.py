"""Thread safety of the sync ``ApicSession`` — the promise the public page makes.

``docs/guide/compatibility.md`` (and its reference twin) state that the sync
session is safe to share across threads and that "a 401 triggers exactly one
re-login however many threads race it".  The async twin has serialised its
reactive re-login since the T1 audit (``_reactive_relogin`` under the token
lock, re-logging in only while the live token is still the one that failed);
these tests hold the sync twin to the same contract, and keep the
``ApicSession`` docstring from contradicting the page.

The stampede is made deterministic with a ``threading.Barrier`` inside the
pytest-httpx callback: every worker's first request is held until all of them
have sent it, so all of them receive their 401 before any re-login can run.
Workers are daemon threads joined with a timeout, so a deadlock (a re-login
path re-entering the non-reentrant token lock) fails the test instead of
hanging the suite.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from niwaki.transport.session import ApicSession
from tests.conftest import HOST, load_fixture, login_payload

WORKERS = 5
JOIN_TIMEOUT = 15.0
GET_PATH = "/api/class/fvTenant.json"
POST_DN = "uni/tn-Prod"
DOCS = Path(__file__).resolve().parents[2] / "docs"


class _RevokedTokenApic:
    """An APIC double whose token is revoked under the feet of N workers.

    The first ``WORKERS`` data requests block on a barrier, then all answer
    401 together.  Every later data request succeeds.  Each aaaLogin issues
    a fresh token ``t1``, ``t2``… so the final token names the login count.
    """

    def __init__(self) -> None:
        self.logins = 0
        self._first_wave = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(WORKERS, timeout=JOIN_TIMEOUT)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/aaaLogin.json":
            with self._lock:
                self.logins += 1
                token = f"t{self.logins}"
            return httpx.Response(200, json=login_payload(token))
        with self._lock:
            in_first_wave = self._first_wave < WORKERS
            self._first_wave += 1
        if in_first_wave:
            self._barrier.wait()  # nobody gets a 401 before everybody sent a request
            return httpx.Response(
                401,
                json={
                    "imdata": [{"error": {"attributes": {"code": "401", "text": "Unauthorized"}}}]
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=load_fixture("fvTenant_list"))
        return httpx.Response(200, json={"totalCount": "0", "imdata": []})


def _race(action: Callable[[], object]) -> list[BaseException]:
    """Run ``action`` on ``WORKERS`` daemon threads; return what they raised."""
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker() -> None:
        try:
            action()
        except BaseException as exc:  # surfaced to the test thread below
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
    stuck = [t for t in threads if t.is_alive()]
    assert not stuck, f"{len(stuck)} worker(s) still blocked — deadlock on the re-login path?"
    return errors


class TestConcurrent401:
    def test_concurrent_401_triggers_single_relogin(self, httpx_mock: HTTPXMock) -> None:
        """N threads hitting a revoked token on reads re-login exactly once.

        Mirror of the async ``test_concurrent_401_triggers_single_relogin``:
        one initial login plus exactly one reactive re-login, every worker's
        replay succeeds, and the session ends on the single new token.
        """
        apic = _RevokedTokenApic()
        httpx_mock.add_callback(apic, is_reusable=True)
        results: list[int] = []
        results_lock = threading.Lock()

        with ApicSession(HOST, "admin", "pass") as session:

            def _read() -> None:
                rows = session.get(GET_PATH)
                with results_lock:
                    results.append(len(rows))

            errors = _race(_read)

            assert errors == []
            assert results == [3] * WORKERS
            assert apic.logins == 2  # one initial login + exactly one reactive re-login
            assert session._token_state is not None
            assert session._token_state.token == "t2"
            assert session.is_authenticated

    def test_concurrent_401_on_writes_triggers_single_relogin(self, httpx_mock: HTTPXMock) -> None:
        """The write path (``_raw_write``) honours the same single re-login."""
        apic = _RevokedTokenApic()
        httpx_mock.add_callback(apic, is_reusable=True)

        with ApicSession(HOST, "admin", "pass") as session:
            errors = _race(lambda: session.post_mo(POST_DN, {"fvTenant": {"attributes": {}}}))

            assert errors == []
            assert apic.logins == 2
            assert session._token_state is not None
            assert session._token_state.token == "t2"


class TestDocumentedThreadSafety:
    """The class docstring and the public pages tell one story."""

    @pytest.mark.parametrize("page", ["guide/compatibility.md", "reference/compatibility.md"])
    def test_the_public_pages_promise_a_single_relogin(self, page: str) -> None:
        text = " ".join((DOCS / page).read_text().split())
        assert "safe to share across threads" in text
        assert "exactly one re-login" in text

    def test_the_docstring_does_not_claim_the_session_is_not_thread_safe(self) -> None:
        doc = " ".join((ApicSession.__doc__ or "").split())
        assert not re.search(r"not\s+thread-safe", doc, re.IGNORECASE), (
            "ApicSession's docstring says it is not thread-safe; the public "
            "compatibility page promises it is safe to share across threads"
        )
        assert "one session per thread" not in doc

    def test_the_docstring_states_the_same_contract_as_the_page(self) -> None:
        doc = " ".join((ApicSession.__doc__ or "").split())
        assert re.search(r"safe to share across threads", doc, re.IGNORECASE)
        assert re.search(r"one re-login", doc, re.IGNORECASE)
