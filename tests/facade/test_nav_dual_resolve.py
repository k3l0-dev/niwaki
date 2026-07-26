"""Facade dual-resolve — every curated design position resolves on the facade.

The vocabulary overlay makes the curated maker name the navigation name at
each curated ``(parent, child)`` position.  This suite drives the facade's
own resolution machinery (``_navigate_jargon``: CHILD_MAP lookup + generated
model import) over all curated positions, proving the write-side vocabulary
is navigable read-side — with warnings escalated to errors so a deprecation
shim can never silently intercept a curated name.
"""

from __future__ import annotations

import warnings

import pytest

from niwaki.design._cursor import _load_class, _tables
from niwaki.facade import _navigate_jargon
from niwaki.models.base import ManagedObject


def _maker_positions() -> list[tuple[str, str, str]]:
    makers = _tables().makers
    return [
        (parent, name, child) for parent, table in makers.items() for name, child in table.items()
    ]


@pytest.mark.parametrize(("parent", "name", "child"), _maker_positions())
def test_curated_position_resolves_on_facade(parent: str, name: str, child: str) -> None:
    """The curated maker name resolves to its exact child class, warning-free."""
    parent_cls = ManagedObject if parent == "polUni" else _load_class(parent)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        target = _navigate_jargon(parent_cls, name)
    assert target.child_cls.__name__ == child
