"""APIC API error exceptions for the niwaki SDK."""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class APIError(NiwakiError):
    """
    The APIC responded with an HTTP error status (4xx or 5xx).

    Attributes:
        status_code: HTTP status code returned by the APIC — or, when the SDK
            raises without one, the status that best describes the failure:
            ``404`` for a DN whose read came back empty, ``0`` when no request
            was made at all.  A non-zero status is therefore *not* proof that
            the controller answered; ``apic_code is None`` is the reliable way
            to tell an SDK-synthesised error from a controller's own.
        apic_message: Error text extracted from the APIC payload, if available;
            otherwise the first 200 characters of the raw body.
        apic_code: The APIC's own error code — ``error.attributes.code`` on the
            wire — as the verbatim string the controller sent (``"103"``,
            ``"801"``), or ``None`` when the response carried no APIC error
            envelope, or the SDK raised without one.

            This is a **cause** discriminator, never a **transience** one: on
            APIC 6.0(9c) many distinct codes all arrive under HTTP 400, and none
            of the measured ones is retryable.  Decide *whether to retry* from
            the exception type; decide *what went wrong* from ``apic_code``.

            The value stays a string rather than being parsed to ``int``, so the
            SDK never fails to report an error because a controller sent a code
            it did not expect.

    Example::

        try:
            aci.node(dn).delete()
        except APIError as exc:
            if exc.apic_code == "107":  # the controller refuses to delete it
                ...
            raise
    """

    def __init__(
        self,
        status_code: int,
        apic_message: str = "",
        *,
        apic_code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.apic_message = apic_message
        self.apic_code = apic_code
        prefix = f"HTTP {status_code}"
        if apic_code is not None:
            prefix = f"{prefix} (APIC code {apic_code})"
        super().__init__(f"{prefix}: {apic_message or '(no APIC message)'}")


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
