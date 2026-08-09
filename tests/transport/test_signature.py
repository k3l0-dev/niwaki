"""Signing a request with a certificate key, instead of exchanging a password.

The value is not cryptographic novelty — it is that CI and enterprise
workstations have no password to put in an environment variable, and the
fabric's audit trail names a certificate rather than a shared account.

What these tests guard is the part that is easy to get wrong and impossible to
debug from the controller's answer: *what exactly gets signed*. An APIC that
disagrees with the signed string returns a flat rejection, naming nothing.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from niwaki import exceptions
from niwaki.transport._signature import (
    load_private_key,
    sign,
    signature_cookies,
    signature_payload,
)

cryptography = pytest.importorskip("cryptography", reason="needs niwaki[x509]")


@pytest.fixture(scope="module")
def key_pem(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway RSA key on disk, generated once for the module."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("x509") / "admin.key"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


class TestWhatGetsSigned:
    """The string itself, written down because the APIC will not tell you."""

    def test_a_read_signs_method_and_path(self) -> None:
        assert signature_payload("GET", "/api/class/fvTenant.json") == (
            "GET/api/class/fvTenant.json"
        )

    def test_a_write_appends_the_body_verbatim(self) -> None:
        body = '{"fvTenant":{"attributes":{"name":"prod"}}}'
        assert signature_payload("POST", "/api/mo/uni.json", body) == (
            f"POST/api/mo/uni.json{body}"
        )

    def test_the_query_string_is_part_of_the_path(self) -> None:
        """Signing the bare path and sending the query is a silent rejection."""
        path = "/api/class/fvBD.json?query-target=subtree"
        assert signature_payload("GET", path).endswith("query-target=subtree")

    def test_the_method_is_upper_cased(self) -> None:
        assert signature_payload("get", "/x") == signature_payload("GET", "/x")

    def test_nothing_separates_the_three_parts(self) -> None:
        """No spaces, no newlines — concatenation, which is easy to over-engineer."""
        assert signature_payload("POST", "/p", "B") == "POST/pB"


class TestSigning:
    def test_the_signature_verifies_against_the_public_key(self, key_pem: Path) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        key = load_private_key(key_pem)
        signature = sign(key, "GET", "/api/class/fvTenant.json")

        key.public_key().verify(
            base64.b64decode(signature),
            b"GET/api/class/fvTenant.json",
            padding.PKCS1v15(),
            hashes.SHA256(),
        )  # raises InvalidSignature if wrong

    def test_it_is_base64_ascii(self, key_pem: Path) -> None:
        signature = sign(load_private_key(key_pem), "GET", "/x")
        assert signature.isascii()
        base64.b64decode(signature, validate=True)

    def test_a_different_body_gives_a_different_signature(self, key_pem: Path) -> None:
        key = load_private_key(key_pem)
        assert sign(key, "POST", "/x", "a") != sign(key, "POST", "/x", "b")

    def test_a_different_path_gives_a_different_signature(self, key_pem: Path) -> None:
        key = load_private_key(key_pem)
        assert sign(key, "GET", "/a") != sign(key, "GET", "/b")

    def test_signing_is_deterministic(self, key_pem: Path) -> None:
        """PKCS1v15, not PSS: the APIC's v1.0 scheme expects the former."""
        key = load_private_key(key_pem)
        assert sign(key, "GET", "/x") == sign(key, "GET", "/x")


class TestTheCookies:
    def test_all_four_are_present(self, key_pem: Path) -> None:
        cookies = signature_cookies(
            load_private_key(key_pem), "uni/userext/user-admin/usercert-c", "GET", "/x"
        )
        assert set(cookies) == {
            "APIC-Certificate-Algorithm",
            "APIC-Certificate-Fingerprint",
            "APIC-Certificate-DN",
            "APIC-Request-Signature",
        }

    def test_the_literals_are_what_the_apic_expects(self, key_pem: Path) -> None:
        """Neither is derived from the key — both are constants the APIC checks."""
        cookies = signature_cookies(load_private_key(key_pem), "dn", "GET", "/x")
        assert cookies["APIC-Certificate-Algorithm"] == "v1.0"
        assert cookies["APIC-Certificate-Fingerprint"] == "fingerprint"

    def test_the_certificate_dn_is_carried_verbatim(self, key_pem: Path) -> None:
        dn = "uni/userext/user-svc-ci/usercert-ci"
        cookies = signature_cookies(load_private_key(key_pem), dn, "GET", "/x")
        assert cookies["APIC-Certificate-DN"] == dn

    def test_the_private_key_never_appears_in_the_cookies(self, key_pem: Path) -> None:
        """A signature, not the secret that made it."""
        key = load_private_key(key_pem)
        cookies = signature_cookies(key, "dn", "POST", "/x", "body")
        blob = "".join(cookies.values())
        assert "PRIVATE KEY" not in blob
        assert key_pem.read_text()[:64] not in blob


class TestLoadingRefusesWhatItCannotUse:
    def test_a_missing_file_names_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(exceptions.LoginError, match="cannot read private key"):
            load_private_key(tmp_path / "absent.key")

    def test_a_file_that_is_not_a_key(self, tmp_path: Path) -> None:
        path = tmp_path / "junk.key"
        path.write_text("this is not a PEM")
        with pytest.raises(exceptions.LoginError, match="not a usable PEM"):
            load_private_key(path)

    def test_an_encrypted_key_without_a_passphrase(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = tmp_path / "locked.key"
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
            )
        )
        with pytest.raises(exceptions.LoginError):
            load_private_key(path)

    def test_an_encrypted_key_with_its_passphrase_loads(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = tmp_path / "locked.key"
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
            )
        )
        assert load_private_key(path, password=b"secret") is not None

    def test_a_non_rsa_key_is_refused_by_name(self, tmp_path: Path) -> None:
        """The APIC's v1.0 scheme is RSA only; failing here beats failing on the wire."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PrivateKey.generate()
        path = tmp_path / "ed.key"
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        with pytest.raises(exceptions.LoginError, match="requires RSA"):
            load_private_key(path)

    def test_the_error_does_not_echo_the_key_material(self, tmp_path: Path) -> None:
        """A failure message must not put the secret in a log."""
        path = tmp_path / "junk.key"
        path.write_text("-----BEGIN PRIVATE KEY-----\nSUPERSECRETMATERIAL\n")
        with pytest.raises(exceptions.LoginError) as excinfo:
            load_private_key(path)
        assert "SUPERSECRETMATERIAL" not in str(excinfo.value)


class TestASignedSessionNeedsNoPassword:
    """The point of the lot: CI has no password to put in an environment.

    Requiring one would make certificate auth impossible in exactly the place
    it is wanted.
    """

    @staticmethod
    def _session(key_pem: Path, **kwargs: object) -> object:
        from niwaki.transport.session import ApicSession

        return ApicSession(
            "https://apic.test",
            "admin",
            None,
            private_key=key_pem,
            cert_dn="uni/userext/user-admin/usercert-c",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_it_constructs_without_a_password_or_the_environment(
        self, key_pem: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APIC_PASSWORD", raising=False)
        assert self._session(key_pem) is not None

    def test_login_makes_no_request_at_all(self, key_pem: Path) -> None:
        """Nothing to trade, nothing to expire: every request proves itself."""
        session = self._session(key_pem)
        session.login()  # type: ignore[attr-defined]  # no mock: a request would fail
        assert session._token_state is None  # type: ignore[attr-defined]

    def test_every_request_carries_its_own_signature(self, key_pem: Path) -> None:
        import httpx

        from niwaki.transport.session import ApicSession

        cookies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            cookies.append(request.headers.get("cookie", ""))
            return httpx.Response(200, json={"totalCount": "0", "imdata": []})

        client = httpx.Client(base_url="https://apic.test", transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(
            client,
            "https://apic.test",
            "admin",
            None,
            private_key=key_pem,
            cert_dn="uni/userext/user-admin/usercert-c",
        )
        session.get("/api/class/fvTenant.json")
        session.get("/api/class/fvBD.json")

        assert len(cookies) == 2
        for jar in cookies:
            assert "APIC-Request-Signature=" in jar
            assert "APIC-Certificate-DN=uni/userext/user-admin/usercert-c" in jar
        # Different paths, so the two signatures must differ.
        assert cookies[0] != cookies[1]

    def test_a_write_signs_the_body_too(self, key_pem: Path) -> None:
        """A signature over the path alone would pass a tampered payload."""
        import httpx

        from niwaki.transport.session import ApicSession

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("cookie", ""))
            return httpx.Response(200, json={"imdata": []})

        client = httpx.Client(base_url="https://apic.test", transport=httpx.MockTransport(handler))
        session = ApicSession.with_client(
            client,
            "https://apic.test",
            "admin",
            None,
            private_key=key_pem,
            cert_dn="uni/userext/user-admin/usercert-c",
        )
        session.post_mo("uni/tn-a", {"fvTenant": {"attributes": {"name": "a"}}})
        session.post_mo("uni/tn-b", {"fvTenant": {"attributes": {"name": "b"}}})

        assert seen[0] != seen[1], "two different bodies produced one signature"


class TestBothHalvesOrNeither:
    @pytest.mark.parametrize(
        "kwargs",
        [{"cert_dn": "uni/x"}, {"private_key": "/tmp/nonexistent.key"}],
        ids=["dn-without-key", "key-without-dn"],
    )
    def test_half_a_configuration_is_refused(self, kwargs: dict[str, str]) -> None:
        """Silently ignoring one half would authenticate by password instead."""
        from niwaki.transport.session import ApicSession

        with pytest.raises(ValueError, match="must be given together"):
            ApicSession("https://apic.test", "admin", "pw", **kwargs)  # type: ignore[arg-type]
