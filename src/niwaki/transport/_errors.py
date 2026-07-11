"""Shared HTTP error helpers for APIC transport layers.

Both :mod:`~niwaki.transport.session` and :mod:`~niwaki.transport.session_async`
call these module-level functions, ensuring a single, authoritative mapping from
APIC HTTP status codes to typed ``NiwakiError`` subclasses.  Adding a new error
case (e.g. 429 Too Many Requests) requires a change in exactly one place.
"""

from __future__ import annotations

from typing import Any

import httpx

from niwaki import exceptions


def extract_apic_error(resp: httpx.Response) -> str:
    """Extract a human-readable error message from an APIC error response.

    Standard APIC error format::

        {"imdata": [{"error": {"attributes": {"code": "401", "text": "..."}}}]}

    Args:
        resp: The httpx error response.

    Returns:
        The APIC ``error.attributes.text`` value when the standard format is
        present, otherwise the first 200 characters of the raw response body.

    Example::

        msg = extract_apic_error(resp)
        raise exceptions.LoginError(f"Login failed: {msg}")
    """
    try:
        data: dict[str, Any] = resp.json()
        return str(data["imdata"][0]["error"]["attributes"]["text"])
    except (KeyError, IndexError, ValueError, TypeError):
        return resp.text[:200]


def raise_for_apic_status(resp: httpx.Response) -> None:
    """Raise a typed niwaki exception for any non-2xx APIC HTTP response.

    Attempts to extract the APIC error message via :func:`extract_apic_error`
    before raising.  Called after every request that may carry an error
    response (i.e. everything except login and token refresh, which have their
    own specialised checks).

    Args:
        resp: The httpx response to inspect.  Returns immediately when the
            response is successful (``resp.is_success``).

    Raises:
        UnauthorizedError: HTTP 401.
        ForbiddenError: HTTP 403.
        NotFoundError: HTTP 404.
        ServerError: HTTP 5xx.
        APIError: Any other non-2xx status.

    Example::

        resp = client.get(path, params=params)
        raise_for_apic_status(resp)   # raises on 4xx/5xx, no-op on 2xx
    """
    if resp.is_success:
        return

    msg = extract_apic_error(resp)
    status = resp.status_code
    if status == 401:
        raise exceptions.UnauthorizedError(status, msg)
    if status == 403:
        raise exceptions.ForbiddenError(status, msg)
    if status == 404:
        raise exceptions.NotFoundError(status, msg)
    if status >= 500:
        raise exceptions.ServerError(status, msg)
    raise exceptions.APIError(status, msg)
