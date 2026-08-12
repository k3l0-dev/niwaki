"""Pins on the model generator's naming plumbing."""

from __future__ import annotations

import pytest

from niwaki._codegen.generate import _aci_to_dot_class, _verify_label_corrections


def test_aci_to_dot_class_builds_from_schema_metadata() -> None:
    """The scopemeta key is built from class_pkg + short class name.

    Exactly the construction generate_catalog uses — the two generators
    cannot disagree.  The pre-2.0 regex split (``^([a-z]+)``) could not
    cross a digit, so every digit-bearing package (l3ext, l2ext, ipv4, ...)
    looked up ``"l.3extOut"`` and silently lost its scopemeta labels — the
    root cause of the six frozen catalogue name_override rows.  Fixed in
    2.0 (one breaking rename wave); the override table is empty and must
    stay so.
    """
    assert _aci_to_dot_class("l3ext", "Out") == "l3ext.Out"
    assert _aci_to_dot_class("fv", "BD") == "fv.BD"
    assert _aci_to_dot_class("uribv4", "Route") == "uribv4.Route"


def test_dead_label_correction_fails_the_build() -> None:
    """A LABEL_CORRECTIONS key absent from every schema label raises.

    Dead curation must break the regen loudly — a typo Cisco has fixed means
    the correction entry must be deleted, not carried forever.
    """
    subset = {
        "fvBD": {
            "class": {"label": "Bridge Domain"},
            "properties": {"arpFlood": {"label": "ARP Flooding"}},
        }
    }
    # The real corrections ("maitenance", "availibility") match nothing here.
    with pytest.raises(ValueError, match="match no schema label token"):
        _verify_label_corrections(subset, {})


def test_live_label_corrections_pass_the_guard() -> None:
    """A subset carrying the typo tokens satisfies the guard."""
    subset = {
        "maintCatMaintP": {
            "class": {"label": "Catalog Maitenance Policy"},
            "properties": {},
        },
        "vmmDomP": {
            "class": {"label": "VMM Domain"},
            "properties": {"hvAvailMonitor": {"label": "Enable Host availibility monitoring"}},
        },
    }
    _verify_label_corrections(subset, {})  # must not raise


def test_correction_alive_only_in_scopemeta_passes_the_guard() -> None:
    """Scopemeta labels count as the funnel's corpus too.

    A typo Cisco fixed in the JSON labels can survive in the scopemeta
    binaries; the guard must not force a deletion that would regress
    scopemeta-derived names.
    """
    subset = {
        "fvBD": {
            "class": {"label": "Bridge Domain"},
            "properties": {"arpFlood": {"label": "ARP Flooding"}},
        }
    }
    sm = {
        "maint.CatMaintP": {"name": "catalog-maitenance-policy"},
        "vmm.DomP": {"hvAvailMonitor": "host-availibility-monitoring"},
    }
    _verify_label_corrections(subset, sm)  # must not raise


def test_guard_tokenizes_like_the_funnel_not_like_grep() -> None:
    """Alive/dead is judged on funnel tokens, not raw substrings.

    ``"foo.maitenance"`` collapses to the single token ``foomaitenance`` in
    label_to_snake (dots are deleted, not separators), so the correction
    never fires on it — the guard must agree and call the correction dead.
    """
    subset = {
        "xOdd": {
            "class": {"label": "foo.maitenance availibility"},
            "properties": {},
        }
    }
    # "availibility" is a real (space-separated) token → alive; the glued
    # "foomaitenance" must NOT count for "maitenance".
    with pytest.raises(ValueError, match="maitenance"):
        _verify_label_corrections(subset, {})
