"""Every way of building a session must produce the same shape of session.

A session can be constructed two ways: the ordinary constructor, or by handing
it a pre-built ``httpx`` client (which is how a caller reaches a proxy, mutual
TLS, a custom transport or an exotic timeout without the SDK knowing about any
of them).

Those two paths are where instance state goes missing. The async session
carries fourteen attributes, and three of them were added on three separate
days by three separate lots — a bound, a firmware version, a semaphore. An
extraction that enumerates them by hand will miss one, and the symptom is not a
crash at construction: it is a session that works until the first concurrent
push, then raises ``AttributeError`` from inside the engine.

So the guard does not enumerate. It compares.
"""

from __future__ import annotations

import httpx
import pytest

from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession

HOST = "https://apic.test"


class TestBothConstructionPathsAgree:
    """The comparison the planner asked for, in both flavours."""

    def test_a_sync_session_built_with_a_client_has_the_same_attributes(self) -> None:
        ordinary = ApicSession(HOST, "admin", "secret")
        injected = ApicSession.with_client(httpx.Client(base_url=HOST), HOST, "admin", "secret")
        assert set(vars(injected)) == set(vars(ordinary))

    def test_an_async_session_built_with_a_client_has_the_same_attributes(self) -> None:
        """The one the blocker named: a missing semaphore is invisible until load."""
        ordinary = AsyncApicSession(HOST, "admin", "secret")
        injected = AsyncApicSession.with_client(
            httpx.AsyncClient(base_url=HOST), HOST, "admin", "secret"
        )
        assert set(vars(injected)) == set(vars(ordinary))

    def test_the_injected_async_session_can_actually_bound_a_wave(self) -> None:
        """Not just present — usable. A semaphore of the wrong kind passes the
        set comparison above and still fails at the first ``async with``."""
        injected = AsyncApicSession.with_client(
            httpx.AsyncClient(base_url=HOST), HOST, "admin", "secret", max_concurrent=4
        )
        assert injected.max_concurrent == 4
        assert injected._semaphore._value == 4  # pyright: ignore[reportPrivateUsage]

    def test_the_injected_session_reports_no_firmware_before_login(self) -> None:
        injected = AsyncApicSession.with_client(
            httpx.AsyncClient(base_url=HOST), HOST, "admin", "secret"
        )
        assert injected.apic_version is None


class TestTheInjectedClientIsTheOneUsed:
    async def test_the_caller_s_transport_is_what_requests_go_through(self) -> None:
        """The whole point: a proxy, an mTLS context, a recording transport."""
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"imdata": []})

        client = httpx.AsyncClient(base_url=HOST, transport=httpx.MockTransport(handler))
        session = AsyncApicSession.with_client(client, HOST, "admin", "secret")
        await session._client.get("/api/class/fvTenant.json")  # pyright: ignore[reportPrivateUsage]
        await session.close()

        assert seen == ["/api/class/fvTenant.json"]

    def test_a_sync_caller_s_transport_is_used_too(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"imdata": []})

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(client, HOST, "admin", "secret")
        session._client.get("/api/class/fvBD.json")  # pyright: ignore[reportPrivateUsage]
        session.close()

        assert seen == ["/api/class/fvBD.json"]


class TestConstructionStillValidates:
    """Injecting a client is not a way around the checks the constructor makes."""

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_bound_is_still_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
            AsyncApicSession.with_client(
                httpx.AsyncClient(base_url=HOST), HOST, "admin", "secret", max_concurrent=bad
            )


class TestTheFacadesInjectToo:
    """A client passed to the façade must reach the session it builds.

    Storing it and never forwarding it is the failure this covers: everything
    constructs, everything looks right, and every request quietly goes out
    through the SDK's own client instead of the caller's proxy.
    """

    def test_a_sync_facade_routes_through_the_given_client(self) -> None:
        from niwaki.facade import Niwaki
        from tests.conftest import login_payload

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("aaaLogin.json"):
                return httpx.Response(200, json=login_payload())
            return httpx.Response(200, json={"totalCount": "0", "imdata": []})

        client = httpx.Client(base_url=HOST, transport=httpx.MockTransport(handler))
        with Niwaki.with_client(client, HOST, "admin", "secret") as aci:
            aci.query("fvTenant").fetch()

        assert "/api/aaaLogin.json" in seen
        assert any("fvTenant" in path for path in seen)

    async def test_an_async_facade_routes_through_the_given_client(self) -> None:
        from niwaki.facade import AsyncNiwaki
        from tests.conftest import login_payload

        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("aaaLogin.json"):
                return httpx.Response(200, json=login_payload())
            return httpx.Response(200, json={"totalCount": "0", "imdata": []})

        client = httpx.AsyncClient(base_url=HOST, transport=httpx.MockTransport(handler))
        async with AsyncNiwaki.with_client(client, HOST, "admin", "secret") as aci:
            await aci.query("fvTenant").fetch()

        assert "/api/aaaLogin.json" in seen
        assert any("fvTenant" in path for path in seen)

    def test_an_ordinary_facade_still_constructs(self) -> None:
        """The attribute must exist on every instance, not only injected ones."""
        from niwaki.facade import AsyncNiwaki, Niwaki

        assert Niwaki(HOST, "a", "b")._injected_client is None
        assert AsyncNiwaki(HOST, "a", "b")._injected_client is None
