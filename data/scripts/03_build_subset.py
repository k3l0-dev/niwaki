"""Build sdk_subset.json from all configurable ACI classes.

Previously this file scoped output to 5 target classes.
Now it includes the full 2222-class set — every concrete configurable ACI class
is a codegen target.

Usage:
    uv run python data/scripts/03_build_subset.py

Input:  data/extracted/classes.json    (from 01_extract_classes.py)
        data/extracted/properties.json (from 02_extract_props.py)
Output: data/extracted/sdk_subset.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CLASSES_FILE = Path(__file__).parent.parent / "extracted" / "classes.json"
PROPS_FILE = Path(__file__).parent.parent / "extracted" / "properties.json"
OUTPUT = Path(__file__).parent.parent / "extracted" / "sdk_subset.json"


def main() -> None:
    for path in (CLASSES_FILE, PROPS_FILE):
        if not path.exists():
            print(f"ERROR: missing {path} — run 01 and 02 first", file=sys.stderr)
            sys.exit(1)

    all_classes: dict[str, dict[str, Any]] = json.loads(CLASSES_FILE.read_text())
    all_props: dict[str, dict[str, Any]] = json.loads(PROPS_FILE.read_text())

    sdk_subset: dict[str, dict[str, Any]] = {
        name: {
            "class": all_classes[name],
            "properties": all_props.get(name, {}),
        }
        for name in sorted(all_classes)
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(sdk_subset, indent=2, sort_keys=True))
    print(f"Written {len(sdk_subset)} classes  →  {OUTPUT}")


if __name__ == "__main__":
    main()
