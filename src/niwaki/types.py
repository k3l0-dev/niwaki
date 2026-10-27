"""Type aliases for raw APIC JSON.

Everything the APIC sends and receives is JSON: an object envelope
(``{"fvTenant": {"attributes": {...}, "children": [...]}}``), the ``imdata``
list of a query response, a ``polUni`` payload, a snapshot tree.  This module
gives that shape a name, so code handling raw payloads stays type-checked
instead of falling back to ``dict[str, Any]``.

Three aliases, all PEP 695 ``type`` statements (lazily evaluated, so the
recursion in ``JsonValue`` needs no forward-reference string, and pydantic
follows it when one is used as a field or ``TypeAdapter`` type):

- ``JsonScalar`` — a JSON leaf: ``str | int | float | bool | None``.
- ``JsonValue`` — any JSON document: a scalar, a ``list`` of values or a
  string-keyed ``dict`` of values.
- ``JsonObject`` — a JSON object, ``dict[str, JsonValue]``: the shape of every
  APIC envelope and response body.

How the SDK uses them:

- **What the SDK returns is exact.**  :meth:`ManagedObject.to_apic
  <niwaki.models.ManagedObject.to_apic>`, :meth:`Cursor.to_payload
  <niwaki.design.Cursor.to_payload>` and :attr:`Snapshot.tree
  <niwaki.snapshot.Snapshot.tree>` are a ``JsonObject``;
  :meth:`ApicSession.get <niwaki.transport.ApicSession.get>` returns a
  ``list[JsonObject]``.  A value read out of one is a ``JsonValue``, so
  narrowing it (``isinstance(value, dict)``) is what proves — to the type
  checker and to the reader — what the wire actually carried.
- **What the SDK reads is permissive.**  Functions that consume an envelope
  (``from_apic``, ``parse_imdata``, ``from_payload``, every ``post_mo``) take
  any ``Mapping[str, object]`` and check the shape at runtime, so an
  unannotated literal or a ``dict[str, dict[str, str]]`` is accepted as is —
  ``dict`` is invariant, so a ``JsonObject`` parameter would reject both.

Example — walk a response with the type checker's help::

    from niwaki.types import JsonObject

    def tenant_names(body: JsonObject) -> list[str]:
        names: list[str] = []
        items = body.get("imdata", [])
        if not isinstance(items, list):
            return names
        for item in items:
            if not isinstance(item, dict):
                continue
            tenant = item.get("fvTenant")
            if isinstance(tenant, dict):
                attributes = tenant.get("attributes")
                if isinstance(attributes, dict):
                    name = attributes.get("name")
                    if isinstance(name, str):
                        names.append(name)
        return names

Example — annotate a payload you build, so it can travel as a ``JsonValue``
inside another one::

    from niwaki.types import JsonObject

    bd: JsonObject = {"fvBD": {"attributes": {"name": "web"}}}
    tenant: JsonObject = {"fvTenant": {"attributes": {"name": "prod"}, "children": [bd]}}
"""

from __future__ import annotations

__all__ = ["JsonObject", "JsonScalar", "JsonValue"]

# A JSON leaf.  ``bool`` is listed explicitly even though it subclasses
# ``int``: a strict validator (pydantic strict mode) keeps them apart.
type JsonScalar = str | int | float | bool | None
"""A JSON leaf — ``str | int | float | bool | None``."""

# Any JSON document: a scalar, an array of values, or a string-keyed object of
# values.  Recursive — the alias refers to itself through ``list``/``dict``.
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
"""Any JSON document — ``JsonScalar | list[JsonValue] | dict[str, JsonValue]``."""

# A JSON object: what the APIC wraps every managed object, response and
# payload in.  A ``dict`` (not a ``Mapping``) because it is what the SDK
# produces: the caller owns it and may mutate it.
type JsonObject = dict[str, JsonValue]
"""A JSON object, ``dict[str, JsonValue]`` — every APIC envelope and body."""
