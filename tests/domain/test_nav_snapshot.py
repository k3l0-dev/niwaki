"""Navigation snapshot — CHILD_MAP and NAV_DEPRECATED pinned to a committed file.

The snapshot makes every navigation rename reviewable in a diff: a regen that
changes any name (or silently gains/loses an edge) shows up as a JSON diff in
code review instead of vanishing into the 24,000-line generated module.

Regenerate after an intentional vocabulary/schema change::

    uv run python -m niwaki._codegen.generate_domain
    uv run python tests/domain/_write_nav_snapshot.py
"""

from __future__ import annotations

import json
from pathlib import Path

from niwaki.domain._child_map import CHILD_MAP, NAV_DEPRECATED

SNAPSHOT = Path(__file__).with_name("nav_snapshot.json")


def test_navigation_matches_committed_snapshot() -> None:
    committed = json.loads(SNAPSHOT.read_text())
    current = {"CHILD_MAP": CHILD_MAP, "NAV_DEPRECATED": NAV_DEPRECATED}
    assert current == committed, (
        "CHILD_MAP/NAV_DEPRECATED diverge from tests/domain/nav_snapshot.json — "
        "if the change is intentional, regenerate the snapshot "
        "(uv run python tests/domain/_write_nav_snapshot.py) and review its diff"
    )
