"""APIC API error exceptions for the niwaki SDK."""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class APIError(NiwakiError):
    """
    The APIC responded with an HTTP error status (4xx or 5xx).

    Attributes:
        status_code: HTTP status code returned by the APIC.
        apic_message: Error text extracted from the APIC payload, if available.
    """

    def __init__(self, status_code: int, apic_message: str = "") -> None:
        self.status_code = status_code
        self.apic_message = apic_message
        super().__init__(f"HTTP {status_code}: {apic_message or '(no APIC message)'}")


class UnauthorizedError(APIError):
    """
    The APIC returned HTTP 401 — the session token is invalid or expired server-side.

    Raised only if the 401 persists after automatic re-authentication,
    indicating that the credentials themselves were revoked or that the
    resource is not accessible to this user.
    """


class ForbiddenError(APIError):
    """
    The APIC returned HTTP 403 — the authenticated user lacks sufficient privileges.

    Difference from ``UnauthorizedError``:
    - 401 = not authenticated (invalid token).
    - 403 = authenticated but not authorised on this resource.
    """


class NotFoundError(APIError):
    """
    The APIC returned HTTP 404 — the requested MO does not exist.

    The DN or API path is invalid, or the object has been deleted.
    """


class ServerError(APIError):
    """
    The APIC returned a 5xx error — server-side APIC error.

    These errors are considered transient and may be retried.
    If they persist after all retry attempts, this exception is raised.
    """
