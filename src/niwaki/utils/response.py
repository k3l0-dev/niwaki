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

from typing import Any

from niwaki.exceptions import DeserializationError
from niwaki.models.base import ManagedObject


def parse_imdata(data: dict[str, Any]) -> list[ManagedObject]:
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
        :exc:`niwaki.exceptions.DeserializationError`: When any item in
            ``imdata`` cannot be deserialised.

    Example::

        raw = {"totalCount": "2", "imdata": [
            {"fvBD": {"attributes": {"name": "web"}}},
            {"fvBD": {"attributes": {"name": "db"}}},
        ]}
        bds = parse_imdata(raw)
        # → [fvBD(name="web"), fvBD(name="db")]
    """
    items: list[ManagedObject] = []
    for raw_item in data.get("imdata", []):
        if "error" in raw_item:
            continue
        try:
            items.append(ManagedObject.from_apic(raw_item))
        except (StopIteration, KeyError, ValueError, TypeError, AttributeError) as exc:
            raise DeserializationError(f"Failed to deserialise APIC response item: {exc}") from exc
    return items
