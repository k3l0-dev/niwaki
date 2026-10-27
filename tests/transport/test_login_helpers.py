"""Tests for the login-path helpers shared by both sessions.

Covers :func:`niwaki.transport._credentials.resolve_password` (str/SecretStr/
None, empty-means-absent, the environment fallback, signed sessions) and
:func:`niwaki.transport._errors.transport_failure` (the httpx → niwaki mapping,
with and without the failure class named), plus the guarantee that ordinary,
non-login requests still chain the httpx error they wrap.
"""

from __future__ import annotations

import ssl

import httpx
import pytest
from pydantic import SecretStr

from niwaki import exceptions
from niwaki.transport._credentials import resolve_password
from niwaki.transport._errors import transport_failure
from niwaki.transport.session import ApicSession
from niwaki.transport.session_async import AsyncApicSession
from tests.conftest import HOST

PASSWORD = "h3lper-Pa55"  # pragma: allowlist secret — distinctive test value
ENV_PASSWORD = "h3lper-env-Pa55"  # pragma: allowlist secret — distinctive test value


@pytest.fixture(autouse=True)
def _no_ambient_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start with no ``APIC_PASSWORD`` (the repository ``.env`` may set one)."""
    monkeypatch.delenv("APIC_PASSWORD", raising=False)


# ── resolve_password ──────────────────────────────────────────────────────────


class TestResolvePassword:
    def test_a_plain_string_is_wrapped(self) -> None:
        secret = resolve_password(PASSWORD, signed=False)
        assert isinstance(secret, SecretStr)
        assert secret.get_secret_value() == PASSWORD

    def test_a_secret_is_kept_as_is(self) -> None:
        given = SecretStr(PASSWORD)
        assert resolve_password(given, signed=False) is given

    def test_an_explicit_password_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        assert resolve_password(PASSWORD, signed=False).get_secret_value() == PASSWORD

    @pytest.mark.parametrize("empty", [None, "", SecretStr("")], ids=["none", "str", "secret"])
    def test_an_empty_value_reads_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, empty: str | SecretStr | None
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        assert resolve_password(empty, signed=False).get_secret_value() == ENV_PASSWORD

    @pytest.mark.parametrize("empty", [None, "", SecretStr("")], ids=["none", "str", "secret"])
    def test_an_empty_value_without_the_environment_raises(
        self, empty: str | SecretStr | None
    ) -> None:
        with pytest.raises(KeyError, match="APIC_PASSWORD"):
            resolve_password(empty, signed=False)

    @pytest.mark.parametrize("empty", [None, "", SecretStr("")], ids=["none", "str", "secret"])
    def test_a_signed_session_never_consults_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, empty: str | SecretStr | None
    ) -> None:
        monkeypatch.setenv("APIC_PASSWORD", ENV_PASSWORD)
        assert resolve_password(empty, signed=True).get_secret_value() == ""

    def test_a_signed_session_keeps_a_given_password(self) -> None:
        assert resolve_password(PASSWORD, signed=True).get_secret_value() == PASSWORD

    def test_the_result_is_masked(self) -> None:
        secret = resolve_password(PASSWORD, signed=False)
        assert PASSWORD not in repr(secret)
        assert PASSWORD not in str(secret)


# ── transport_failure ─────────────────────────────────────────────────────────


def _tls_error() -> httpx.ConnectError:
    error = httpx.ConnectError("[SSL] handshake failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed")
    return error


class TestTransportFailure:
    def test_a_timeout_maps_to_timeout_error(self) -> None:
        error = transport_failure(httpx.ConnectTimeout("too slow"), HOST)
        assert type(error) is exceptions.TimeoutError
        assert str(error) == "APIC request timed out: too slow"

    def test_a_read_timeout_is_a_timeout_too(self) -> None:
        assert type(transport_failure(httpx.ReadTimeout("x"), HOST)) is exceptions.TimeoutError

    def test_a_connect_error_maps_to_connection_error(self) -> None:
        error = transport_failure(httpx.ConnectError("refused"), HOST)
        assert type(error) is exceptions.ConnectionError
        assert str(error) == f"Cannot reach APIC at {HOST}: refused"

    def test_an_ssl_caused_connect_error_maps_to_tls_error(self) -> None:
        error = transport_failure(_tls_error(), HOST)
        assert type(error) is exceptions.TLSError
        assert str(error) == (
            f"TLS/SSL error connecting to {HOST}: certificate verify failed: self-signed"
        )

    def test_an_ssl_context_counts_like_a_cause(self) -> None:
        failure = httpx.ConnectError("handshake")
        failure.__context__ = ssl.SSLError("bad record")
        assert type(transport_failure(failure, HOST)) is exceptions.TLSError

    def test_any_other_transport_error_maps_to_the_base_type(self) -> None:
        error = transport_failure(httpx.ReadError("reset by peer"), HOST)
        assert type(error) is exceptions.TransportError
        assert str(error) == "reset by peer"

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (httpx.ConnectTimeout("too slow"), "APIC request timed out (ConnectTimeout): too slow"),
            (httpx.ConnectError("refused"), f"Cannot reach APIC at {HOST} (ConnectError): refused"),
            (httpx.ReadError("reset by peer"), "ReadError: reset by peer"),
        ],
        ids=["timeout", "connect", "other"],
    )
    def test_name_failure_adds_the_httpx_class(
        self, failure: httpx.TransportError, expected: str
    ) -> None:
        assert str(transport_failure(failure, HOST, name_failure=True)) == expected

    def test_name_failure_on_tls_keeps_the_ssl_text(self) -> None:
        message = str(transport_failure(_tls_error(), HOST, name_failure=True))
        assert "(ConnectError)" in message
        assert "certificate verify failed: self-signed" in message

    def test_it_returns_without_chaining(self) -> None:
        """Chaining is the caller's decision: the mapper sets nothing."""
        error = transport_failure(httpx.ConnectError("refused"), HOST)
        assert error.__cause__ is None
        assert error.__context__ is None


# ── Ordinary requests still chain ─────────────────────────────────────────────


class TestOrdinaryRequestsKeepTheirChain:
    """Only the login path severs the chain; every other request keeps it."""

    def test_the_sync_transport_wrapper_chains(self) -> None:
        session = ApicSession(HOST, "u", PASSWORD)
        failure = httpx.ConnectError("refused")
        try:
            with pytest.raises(exceptions.ConnectionError) as excinfo, session._http_transport():
                raise failure
        finally:
            session.close()
        assert excinfo.value.__cause__ is failure
        assert "(ConnectError)" not in str(excinfo.value)

    async def test_the_async_transport_wrapper_chains(self) -> None:
        session = AsyncApicSession(HOST, "u", PASSWORD)
        failure = httpx.ConnectTimeout("too slow")
        try:
            with pytest.raises(exceptions.TimeoutError) as excinfo:
                async with session._http_transport():
                    raise failure
        finally:
            await session.close()
        assert excinfo.value.__cause__ is failure
        assert "(ConnectTimeout)" not in str(excinfo.value)
