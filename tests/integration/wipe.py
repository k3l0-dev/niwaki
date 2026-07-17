"""Manual teardown for the integration provisioning suite — USER-ONLY.

The exhaustive provisioning tests **never** clean up after themselves: they push
objects and leave them on the fabric (that is the point — the state stays for
inspection). Each test file owns a module-level ``wipe(aci)`` function that
deletes only the objects that file created. This runner locates those functions
and calls them against the live APIC.

It is run **by hand, by the operator** — the test suite never invokes it, and it
is deliberately kept out of pytest so it can never fire automatically.

Credentials come from ``APIC_HOST`` / ``APIC_USERNAME`` / ``APIC_PASSWORD``
(loaded from the repo-root ``.env``).

Usage::

    # wipe every file in a phase
    uv run python tests/integration/wipe.py 02_fabric-access

    # wipe a single file
    uv run python tests/integration/wipe.py 02_fabric-access/test_002_pools.py

    # wipe several targets at once
    uv run python tests/integration/wipe.py 02_fabric-access 04_tenant
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from dotenv import load_dotenv

from niwaki import Niwaki

ROOT = Path(__file__).resolve().parent  # tests/integration/


def _load_module(path: Path) -> ModuleType:
    """Import a test file by path (the phase folders are not Python packages)."""
    spec = importlib.util.spec_from_file_location(f"_wipe_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wipe_fn(path: Path) -> Callable[[Niwaki], object] | None:
    """Return the file's ``wipe`` callable, or ``None`` when it declares none."""
    fn = getattr(_load_module(path), "wipe", None)
    return fn if callable(fn) else None


def _targets(selectors: list[str]) -> list[Path]:
    """Resolve selectors (phase folder or single file) to test-file paths."""
    paths: list[Path] = []
    for selector in selectors:
        target = ROOT / selector
        if target.is_dir():
            paths.extend(sorted(target.glob("test_*.py")))
        elif target.is_file():
            paths.append(target)
        else:
            print(f"!! not found: {selector}", file=sys.stderr)
    return paths


def main() -> int:
    """Run the ``wipe`` of every selected test file against the live APIC."""
    selectors = sys.argv[1:]
    if not selectors:
        print(__doc__)
        return 2

    load_dotenv(ROOT.parent.parent / ".env")
    missing = [v for v in ("APIC_HOST", "APIC_USERNAME", "APIC_PASSWORD") if not os.getenv(v)]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2

    targets = _targets(selectors)
    if not targets:
        print("nothing to wipe", file=sys.stderr)
        return 2

    with Niwaki(
        os.environ["APIC_HOST"],
        os.environ["APIC_USERNAME"],
        os.environ["APIC_PASSWORD"],
        verify_ssl=False,
    ) as aci:
        for path in targets:
            rel = path.relative_to(ROOT)
            wipe = _wipe_fn(path)
            if wipe is None:
                print(f"—  {rel}: no wipe() declared, skipped")
                continue
            print(f"⌫  {rel}: wiping …")
            wipe(aci)
            print(f"✓  {rel}: done")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
