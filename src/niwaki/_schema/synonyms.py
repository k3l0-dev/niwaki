"""Enum values the APIC accepts under two names but stores under one.

A handful of ACI enums list two spellings for the same underlying value — the
schema gives them the same numeric code.  ``pol:Color`` accepts both ``cyan``
and ``aqua`` for ``0x00FFFF``, and both ``magenta`` and ``fuchsia`` for
``0xFF00FF``; they are the X11 colour pairs, and CSS treats them the same way.

The APIC accepts either spelling on write and answers with **one** of them.
Left alone, that makes a design permanently disagree with the fabric it just
configured: a contract label declared ``magenta`` reads back ``fuchsia``, and
every subsequent ``mode="plan"`` reports a change that does not exist.

So the non-canonical spelling is not a member of the generated enum — it is an
alias that coerces to the one the APIC stores, exactly as the numeric codes do.
Writing ``magenta`` keeps working; the model simply holds what the fabric
holds.

Curated by hand, deliberately.  These pairs cannot be derived: nothing in the
schema says which spelling the controller elects, and the two below are the
ones observed on a live 6.0(9c) fabric.  Everything else that shares a numeric
code in the schemas is either an internal registry (``mo:PropId``,
``fsm:Flags``) or a distinct concept the APIC does not canonicalise.
"""

from __future__ import annotations

from typing import Final

#: ``modelType`` → ``{spelling the APIC discards: spelling the APIC stores}``.
ENUM_SYNONYMS: Final[dict[str, dict[str, str]]] = {
    "pol:Color": {"cyan": "aqua", "magenta": "fuchsia"},
    "health:ColorT": {"cyan": "aqua", "magenta": "fuchsia"},
}


def canonical_value(model_type: str, value: str) -> str:
    """The spelling the APIC stores for *value*.

    Args:
        model_type: The schema ``modelType`` of the property, e.g.
            ``"pol:Color"``.
        value: A value the schema lists for that type.

    Returns:
        The canonical spelling, or *value* unchanged when the type has no
        synonyms or the value is already canonical.

    Example::

        canonical_value("pol:Color", "magenta")   # "fuchsia"
        canonical_value("pol:Color", "aqua")      # "aqua"
        canonical_value("fv:RtctrlDir", "magenta")  # "magenta" — not a colour
    """
    return ENUM_SYNONYMS.get(model_type, {}).get(value, value)
