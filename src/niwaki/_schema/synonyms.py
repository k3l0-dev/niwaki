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
#: Consumed by the extraction pipeline (``data/scripts/02_extract_props.py``),
#: which bakes the alias into the generated enum member — the runtime never
#: needs a lookup of its own.
ENUM_SYNONYMS: Final[dict[str, dict[str, str]]] = {
    "pol:Color": {"cyan": "aqua", "magenta": "fuchsia"},
    "health:ColorT": {"cyan": "aqua", "magenta": "fuchsia"},
}
