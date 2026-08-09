"""Signing APIC requests with an X.509 private key, instead of a password.

The APIC accepts a second form of authentication: rather than exchanging a
password for a session token, each request carries a signature made with the
private key of a certificate the fabric already knows.  That is what makes the
SDK usable from CI and from an enterprise workstation — there is no password to
put in an environment variable, no token to keep alive, and the fabric's audit
trail names a certificate rather than a shared account.

The signature covers the request *line* — method, path and body — not the
headers, so it is computed here and shipped in four cookies the controller
reads back:

===========================  =================================================
``APIC-Certificate-Algorithm``  always ``v1.0``, the only scheme the APIC has
``APIC-Certificate-Fingerprint``  always ``fingerprint``, a literal the APIC expects
``APIC-Certificate-DN``       the DN of the ``aaaUserCert`` object on the fabric
``APIC-Request-Signature``    base64 of the RSA-SHA256 signature
===========================  =================================================

The APIC validates the certificate's ``notBefore``/``notAfter`` window against
its own clock, as any X.509 validator does — so a lab controller whose clock
has drifted will refuse a certificate minted a moment ago.  Nothing to work
around in production, where NTP settles it; worth knowing when generating a
throwaway certificate against a simulator.

``cryptography`` is an optional dependency (``pip install niwaki[x509]``): it is
a compiled package with its own release cadence, and most callers authenticate
with a password.  Importing this module without it raises immediately, naming
the extra, rather than failing later at the first request.
"""

# Because the extra is optional, `cryptography` is genuinely absent from an
# install that did not ask for it, and pyright cannot resolve these imports
# there.  The suppression is the whole file rather than each import because
# pyright has no per-module setting (mypy's equivalent lives in pyproject.toml,
# scoped to `cryptography.*`) and every per-import form overruns the line
# budget.  It costs little: the only other imports here are httpx and
# niwaki.exceptions, both mandatory, both still checked by mypy.
# pyright: reportMissingImports=false

from __future__ import annotations

import base64
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from niwaki import exceptions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

_ALGORITHM = "v1.0"
_FINGERPRINT = "fingerprint"


def _require_cryptography() -> Any:
    """Import ``cryptography``, or explain which extra provides it.

    Returns:
        The ``cryptography.hazmat.primitives`` module namespace.

    Raises:
        MissingDependencyError: The extra is not installed.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by a skip test
        raise exceptions.MissingDependencyError(
            "certificate authentication needs the 'cryptography' package: install niwaki[x509]"
        ) from exc
    return hashes, serialization, padding


def load_private_key(path: str | Path, password: bytes | None = None) -> RSAPrivateKey:
    """Load an RSA private key from a PEM file.

    Args:
        path: Path to the PEM file holding the key that matches the
            ``aaaUserCert`` registered on the fabric.
        password: Passphrase, when the key is encrypted, else ``None``.

    Returns:
        The loaded key, ready to sign.

    Raises:
        MissingDependencyError: ``cryptography`` is not installed.
        LoginError: The file is missing, unreadable, not a private key, is
            encrypted and no passphrase was given, or is not RSA — the APIC
            accepts nothing else under the ``v1.0`` scheme.

    Example::

        key = load_private_key("/etc/niwaki/admin.key")
    """
    _, serialization, _ = _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey as _RSAPrivateKey

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise exceptions.LoginError(f"cannot read private key at {path}: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(raw, password=password)
    except (ValueError, TypeError) as exc:
        raise exceptions.LoginError(f"not a usable PEM private key at {path}: {exc}") from exc
    if not isinstance(key, _RSAPrivateKey):
        raise exceptions.LoginError(
            f"key at {path} is {type(key).__name__}; the APIC's v1.0 scheme requires RSA"
        )
    return key


def signature_payload(method: str, path: str, body: str = "") -> str:
    """The exact string the APIC signs — method, path, then body.

    Written down because getting it wrong produces a signature the controller
    rejects with no explanation of which part disagreed.  The path is the
    request path *including* its query string, as sent; the body is the raw
    JSON for a write and empty for a read.

    Args:
        method: HTTP method, upper-case.
        path: Request path with query string, e.g. ``/api/mo/uni.json``.
        body: Raw request body, or ``""``.

    Returns:
        The string to sign.

    Example::

        signature_payload("GET", "/api/class/fvTenant.json")
    """
    return f"{method.upper()}{path}{body}"


def sign(key: RSAPrivateKey, method: str, path: str, body: str = "") -> str:
    """Sign one request, returning the base64 signature the APIC expects.

    Args:
        key: The RSA private key from :func:`load_private_key`.
        method: HTTP method.
        path: Request path with query string.
        body: Raw request body, or ``""`` for a read.

    Returns:
        Base64-encoded RSA-SHA256 signature, ASCII.

    Example::

        sig = sign(key, "GET", "/api/class/fvTenant.json")
    """
    hashes, _, padding = _require_cryptography()
    payload = signature_payload(method, path, body).encode("utf-8")
    raw = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode("ascii")


def signature_cookies(
    key: RSAPrivateKey, cert_dn: str, method: str, path: str, body: str = ""
) -> dict[str, str]:
    """The four cookies that authenticate one signed request.

    Args:
        key: The RSA private key.
        cert_dn: DN of the ``aaaUserCert`` object on the fabric, e.g.
            ``uni/userext/user-admin/usercert-admin-cert``.
        method: HTTP method.
        path: Request path with query string.
        body: Raw request body, or ``""``.

    Returns:
        A mapping of cookie name to value, ready to attach to a request.  The
        algorithm and fingerprint values are literals the APIC requires rather
        than anything derived from the key.

    Example::

        cookies = signature_cookies(key, cert_dn, "GET", "/api/class/fvBD.json")
    """
    return {
        "APIC-Certificate-Algorithm": _ALGORITHM,
        "APIC-Certificate-Fingerprint": _FINGERPRINT,
        "APIC-Certificate-DN": cert_dn,
        "APIC-Request-Signature": sign(key, method, path, body),
    }


class CertificateAuth(httpx.Auth):
    """Signs every outgoing request with the certificate's private key.

    Implemented as an ``httpx.Auth`` rather than by patching each call site,
    because the signature must cover the request *as sent* — the final path
    with its query string, and the body httpx actually serialised. Anything
    computed earlier can drift from what goes on the wire, and the APIC's
    rejection names nothing.

    Attributes:
        cert_dn: DN of the ``aaaUserCert`` object registered on the fabric.

    Example::

        client = httpx.Client(base_url=host, auth=CertificateAuth(key, cert_dn))
    """

    def __init__(self, key: RSAPrivateKey, cert_dn: str) -> None:
        self._key = key
        self.cert_dn = cert_dn

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Attach the four signature cookies, then send the request once.

        Args:
            request: The outgoing request, already serialised.

        Yields:
            The same request, carrying its signature.
        """
        path = request.url.raw_path.decode("ascii")
        body = request.content.decode("utf-8") if request.content else ""
        for name, value in signature_cookies(
            self._key, self.cert_dn, request.method, path, body
        ).items():
            request.headers["Cookie"] = (
                f"{request.headers['Cookie']}; {name}={value}"
                if "Cookie" in request.headers
                else f"{name}={value}"
            )
        yield request
