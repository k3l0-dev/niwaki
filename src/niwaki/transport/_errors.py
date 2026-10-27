"""Shared HTTP error helpers for APIC transport layers.

Both :mod:`~niwaki.transport.session` and :mod:`~niwaki.transport.session_async`
call these module-level functions, ensuring a single, authoritative mapping from
APIC HTTP status codes to typed ``NiwakiError`` subclasses.  Adding a new error
case (e.g. 429 Too Many Requests) requires a change in exactly one place.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence
from typing import NamedTuple, Protocol, TypedDict

import httpx

from niwaki import exceptions
from niwaki.types import JsonObject, JsonScalar

# Query-string parameters of an APIC GET: what httpx accepts as primitive
# query values — the JSON scalar set — or a sequence of them (httpx repeats
# the key).  A ``Mapping`` so a caller's ``dict[str, str]`` is accepted as-is.
type QueryParamValue = JsonScalar | Sequence[JsonScalar]
type QueryParams = Mapping[str, QueryParamValue]


class RequestOptions(TypedDict, total=False):
    """The ``httpx`` request options the transport forwards — and nothing else.

    Attributes:
        params: Query-string parameters of a GET.
        json: The JSON body of a POST (an APIC envelope, any mapping).
    """

    params: QueryParams
    json: Mapping[str, object]


class _JsonResponse(Protocol):
    """Anything that answers ``.json()`` and carries ``.text`` — a response."""

    def json(self) -> object: ...

    @property
    def text(self) -> str: ...


def _imdata_attributes(resp: _JsonResponse, *keys: str) -> JsonObject:
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
        data = resp.json()
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    imdata: object = data.get("imdata")
    if not isinstance(imdata, list) or not imdata:
        return {}
    inner: object = imdata[0]
    if not isinstance(inner, Mapping):
        return {}
    for key in keys:
        wrapper: object = inner.get(key)
        if isinstance(wrapper, Mapping):
            attrs: object = wrapper.get("attributes")
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


def json_data(resp: httpx.Response) -> JsonObject:
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


def imdata_items(resp: httpx.Response, data: JsonObject) -> list[JsonObject]:
    """Return the ``imdata`` list of a parsed APIC body, checked item by item.

    Every APIC answer carries its objects as ``{"imdata": [{<class>: {...}},
    ...]}``.  This is the one place that proves the shape instead of assuming
    it: a missing ``imdata`` is an empty answer, anything else that is not a
    list of JSON objects is a body the APIC never sends — a proxy or a broken
    simulator talking — and surfaces as a typed error.

    Args:
        resp: The response *data* was parsed from (its status and body
            prefix go into the error).
        data: The parsed body, as returned by :func:`json_data`.

    Returns:
        The ``imdata`` items, the same list object the body carried.

    Raises:
        APIError: ``imdata`` is present but not a list of JSON objects.

    Example::

        items = imdata_items(resp, json_data(resp))
    """
    items = data.get("imdata", [])
    if not isinstance(items, list):
        raise exceptions.APIError(resp.status_code, f"non-list imdata: {resp.text[:200]!r}")
    objects: list[JsonObject] = []
    for item in items:
        if not isinstance(item, dict):
            raise exceptions.APIError(
                resp.status_code, f"non-object imdata item: {resp.text[:200]!r}"
            )
        objects.append(item)
    return objects


def total_count(resp: httpx.Response, data: JsonObject) -> int:
    """Read the ``totalCount`` of a parsed APIC body, as an integer.

    The APIC sends it as a numeric string (``"42"``); an absent one counts as
    ``0``.  Anything else — an object, a list, a non-numeric string — is a
    body the APIC never sends and surfaces as a typed error.

    Args:
        resp: The response *data* was parsed from (status and body prefix go
            into the error).
        data: The parsed body, as returned by :func:`json_data`.

    Returns:
        The count.

    Raises:
        APIError: ``totalCount`` is present but not a number.

    Example::

        total = total_count(resp, json_data(resp))
    """
    raw = data.get("totalCount", 0)
    if isinstance(raw, str | int | float):
        try:
            return int(raw)
        except ValueError:
            pass
    raise exceptions.APIError(
        resp.status_code, f"malformed totalCount {raw!r}: {resp.text[:200]!r}"
    )


def json_imdata(resp: httpx.Response) -> list[JsonObject]:
    """Parse a checked 2xx response and return its ``imdata`` items.

    Shorthand for ``imdata_items(resp, json_data(resp))`` — the data paths
    that only want the objects, not ``totalCount``.

    Args:
        resp: A response that already passed :func:`raise_for_apic_status`.

    Returns:
        The ``imdata`` items (empty when the body has none).

    Raises:
        APIError: The body is not a JSON object, or its ``imdata`` is not a
            list of JSON objects.
    """
    return imdata_items(resp, json_data(resp))


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


def transport_failure(
    exc: httpx.TransportError, host: str, *, name_failure: bool = False
) -> exceptions.TransportError:
    """Map an httpx transport failure to the typed niwaki error, without raising it.

    The single mapping behind every request the sessions send.  It *returns*
    the error rather than raising it so the caller decides how it is chained:
    an ordinary request raises it ``from`` the httpx error, while the login
    path raises it outside the ``except`` block with no chain at all — an
    httpx error keeps the request it failed on, and an aaaLogin request's
    body is the password.

    Args:
        exc: The httpx failure.
        host: The APIC base URL, named in connection and TLS messages.
        name_failure: Also name the httpx exception class in the message
            (``"(ConnectTimeout)"``).  Set where the chain is severed: the
            class name is then the only record of which failure it was.

    Returns:
        :class:`~niwaki.exceptions.TimeoutError` for an
        ``httpx.TimeoutException``; :class:`~niwaki.exceptions.TLSError` for an
        ``httpx.ConnectError`` caused by an ``ssl.SSLError`` (the message
        carries the SSL error's text); :class:`~niwaki.exceptions.ConnectionError`
        for any other ``httpx.ConnectError``; the base
        :class:`~niwaki.exceptions.TransportError` for anything else.

    Example::

        try:
            resp = client.get(path)
        except httpx.TransportError as exc:
            raise transport_failure(exc, host) from exc
    """
    kind = f" ({type(exc).__name__})" if name_failure else ""
    match exc:
        case httpx.TimeoutException():
            return exceptions.TimeoutError(f"APIC request timed out{kind}: {exc}")
        case httpx.ConnectError():
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, ssl.SSLError):
                return exceptions.TLSError(f"TLS/SSL error connecting to {host}{kind}: {cause}")
            return exceptions.ConnectionError(f"Cannot reach APIC at {host}{kind}: {exc}")
        case _:
            return exceptions.TransportError(f"{type(exc).__name__}: {exc}" if kind else str(exc))
