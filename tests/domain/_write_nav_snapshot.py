"""Regenerate tests/domain/nav_snapshot.json from the current generated map.

Usage (from repo root)::

    uv run python tests/domain/_write_nav_snapshot.py
"""

from __future__ import annotations

import json
from pathlib import Path

from niwaki.domain._child_map import CHILD_MAP, NAV_DEPRECATED


def main() -> None:
    snapshot = Path(__file__).with_name("nav_snapshot.json")
    payload = {
        "CHILD_MAP": {p: dict(sorted(r.items())) for p, r in sorted(CHILD_MAP.items())},
        "NAV_DEPRECATED": {p: dict(sorted(r.items())) for p, r in sorted(NAV_DEPRECATED.items())},
    }
    snapshot.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    edges = sum(len(r) for r in CHILD_MAP.values())
    aliases = sum(len(r) for r in NAV_DEPRECATED.values())
    print(f"wrote {snapshot}: {edges} edges, {aliases} deprecated aliases")


if __name__ == "__main__":
    main()
