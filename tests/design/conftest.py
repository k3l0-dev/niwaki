"""Shared fixtures for the design-DSL test suite."""

from __future__ import annotations

from typing import Any

from niwaki.design import Cursor, tenant


def find_child(envelope: dict[str, Any], aci_class: str, **attrs: str) -> dict[str, Any]:
    """Return the first child envelope of *aci_class* matching *attrs*.

    Raises:
        AssertionError: No child matches.
    """
    parent_cls = next(iter(envelope))
    for child in envelope[parent_cls].get("children", []):
        if aci_class not in child:
            continue
        child_attrs = child[aci_class]["attributes"]
        if all(child_attrs.get(k) == v for k, v in attrs.items()):
            return child
    raise AssertionError(f"No {aci_class} child matching {attrs} in {envelope}")


def mini_design() -> Cursor:
    """Small three-object design used across push tests."""
    return tenant("prod").bd("web").set(unicast_routing=True).bind(vrf="prod").vrf("prod")
