"""``verify_ssl`` — bool passthrough and CA-bundle-path support."""

from __future__ import annotations

import ssl
from typing import Any

import certifi
import httpx
import pytest

from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession


@pytest.fixture
def captured_verify(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the ``verify=`` kwarg handed to the httpx client constructors."""
    seen: dict[str, Any] = {}

    class _SpyClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["sync"] = kwargs.get("verify")
            super().__init__(*args, **kwargs)

    class _SpyAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["async"] = kwargs.get("verify")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _SpyClient)
    monkeypatch.setattr(httpx, "AsyncClient", _SpyAsyncClient)
    return seen


class TestVerifySslBools:
    """Booleans reach httpx untouched."""

    @pytest.mark.parametrize("value", [True, False])
    def test_sync_session(self, captured_verify: dict[str, Any], value: bool) -> None:
        session = ApicSession("https://apic", "admin", "pw", verify_ssl=value)
        assert captured_verify["sync"] is value
        session.close()

    @pytest.mark.parametrize("value", [True, False])
    async def test_async_session(self, captured_verify: dict[str, Any], value: bool) -> None:
        session = AsyncApicSession("https://apic", "admin", "pw", verify_ssl=value)
        assert captured_verify["async"] is value
        await session.close()

    def test_default_is_verified(self, captured_verify: dict[str, Any]) -> None:
        session = ApicSession("https://apic", "admin", "pw")
        assert captured_verify["sync"] is True
        session.close()


class TestVerifySslCaBundle:
    """A CA bundle path becomes an ``ssl.SSLContext`` (no httpx deprecation)."""

    def test_sync_session(self, captured_verify: dict[str, Any]) -> None:
        session = ApicSession("https://apic", "admin", "pw", verify_ssl=certifi.where())
        assert isinstance(captured_verify["sync"], ssl.SSLContext)
        session.close()

    async def test_async_session(self, captured_verify: dict[str, Any]) -> None:
        session = AsyncApicSession("https://apic", "admin", "pw", verify_ssl=certifi.where())
        assert isinstance(captured_verify["async"], ssl.SSLContext)
        await session.close()

    def test_missing_bundle_fails_at_construction(self) -> None:
        with pytest.raises(FileNotFoundError):
            ApicSession("https://apic", "admin", "pw", verify_ssl="/nonexistent/ca.pem")
