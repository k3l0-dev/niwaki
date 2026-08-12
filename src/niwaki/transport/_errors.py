"""Shared HTTP error helpers for APIC transport layers.

Both :mod:`~niwaki.transport.session` and :mod:`~niwaki.transport.session_async`
call these module-level functions, ensuring a single, authoritative mapping from
APIC HTTP status codes to typed ``NiwakiError`` subclasses.  Adding a new error
case (e.g. 429 Too Many Requests) requires a change in exactly one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple, Protocol

import httpx

from niwaki import exceptions


class _JsonResponse(Protocol):
    """Anything that answers ``.json()`` and carries ``.text`` — a response."""

    def json(self) -> Any: ...

    @property
    def text(self) -> str: ...


def _imdata_attributes(resp: _JsonResponse, *keys: str) -> dict[str, Any]:
    """Return ``imdata[0].<key>.attributes`` for the first *keys* entry present.

    Every APIC response — success, error, login, refresh — wraps its payload the
    same way::

        {"totalCount": "1", "imdata": [{"<key>": {"attributes": {...}}}]}

    Several routines in the transport layer need that descent, and every one of
    them has to survive a body that does not have that shape: a simulator under
    load answers an nginx HTML page, a successful write answers an empty body,
    and ``/api/aaaRefresh.json`` answers inside an ``aaaLogin`` envelope.  This
    is the only place that knows how to walk it.

    Args:
        resp: Any object with ``.json()`` and ``.text`` — an
            :class:`httpx.Response`, or a test double.  ``.json()`` is allowed
            to raise.
        keys: Wrapper keys to try, in order.  The first key whose value is a
            mapping carrying a mapping ``"attributes"`` wins, which is what lets
            ``_imdata_attributes(resp, "aaaLogin", "aaaRefresh")`` accept either
            envelope from the refresh endpoint.

    Returns:
        A copy of the attributes mapping, or an **empty dict** when the body is
        not JSON, has no ``imdata``, has an empty ``imdata``, has an ``imdata``
        that is not a list, has a first element that is not a mapping, carries
        none of *keys*, or carries a ``null``/non-mapping ``attributes``.  It
        never raises and never returns ``None``, so callers read it with
        ``.get()`` and choose their own fallback.

    Example::

        attrs = _imdata_attributes(resp, "error")
        code = attrs.get("code")   # None when the body was not an APIC error
    """
    try:
        data: Any = resp.json()
        inner: Any = data["imdata"][0]
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    if not isinstance(inner, Mapping):
        return {}
    for key in keys:
        wrapper: Any = inner.get(key)
        if isinstance(wrapper, Mapping):
            attrs: Any = wrapper.get("attributes")
            if isinstance(attrs, Mapping):
                return dict(attrs)
    return {}


class ApicErrorFields(NamedTuple):
    """The two fields an APIC error envelope carries.

    Attributes:
        message: ``error.attributes.text``, or the first 200 characters of the
            raw body when the response is not an APIC error envelope.
        code: ``error.attributes.code`` verbatim, or ``None`` when absent.  The
            two fall back independently: a body may carry a code and no text.
    """

    message: str
    code: str | None


def apic_error_fields(resp: httpx.Response) -> ApicErrorFields:
    """Extract both fields of an APIC error response in one pass.

    Reads the body once.  :func:`extract_apic_error` is the message-only view of
    this function, kept because the auth paths want nothing else.

    Args:
        resp: The httpx error response.

    Returns:
        An :class:`ApicErrorFields` — never raises, whatever the body contains.

    Example::

        message, code = apic_error_fields(resp)
        raise exceptions.APIError(resp.status_code, message, apic_code=code)
    """
    attrs = _imdata_attributes(resp, "error")
    text = attrs.get("text")
    code = attrs.get("code")
    return ApicErrorFields(
        message=str(text) if text is not None else resp.text[:200],
        code=str(code) if code is not None else None,
    )


def extract_apic_error(resp: httpx.Response) -> str:
    """Extract a human-readable error message from an APIC error response.

    Standard APIC error format::

        {"imdata": [{"error": {"attributes": {"code": "401", "text": "..."}}}]}

    Args:
        resp: The httpx error response.

    Returns:
        The APIC ``error.attributes.text`` value when the standard format is
        present, otherwise the first 200 characters of the raw response body.

    See Also:
        :func:`apic_error_fields` — the same extraction, keeping the APIC's own
        error code alongside the message.

    Example::

        msg = extract_apic_error(resp)
        raise exceptions.LoginError(f"Login failed: {msg}")
    """
    return apic_error_fields(resp).message


def json_data(resp: httpx.Response) -> dict[str, Any]:
    """Parse a checked 2xx response body as the APIC JSON envelope.

    A 2xx whose body is not JSON — the nginx HTML page a simulator under load
    serves, a proxy interposing its own error page — must surface as a typed
    error, never a bare ``json.JSONDecodeError``: the session promises that
    only :class:`~niwaki.exceptions.NiwakiError` subclasses propagate.  The
    auth paths already survive this shape through ``_imdata_attributes``; this
    is the same hardening for the data paths.

    Args:
        resp: A response that already passed :func:`raise_for_apic_status`.

    Returns:
        The parsed JSON object.

    Raises:
        APIError: The body is not valid JSON, or not a JSON object.  Carries
            the *real* HTTP status and the first 200 characters of the body.
    """
    try:
        data = resp.json()
    except ValueError as exc:
        raise exceptions.APIError(resp.status_code, f"non-JSON body: {resp.text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise exceptions.APIError(resp.status_code, f"non-object JSON body: {resp.text[:200]!r}")
    return data


def raise_for_apic_status(resp: httpx.Response) -> None:
    """Raise a typed niwaki exception for any non-2xx APIC HTTP response.

    Attempts to extract the APIC error message and the APIC's own error code via
    :func:`apic_error_fields` before raising.  Called after every request that
    may carry an error response (i.e. everything except login and token refresh,
    which have their own specialised checks).

    The choice of exception stays keyed on the **HTTP status** alone.  The APIC
    code rides along on every one of them as ``apic_code``, because it is a
    cause discriminator and not a transience one: on 6.0(9c) many distinct codes
    share HTTP 400, so promoting one of them to a status-named type — a code-102
    "parent not found" into a :class:`NotFoundError`, say — would make the type
    lie about the status.

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

    msg, code = apic_error_fields(resp)
    status = resp.status_code
    if status == 401:
        raise exceptions.UnauthorizedError(status, msg, apic_code=code)
    if status == 403:
        raise exceptions.ForbiddenError(status, msg, apic_code=code)
    if status == 404:
        raise exceptions.NotFoundError(status, msg, apic_code=code)
    if status >= 500:
        raise exceptions.ServerError(status, msg, apic_code=code)
    raise exceptions.APIError(status, msg, apic_code=code)
