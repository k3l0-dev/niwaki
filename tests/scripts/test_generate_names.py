"""Pins on the model generator's naming plumbing."""

from __future__ import annotations

from niwaki._codegen.generate import _aci_to_dot_class


def test_aci_to_dot_class_digit_split_is_pinned() -> None:
    """The digit-split bug is DELIBERATE 2.0 debt — do not fix in a minor.

    ``^([a-z]+)`` cannot cross a digit, so every digit-containing package
    (l3ext, l2ext, ipv4, ...) mis-splits and the scopemeta lookup misses:
    35 scopemeta classes are invisible to the model generator.  Fixing it
    RENAMES generated model fields (breaking) — the catalogue compensates via
    its name_override table, frozen from the models' actually-emitted names.
    """
    assert _aci_to_dot_class("l3extOut") == "l.3extOut"  # the bug, pinned
    assert _aci_to_dot_class("fvBD") == "fv.BD"  # digit-free names split right
