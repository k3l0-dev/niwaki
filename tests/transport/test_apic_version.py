"""The firmware a fabric reports, and the one this SDK was built for.

"Will this work against my fabric?" had no programmatic answer. The controller
states its version in the login envelope — the SDK read that envelope for the
token and discarded the rest.

Two halves make the question answerable: what the *fabric* runs
(``apic_version``, read at login) and what the *SDK* was generated from
(``catalog.schema_version()``, read from the shipped artifact's own manifest
rather than a constant that could drift from it).
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from niwaki import catalog
from niwaki.facade import AsyncNiwaki, Niwaki
from tests.conftest import HOST, LOGIN_URL, login_payload


def _login(version: str | None) -> dict[str, object]:
    """A login payload, optionally naming a firmware version."""
    payload = login_payload()
    attrs = payload["imdata"][0]["aaaLogin"]["attributes"]  # type: ignore[index]
    if version is None:
        attrs.pop("version", None)  # type: ignore[union-attr]
    else:
        attrs["version"] = version  # type: ignore[index]
    return payload


class TestTheSdkKnowsWhatItWasBuiltFor:
    def test_the_schema_version_comes_from_the_shipped_artifact(self) -> None:
        """Not from a constant in the source, which could drift from the data."""
        assert catalog.schema_version() == "6.0(9c)"

    def test_it_is_offline(self) -> None:
        """No APIC involved — the same promise as the rest of the catalogue."""
        assert isinstance(catalog.schema_version(), str)


class TestTheFabricStatesItsOwn:
    def test_it_is_none_before_connecting(self) -> None:
        """Nothing has been asked yet, so nothing is claimed."""
        assert Niwaki(HOST, "admin", "secret").apic_version is None
        assert AsyncNiwaki(HOST, "admin", "secret").apic_version is None

    def test_it_is_read_at_login(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("6.0(9c)"))
        with Niwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version == "6.0(9c)"

    async def test_the_async_client_reads_it_too(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("5.2(8h)"))
        async with AsyncNiwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version == "5.2(8h)"

    def test_a_controller_that_names_none_leaves_it_none(self, httpx_mock: HTTPXMock) -> None:
        """Absence is reported as absence, never as a guess."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login(None))
        with Niwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version is None

    def test_a_silent_refresh_does_not_erase_what_the_fabric_said(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """One quiet reply must not blank a value the login established.

        The refresh endpoint repeats the version in practice, but a controller
        that omits it on one reply should not make the SDK forget the fabric it
        is talking to.
        """
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("6.0(9c)"))
        with Niwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version == "6.0(9c)"
            session = aci._sync_session  # pyright: ignore[reportPrivateUsage]
            import httpx

            session._capture_apic_version(  # pyright: ignore[reportPrivateUsage]
                httpx.Response(200, json={"imdata": [{"aaaLogin": {"attributes": {}}}]})
            )
            assert aci.apic_version == "6.0(9c)"

    def test_a_refresh_that_names_a_new_version_updates_it(self, httpx_mock: HTTPXMock) -> None:
        """A controller upgraded under a long-lived session is not a lie to keep."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("6.0(9c)"))
        with Niwaki(HOST, "admin", "secret") as aci:
            session = aci._sync_session  # pyright: ignore[reportPrivateUsage]
            import httpx

            session._capture_apic_version(  # pyright: ignore[reportPrivateUsage]
                httpx.Response(
                    200, json={"imdata": [{"aaaRefresh": {"attributes": {"version": "6.1(2a)"}}}]}
                )
            )
            assert aci.apic_version == "6.1(2a)"


class TestTheTwoHalvesAnswerTheQuestion:
    def test_a_matching_fabric_is_recognisable(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login(catalog.schema_version()))
        with Niwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version == catalog.schema_version()

    def test_a_divergent_fabric_is_visible_without_being_fatal(self, httpx_mock: HTTPXMock) -> None:
        """A mismatch is a reason to pilot, not an error — nothing raises."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("5.2(8h)"))
        with Niwaki(HOST, "admin", "secret") as aci:
            assert aci.apic_version != catalog.schema_version()

    def test_no_warning_is_emitted_on_a_mismatch(self, httpx_mock: HTTPXMock) -> None:
        """Connecting is not the moment to editorialise about firmware.

        The SDK reports both numbers and lets the caller decide. A warning on
        every connection to a 5.x lab would be noise, and the compatibility
        guide already states the read-tolerant / write-fail-loud stance.
        """
        import warnings

        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login("5.2(8h)"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with Niwaki(HOST, "admin", "secret") as aci:
                _ = aci.apic_version
        assert [w for w in caught if "version" in str(w.message).lower()] == []


@pytest.mark.parametrize("version", ["6.0(9c)", "5.2(8h)", "6.1(4a)", ""])
def test_whatever_the_controller_says_is_carried_verbatim(
    version: str, httpx_mock: HTTPXMock
) -> None:
    """No parsing, no normalising: a version the SDK cannot read is still shown."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=_login(version))
    with Niwaki(HOST, "admin", "secret") as aci:
        assert aci.apic_version == version
