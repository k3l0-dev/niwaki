"""Authentication exceptions for the niwaki SDK."""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class AuthError(NiwakiError):
    """
    Base class for APIC authentication errors.

    Subclasses cover login failure, token refresh failure,
    and full session expiry.
    """


class LoginError(AuthError):
    """
    The APIC rejected the credentials during login.

    Raised when POST ``/api/aaaLogin.json`` returns a non-200 status
    or the response contains an APIC error message (wrong password,
    locked account, etc.).

    Attributes:
        args[0]: Error message including the HTTP status and APIC text.
    """


class TokenRefreshError(AuthError):
    """
    Token refresh via ``/api/aaaRefresh.json`` failed.

    The session will automatically attempt a full re-login as fallback.
    This exception should not reach the caller in the normal case;
    it surfaces only if the fallback itself is disabled or fails.
    """


class SessionExpiredError(AuthError):
    """
    The session is fully expired: both refresh and re-login failed.

    Raised when the token has expired and no renewal attempt succeeded.
    The caller must create a new ``ApicSession``.

    Attributes:
        args[0]: Message describing the reason for expiry.
    """
