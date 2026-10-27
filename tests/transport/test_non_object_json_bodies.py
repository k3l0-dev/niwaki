"""A body that is valid JSON but not an object surfaces as ``APIError``.

The APIC always answers with a JSON object (``{"totalCount": ..., "imdata":
[...]}``).  Anything else — a list, a bare string, ``null`` — is a proxy, a
captive portal or a broken simulator talking.  The data paths already turn
such a body into :class:`~niwaki.exceptions.APIError` (carrying the real HTTP
status); these tests pin the same contract on the two paths that used to read
``resp.json()`` straight into a ``JsonObject`` binding and then failed later as
a raw ``AttributeError`` / ``TypeError``:

* ``Query.count()`` / ``AsyncQuery.count()`` — including a ``totalCount`` that
  is not a number, the same malformed-body family;
* ``subscribe()`` on the sync and async subscription sockets
  (``SubscribeRejectedError`` is an ``APIError`` too, so either is accepted).
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from niwaki import Niwaki, exceptions
from niwaki.query import AsyncQuery
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST, LOGIN_URL, login_payload

# Raw bytes, so ``null`` really is the JSON literal (``json=None`` would send
# an empty body — a different failure, already covered by the transport).
_NON_OBJECT_BODIES = [b"[1, 2]", b'["fvBD"]', b'"imdata"', b"null", b"42"]
_NON_OBJECT_IDS = ["list", "list-of-strings", "string", "null", "number"]

_BAD_TOTALS = [{"x": 1}, [3], "many"]
_BAD_TOTAL_IDS = ["object", "list", "non-numeric-string"]


def _add_body(httpx_mock: HTTPXMock, body: bytes) -> None:
    httpx_mock.add_response(
        method="GET", content=body, headers={"Content-Type": "application/json"}
    )


# ── count() ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=_NON_OBJECT_IDS)
def test_sync_count_rejects_a_non_object_body(
    aci: Niwaki, httpx_mock: HTTPXMock, body: bytes
) -> None:
    _add_body(httpx_mock, body)
    with pytest.raises(exceptions.APIError) as exc_info:
        aci.query("fvBD").count()
    assert exc_info.value.status_code == 200


@pytest.mark.parametrize("total", _BAD_TOTALS, ids=_BAD_TOTAL_IDS)
def test_sync_count_rejects_a_malformed_total(
    aci: Niwaki, httpx_mock: HTTPXMock, total: object
) -> None:
    httpx_mock.add_response(method="GET", json={"totalCount": total, "imdata": []})
    with pytest.raises(exceptions.APIError):
        aci.query("fvBD").count()


def test_sync_count_still_reads_a_well_formed_total(aci: Niwaki, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json={"totalCount": "7", "imdata": []})
    assert aci.query("fvBD").count() == 7


async def _async_session(httpx_mock: HTTPXMock) -> AsyncApicSession:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    session = AsyncApicSession(HOST, "admin", "secret")
    await session.login()
    return session


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=_NON_OBJECT_IDS)
async def test_async_count_rejects_a_non_object_body(httpx_mock: HTTPXMock, body: bytes) -> None:
    session = await _async_session(httpx_mock)
    _add_body(httpx_mock, body)
    try:
        with pytest.raises(exceptions.APIError) as exc_info:
            await AsyncQuery("fvBD", session).count()
        assert exc_info.value.status_code == 200
    finally:
        await session.close()


@pytest.mark.parametrize("total", _BAD_TOTALS, ids=_BAD_TOTAL_IDS)
async def test_async_count_rejects_a_malformed_total(httpx_mock: HTTPXMock, total: object) -> None:
    session = await _async_session(httpx_mock)
    httpx_mock.add_response(method="GET", json={"totalCount": total, "imdata": []})
    try:
        with pytest.raises(exceptions.APIError):
            await AsyncQuery("fvBD", session).count()
    finally:
        await session.close()


# ── subscribe() ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=_NON_OBJECT_IDS)
def test_sync_subscribe_rejects_a_non_object_body(
    ws_session: ApicSession, httpx_mock: HTTPXMock, body: bytes
) -> None:
    _add_body(httpx_mock, body)
    with pytest.raises(exceptions.APIError):
        ws_session.subscribe("/api/class/fvBD.json", {})


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES, ids=_NON_OBJECT_IDS)
async def test_async_subscribe_rejects_a_non_object_body(
    async_ws_session: AsyncApicSession, httpx_mock: HTTPXMock, body: bytes
) -> None:
    _add_body(httpx_mock, body)
    with pytest.raises(exceptions.APIError):
        await async_ws_session.subscribe("/api/class/fvBD.json", {})
