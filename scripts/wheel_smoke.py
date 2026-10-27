"""Public-surface smoke against an *installed* niwaki wheel.

Run with the interpreter of a pristine venv where the wheel was installed
(never with the repo's editable venv):

    uv venv /tmp/smoke
    uv pip install --python /tmp/smoke/bin/python dist/niwaki-*.whl
    /tmp/smoke/bin/python scripts/wheel_smoke.py

Exercises the exact surface a consumer touches first: import, clients and
exceptions, the public model import path, a multi-domain design, closed-world
reference resolution, and the compiled payload.  Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import niwaki
from niwaki import AsyncNiwaki, Niwaki, RetryConfig  # noqa: F401 — import surface
from niwaki.design import design
from niwaki.exceptions import NiwakiError, StagedPushError  # noqa: F401

# The public model path is a generated mirror of real re-export modules
# (src/niwaki/models/<pkg>/<Cls>.py) — importing it from the installed wheel
# proves those files are packaged.
from niwaki.models.fv.fvBD import fvBD


def _niwaki_import_hooks() -> list[str]:
    """``MetaPathFinder`` instances in ``sys.meta_path`` whose class is niwaki's."""
    return [
        f"{type(finder).__module__}.{type(finder).__qualname__}"
        for finder in sys.meta_path
        if type(finder).__module__ == "niwaki" or type(finder).__module__.startswith("niwaki.")
    ]


# The typed navigation is a stub-only package: checker-facing ``.pyi`` files,
# no runtime module.  These are the files a wheel without them would lose.
NAV_STUBS_REQUIRED = frozenset({"__init__.pyi", "_base.pyi", "_root.pyi", "fv.pyi"})


def nav_stub_problems(package_dir: Path) -> list[str]:
    """What is wrong with the typed-navigation stubs of an installed niwaki.

    Args:
        package_dir: The installed ``niwaki`` package directory.

    Returns:
        Human-readable problems, empty when the stubs are packaged correctly:
        ``py.typed`` present (without it checkers ignore every stub),
        ``_generated_nav/`` holding at least :data:`NAV_STUBS_REQUIRED`, and
        not a single ``.py`` in it (a ``.py`` would make it importable).
    """
    problems: list[str] = []
    if not (package_dir / "py.typed").is_file():
        problems.append("py.typed marker missing")
    nav = package_dir / "_generated_nav"
    stubs = {path.name for path in nav.glob("*.pyi")} if nav.is_dir() else set()
    missing = sorted(NAV_STUBS_REQUIRED - stubs)
    if missing:
        problems.append(f"navigation stubs missing: {missing}")
    if nav.is_dir() and sorted(nav.rglob("*.py")):
        problems.append("a .py module inside the stub-only _generated_nav package")
    return problems


def main() -> None:
    # No import hook may serve the model path: a hook would hide a packaging
    # slip that dropped the mirror files from the wheel.
    hooks = _niwaki_import_hooks()
    if hooks:
        sys.exit(f"wheel smoke FAILED — niwaki import hook(s) in sys.meta_path: {hooks}")
    assert fvBD.__module__ == "niwaki.models._generated.fv.fvBD", fvBD.__module__
    assert fvBD.__name__ == "fvBD"
    alias_file = sys.modules["niwaki.models.fv.fvBD"].__file__
    assert alias_file is not None, "the public model module has no file"
    assert Path(alias_file).parts[-3:] == ("models", "fv", "fvBD.py"), alias_file

    # Typed navigation ships as .pyi stubs only — in the wheel, never imported.
    problems = nav_stub_problems(Path(niwaki.__file__).resolve().parent)
    if problems:
        sys.exit(f"wheel smoke FAILED — typed navigation stubs: {problems}")

    cfg = design()
    cfg.fabric().datetime_policy("ntp").ntp_provider("10.0.0.1")
    tn = cfg.tenant("prod")
    tn.vrf("main")
    tn.bd("web", unicast_routing=True).bind(vrf="main").subnet("10.0.1.1/24")

    payload = json.dumps(cfg.to_payload())
    assert "fabricInst" in payload, "fabric domain missing from the envelope"
    assert '"tnFvCtxName": "main"' in payload, "closed-world bind did not resolve"

    # The read catalogue must ship IN the wheel and open from its installed
    # location — the v1.2.0 near-miss (catalog.db generated but never
    # packaged) is exactly what these probes pin.
    from niwaki import catalog

    doc = catalog.describe("topSystem")  # catalogue-served class, no model
    assert doc.props, "catalogue served no properties for topSystem"
    assert "fvBD" in catalog.generated_classes(), "generated-set enumeration broken"
    assert not catalog.class_meta("topSystem").has_model
    # Naming seam: the shipped db and its reader move together — and the 2.0
    # naming policy is what the wheel actually serves (the readable name,
    # not the 1.x wire transliteration).
    assert (
        catalog.class_meta("l3extOut").wire_to_readable["enforceRtctrl"] == "enforce_route_control"
    )
    assert catalog.fault_name("F0467"), "fault index missing from the catalogue"

    # The subscription stack must import from the wheel (its websockets
    # dependency is declared, not vendored — a packaging slip shows here).
    from niwaki.query import EventKind, Subscription  # noqa: F401
    from niwaki.transport._subscription_socket import SubscriptionSocket  # noqa: F401

    assert EventKind.CREATED == "created"

    navigation = Niwaki().tenant("prod").bd("web")
    assert navigation.dn == "uni/tn-prod/BD-web", navigation.dn
    leaked = sorted(m for m in sys.modules if m.startswith("niwaki._generated_nav"))
    assert not leaked, f"the navigation stubs were imported at runtime: {leaked}"

    print(f"wheel smoke OK — niwaki {niwaki.__version__}")


if __name__ == "__main__":
    main()
