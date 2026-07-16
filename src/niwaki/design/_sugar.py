"""Parameter sugar — operator vocabulary translated to APIC attributes.

Per the design rule ("structure is literal, verbatim is translated"), sugar
never creates or hides objects — it only rewrites *attribute* keyword
arguments of the current object into their real schema fields:

- ``vzEntry``: ``tcp=80`` / ``udp=(5000, 5010)`` expand to ``ethernet_type``,
  ``protocol`` and destination port range fields; ``protocol="icmp"`` implies
  ``ethernet_type="ip"``.
- ``vzBrCP``: ``scope="vrf"`` maps to the APIC enum value ``"context"``
  (operators say "VRF", the schema says "context").
"""

from __future__ import annotations

from typing import Any

from niwaki.exceptions._design import DesignError

_PORT_RANGE_KEYS = ("tcp", "udp")


def _port_range(value: Any) -> tuple[int | str, int | str]:
    """Normalise a port spec into ``(from_port, to_port)``.

    Accepts an int (``80``), a 2-tuple/list (``(8000, 8090)``), a dashed
    string (``"8000-8090"``), or a named string (``"http"``).

    The value is passed through as it was written — the model owns the rest.  A
    port with a name is stored under it (``80`` becomes ``"http"``, which is what
    the APIC stores), and a port without one stays a number.
    """
    match value:
        case int():
            return value, value
        case (int() | str() as lo, int() | str() as hi):
            return lo, hi
        case str() if "-" in value:
            lo, _, hi = value.partition("-")
            return lo.strip(), hi.strip()
        case str():
            return value, value
        case _:
            raise DesignError(f"Unsupported port specification: {value!r}")


def apply_sugar(aci_class: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Rewrite sugared keyword arguments into real schema fields.

    Args:
        aci_class: ACI class of the object being configured.
        attrs: Raw keyword arguments from a maker or ``set()`` call.

    Returns:
        A new dict with sugar keys replaced by schema field names.  Unknown
        keys pass through untouched (Pydantic validation catches typos).

    Raises:
        DesignError: ``tcp=`` and ``udp=`` used together on the same entry.
    """
    if aci_class == "vzEntry":
        return _entry_sugar(attrs)
    if aci_class == "vzBrCP" and attrs.get("scope") == "vrf":
        return {**attrs, "scope": "context"}
    return attrs


def _entry_sugar(attrs: dict[str, Any]) -> dict[str, Any]:
    """Expand ``tcp=`` / ``udp=`` / ``protocol="icmp"`` on a ``vzEntry``.

    The defaulted ``ethernet_type="ip"`` is the *generic* IP ether-type: it
    matches both IPv4 and IPv6 traffic, so a ``tcp=``/``udp=`` entry needs no
    extra work for IPv6.  Pass ``ethernet_type="ipv4"`` or ``"ipv6"`` explicitly
    to restrict to one family (the sugar uses ``setdefault``, so it is honoured).
    ``protocol="icmpv6"`` defaults to ``ethernet_type="ipv6"`` since ICMPv6 only
    exists over IPv6.
    """
    given = [k for k in _PORT_RANGE_KEYS if k in attrs]
    if len(given) > 1:
        raise DesignError("Filter entry accepts either tcp= or udp=, not both.")

    out = dict(attrs)
    if given:
        proto = given[0]
        lo, hi = _port_range(out.pop(proto))
        out.setdefault("ethernet_type", "ip")
        out.setdefault("protocol", proto)
        out.setdefault("destination_from_port", lo)
        out.setdefault("destination_to_port", hi)
    elif out.get("protocol") == "icmp":
        out.setdefault("ethernet_type", "ip")
    elif out.get("protocol") == "icmpv6":
        out.setdefault("ethernet_type", "ipv6")
    return out
