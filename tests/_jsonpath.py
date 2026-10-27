"""Typed access into raw APIC JSON for test assertions.

The SDK types every raw payload as :data:`niwaki._json.JsonObject` — a
``dict[str, JsonValue]`` whose values can be any JSON value.  Indexing a
:data:`~niwaki._json.JsonValue` therefore needs a proof of its shape first,
which is exactly what a test wants anyway: a payload that is not shaped as
expected must fail the test loudly, at the step that is wrong.

These helpers walk and narrow, asserting each step::

    attrs = as_object(at(mo.to_apic(), "fvBD", "attributes"))
    assert attrs["name"] == "web"
"""

from __future__ import annotations

from niwaki._json import JsonObject, JsonValue


def as_object(value: JsonValue) -> JsonObject:
    """Assert *value* is a JSON object and return it, typed."""
    assert isinstance(value, dict), f"expected a JSON object, got {type(value).__name__}"
    return value


def as_array(value: JsonValue) -> list[JsonValue]:
    """Assert *value* is a JSON array and return it, typed."""
    assert isinstance(value, list), f"expected a JSON array, got {type(value).__name__}"
    return value


def as_objects(value: JsonValue) -> list[JsonObject]:
    """Assert *value* is a JSON array of objects and return it, typed."""
    return [as_object(item) for item in as_array(value)]


def at(value: JsonValue, *path: str | int) -> JsonValue:
    """Walk *value* along *path* — a string key steps into an object, an int into an array.

    Args:
        value: The document to walk.
        *path: Keys and indices, outermost first.

    Returns:
        The value at the end of the path.

    Raises:
        AssertionError: A step meets the wrong JSON type.
        KeyError: An object step names a missing key.
        IndexError: An array step is out of range.
    """
    for step in path:
        value = as_object(value)[step] if isinstance(step, str) else as_array(value)[step]
    return value
