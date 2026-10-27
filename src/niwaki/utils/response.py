"""APIC REST response parsing utilities.

APIC responses always have the envelope::

    {
        "totalCount": "42",
        "imdata": [
            {"fvBD": {"attributes": {...}, "children": [...]}},
            ...
        ]
    }

:func:`parse_imdata` consumes that envelope and returns typed ManagedObjects.
HTTP-level error extraction lives in ``niwaki.transport._errors``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from niwaki._json import as_list, as_mapping
from niwaki.exceptions import DeserializationError
from niwaki.models.base import ManagedObject


def parse_imdata(data: Mapping[str, object]) -> list[ManagedObject]:
    """Deserialise all objects in the ``imdata`` array into typed ManagedObjects.

    Uses :meth:`~niwaki.models.ManagedObject.from_apic` for each item, which dispatches to the
    correct generated subclass via ``REGISTRY``.  Error entries (items keyed
    ``"error"``) are skipped — HTTP status handling raises typed exceptions
    before this parser runs.

    Args:
        data: Raw APIC response dict (the top-level JSON object).

    Returns:
        List of :class:`~niwaki.models.ManagedObject` instances (may be empty).

    Raises:
        :exc:`niwaki.exceptions.DeserializationError`: When ``imdata`` is not
            a list of JSON objects, or any item in it cannot be deserialised.

    Example::

        raw = {"totalCount": "2", "imdata": [
            {"fvBD": {"attributes": {"name": "web"}}},
            {"fvBD": {"attributes": {"name": "db"}}},
        ]}
        bds = parse_imdata(raw)
        # → [fvBD(name="web"), fvBD(name="db")]
    """
    raw_items = data.get("imdata", [])
    items = as_list(raw_items)
    if items is None:
        raise DeserializationError(f"imdata is {type(raw_items).__name__}, not a list")
    envelopes: list[Mapping[str, object]] = []
    for raw_item in items:
        envelope = as_mapping(raw_item)
        if envelope is None:
            raise DeserializationError(
                f"Failed to deserialise APIC response item: {type(raw_item).__name__} "
                "is not an object"
            )
        envelopes.append(envelope)
    return parse_envelopes(envelopes)


def parse_envelopes(envelopes: Iterable[Mapping[str, object]]) -> list[ManagedObject]:
    """Deserialise already-unwrapped ``imdata`` items into typed ManagedObjects.

    The body of :func:`parse_imdata`, for callers that hold the item list
    itself (the transport hands back ``imdata``, not the whole response).
    Error entries (items keyed ``"error"``) are skipped.

    Args:
        envelopes: APIC envelopes, one class key each.

    Returns:
        The deserialised objects, in order.

    Raises:
        :exc:`niwaki.exceptions.DeserializationError`: When an item cannot be
            deserialised.

    Example::

        bds = parse_envelopes(session.get("/api/class/fvBD.json"))
    """
    items: list[ManagedObject] = []
    for raw_item in envelopes:
        if "error" in raw_item:
            continue
        try:
            items.append(ManagedObject.from_apic(raw_item))
        except (StopIteration, KeyError, ValueError, TypeError, AttributeError) as exc:
            raise DeserializationError(f"Failed to deserialise APIC response item: {exc}") from exc
    return items
