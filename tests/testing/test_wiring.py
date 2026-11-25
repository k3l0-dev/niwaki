"""``niwaki.testing`` — the package, its public surface and how a fake is wired in.

The contract pinned here (decisions recorded for the implementer):

- ``niwaki.testing`` is a public module exporting :class:`FakeApic` and
  :class:`StoredObject` (``__all__``).  ``import niwaki`` never loads it (the
  cold-start budget stays untouched) and it never imports ``pytest`` — only
  the opt-in plugin module ``niwaki.testing.pytest`` does.
- ``FakeApic(*, host="https://fake-apic.invalid")``; ``fake.host`` echoes it.
- ``fake.client(**kwargs) -> Niwaki`` / ``fake.async_client(**kwargs) ->
  AsyncNiwaki`` are built through ``with_client`` on ``fake.transport`` (an
  :class:`httpx.MockTransport`): inert until entered, then logged in with
  fake credentials answered inline (no fixture file).  Every keyword is
  relayed to the client constructor untouched — so the sync client refuses
  ``max_concurrent`` exactly like ``Niwaki`` does.
- The fake claims the firmware the SDK was generated from:
  ``aci.apic_version == catalog.schema_version()``.
- ``fake.handle(request)`` is host-agnostic: it is the callback the
  documentation harness registers with pytest_httpx for snippets that name
  ``https://apic.example.com``.
- ``fake.requests`` is every request received, in order, login included.
"""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest

import niwaki.testing
from niwaki import AsyncNiwaki, Niwaki, catalog
from niwaki.design import tenant
from niwaki.models.fv.fvBD import fvBD
from niwaki.testing import FakeApic, StoredObject
from niwaki.transport import RetryConfig

from .conftest import FABRIC, request


class TestPublicSurface:
    def test_the_package_exports_the_fake_and_its_store_view(self) -> None:
        assert set(niwaki.testing.__all__) >= {"FakeApic", "StoredObject"}
        assert niwaki.testing.FakeApic is FakeApic
        assert niwaki.testing.StoredObject is StoredObject

    def test_the_default_host_is_a_reserved_invalid_name(self) -> None:
        assert FakeApic().host == "https://fake-apic.invalid"

    def test_the_host_is_configurable_and_keyword_only(self) -> None:
        assert FakeApic(host="https://apic.lab.test").host == "https://apic.lab.test"
        with pytest.raises(TypeError):
            FakeApic("https://apic.lab.test")  # pyright: ignore[reportCallIssue]

    def test_the_transport_is_an_httpx_mock_transport(self) -> None:
        assert isinstance(FakeApic().transport, httpx.MockTransport)

    def test_a_new_fake_holds_only_the_universe_and_has_heard_nothing(self) -> None:
        fake = FakeApic()
        assert fake.objects() == (StoredObject(dn="uni", aci_class="polUni", attributes={}),)
        assert list(fake.requests) == []


class TestImportIsolation:
    """Measured in a fresh interpreter: the test process has both loaded already."""

    def test_niwaki_does_not_load_testing_and_testing_does_not_load_pytest(self) -> None:
        probe = (
            "import json, sys\n"
            "import niwaki\n"
            "after_niwaki = 'niwaki.testing' in sys.modules\n"
            "import niwaki.testing\n"
            "pytest_loaded = sorted(m for m in sys.modules "
            "if m == 'pytest' or m.startswith('_pytest') or m.startswith('pytest_'))\n"
            "print(json.dumps({'after_niwaki': after_niwaki, 'pytest': pytest_loaded}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        ).stdout
        result = json.loads(out)
        assert result["after_niwaki"] is False, "import niwaki must not import niwaki.testing"
        assert result["pytest"] == [], (
            f"niwaki.testing must not import pytest (loaded: {result['pytest']})"
        )


class TestSyncClient:
    def test_the_client_is_a_niwaki_and_is_inert_until_entered(self, fake: FakeApic) -> None:
        client = fake.client()
        assert isinstance(client, Niwaki)
        assert list(fake.requests) == []

    def test_entering_logs_in_against_the_fake(self, fake: FakeApic) -> None:
        with fake.client() as aci:
            assert aci.apic_version == catalog.schema_version()
        first = fake.requests[0]
        assert (first.method, first.url.path) == ("POST", "/api/aaaLogin.json")

    def test_every_request_goes_to_the_fake_host(self) -> None:
        fake = FakeApic(host="https://apic.lab.test")
        with fake.client() as aci:
            aci.query(fvBD).fetch()
        assert {r.url.host for r in fake.requests} == {"apic.lab.test"}

    def test_push_then_read_round_trips(self, fake: FakeApic) -> None:
        with fake.client() as aci:
            config = tenant("prod")
            config.bd("web", unicast_routing=True).bind(vrf="main")
            config.vrf("main")
            config.push(aci)
            bd = aci.tenant("prod").bd("web").read()
        assert isinstance(bd, fvBD)
        assert bd.unicast_routing is True
        assert fake.get("uni/tn-prod/BD-web/rsctx") is not None

    def test_the_retry_policy_is_relayed(self, fake: FakeApic) -> None:
        policy = RetryConfig(attempts=1)
        assert fake.client(retry=policy).retry is policy

    def test_keywords_reach_the_constructor_untouched(self, fake: FakeApic) -> None:
        """The sync client has no ``max_concurrent``: relayed, it must be refused."""
        with pytest.raises(TypeError):
            fake.client(max_concurrent=3)

    def test_a_token_refresh_is_served_inline(self, fake: FakeApic) -> None:
        # A threshold larger than any token lifetime forces a proactive refresh
        # before every request.
        with fake.client(refresh_threshold=10**7) as aci:
            aci.query(fvBD).fetch()
        paths = [r.url.path for r in fake.requests]
        assert "/api/aaaRefresh.json" in paths
        assert "/api/class/fvBD.json" in paths

    def test_a_session_survives_several_clients(self, fake: FakeApic) -> None:
        """The store belongs to the fake, not to a client."""
        with fake.client() as writer:
            tenant("prod").bd("web").push(writer)
        with fake.client() as reader:
            assert reader.tenant("prod").bd("web").read().name == "web"


class TestAsyncClient:
    async def test_the_async_client_is_an_async_niwaki_and_inert(self, fake: FakeApic) -> None:
        client = fake.async_client()
        assert isinstance(client, AsyncNiwaki)
        assert list(fake.requests) == []

    async def test_async_push_then_read_round_trips(self, fake: FakeApic) -> None:
        async with fake.async_client() as aci:
            config = tenant("prod")
            config.bd("web", unicast_routing=True).bind(vrf="main")
            config.vrf("main")
            await config.push(aci, mode="staged")
            bd = await aci.tenant("prod").bd("web").read()
            found = await aci.query(fvBD).fetch()
        assert bd.unicast_routing is True
        assert [mo.dn for mo in found] == ["uni/tn-prod/BD-web"]
        assert fake.requests[0].url.path == "/api/aaaLogin.json"

    async def test_max_concurrent_is_relayed(self, fake: FakeApic) -> None:
        assert fake.async_client(max_concurrent=3).max_concurrent == 3


class TestHandMadeClient:
    def test_with_client_over_the_transport(self, fake: FakeApic) -> None:
        http = httpx.Client(base_url=fake.host, transport=fake.transport)
        with Niwaki.with_client(http, fake.host, "ops", "anything") as aci:
            tenant("prod").bd("web").push(aci)
            assert aci.tenant("prod").bd("web").read().name == "web"
        assert fake.get("uni/tn-prod/BD-web") is not None

    async def test_async_with_client_over_the_transport(self, fake: FakeApic) -> None:
        http = httpx.AsyncClient(base_url=fake.host, transport=fake.transport)
        async with AsyncNiwaki.with_client(http, fake.host, "ops", "anything") as aci:
            await tenant("prod").bd("web").push(aci)
            assert (await aci.tenant("prod").bd("web").read()).name == "web"


class TestHandle:
    def test_handle_answers_whatever_host_it_is_given(self, fake: FakeApic) -> None:
        fake.seed(FABRIC)
        response = fake.handle(request("GET", "/api/mo/uni/tn-prod.json"))
        assert response.status_code == 200

    def test_login_and_refresh_are_answered_without_fixture_files(self, fake: FakeApic) -> None:
        login = fake.handle(
            request(
                "POST", "/api/aaaLogin.json", {"aaaUser": {"attributes": {"name": "a", "pwd": "b"}}}
            )
        )
        refresh = fake.handle(request("GET", "/api/aaaRefresh.json"))
        for response, key in ((login, "aaaLogin"), (refresh, "aaaRefresh")):
            assert response.status_code == 200
            (item,) = json.loads(response.content)["imdata"]
            attributes = item[key]["attributes"]
            assert attributes["token"]
            assert int(attributes["refreshTimeoutSeconds"]) > 0

    def test_handled_requests_are_logged_in_order(self, fake: FakeApic) -> None:
        fake.handle(request("GET", "/api/mo/uni.json"))
        fake.handle(request("DELETE", "/api/mo/uni/tn-x.json"))
        assert [(r.method, r.url.path) for r in fake.requests] == [
            ("GET", "/api/mo/uni.json"),
            ("DELETE", "/api/mo/uni/tn-x.json"),
        ]

    def test_seeding_is_not_a_request(self, fake: FakeApic) -> None:
        fake.seed(FABRIC)
        assert list(fake.requests) == []
