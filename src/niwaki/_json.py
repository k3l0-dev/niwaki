"""Raw-JSON plumbing shared by the SDK — the aliases and the runtime narrowing.

The aliases are defined (and documented) in the public :mod:`niwaki.types`;
they are re-exported here as the very same objects, so internal modules and
user code name one type, never two lookalikes.

The helpers turn an ``object`` read from the wire — or handed in by a caller
through a ``Mapping[str, object]`` parameter — into a typed view, or ``None``
when the value does not have the expected shape.  Each caller decides what a
wrong shape means for it (a typed error, a collected import problem, a skip).
They are the single place where a runtime ``isinstance`` proof becomes a
static type.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from niwaki.types import JsonObject, JsonScalar, JsonValue

__all__ = ["JsonObject", "JsonScalar", "JsonValue", "as_list", "as_mapping"]


def as_mapping(value: object) -> Mapping[str, object] | None:
    """Return *value* as a read-only string-keyed mapping, or ``None``.

    Args:
        value: Anything — a parsed JSON value, a caller's argument.

    Returns:
        *value* itself when it is a :class:`~collections.abc.Mapping` (no
        copy), ``None`` otherwise.  Keys are not checked: JSON object keys are
        strings by construction.

    Example:
        >>> as_mapping({"name": "web"})
        {'name': 'web'}
        >>> as_mapping(["name"]) is None
        True
    """
    if isinstance(value, Mapping):
        mapping: Mapping[str, object] = value
        return mapping
    return None


def as_list(value: object) -> Sequence[object] | None:
    """Return *value* as a read-only sequence when it is a JSON array, or ``None``.

    Only a ``list`` or ``tuple`` qualifies: a ``str`` is a sequence too, but
    never a JSON array.

    Args:
        value: Anything — a parsed JSON value, a caller's argument.

    Returns:
        *value* itself (no copy), or ``None``.

    Example:
        >>> as_list([1, 2])
        [1, 2]
        >>> as_list("12") is None
        True
    """
    if isinstance(value, list | tuple):
        items: Sequence[object] = value
        return items
    return None
