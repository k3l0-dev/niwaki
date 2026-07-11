"""Network transport exceptions for the niwaki SDK."""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class TransportError(NiwakiError):
    """
    Base class for network/transport layer errors.

    All subclasses wrap corresponding ``httpx`` exceptions.
    """


class ConnectionError(TransportError):
    """
    The APIC host is unreachable (DNS failure, TCP refused, interface down, etc.).

    Wraps ``httpx.ConnectError`` in non-TLS cases.

    Note:
        This name intentionally shadows the Python builtin ``ConnectionError``.
        Inside the SDK, use ``niwaki.exceptions.ConnectionError``.
    """


class TimeoutError(TransportError):
    """
    A request to the APIC exceeded the configured timeout.

    Wraps ``httpx.TimeoutException`` (covers connect timeout,
    read timeout, write timeout, and pool timeout).

    Note:
        This name intentionally shadows the Python builtin ``TimeoutError``.
    """


class TLSError(TransportError):
    """
    TLS/SSL error when connecting to the APIC.

    Common causes:
    - Self-signed certificate with ``verify_ssl=True`` (default).
    - Expired certificate or incorrect domain name.
    - Incomplete CA chain.

    Quick fix (not recommended in production): ``verify_ssl=False``.
    """
