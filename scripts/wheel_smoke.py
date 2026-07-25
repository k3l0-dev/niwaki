"""Public-surface smoke against an *installed* niwaki wheel.

Run with the interpreter of a pristine venv where the wheel was installed
(never with the repo's editable venv):

    uv venv /tmp/smoke
    uv pip install --python /tmp/smoke/bin/python dist/niwaki-*.whl
    /tmp/smoke/bin/python scripts/wheel_smoke.py

Exercises the exact surface a consumer touches first: import, clients and
exceptions, a multi-domain design, closed-world reference resolution, and
the compiled payload.  Exits non-zero on any failure.
"""

from __future__ import annotations

import json

import niwaki
from niwaki import AsyncNiwaki, Niwaki, RetryConfig  # noqa: F401 — import surface
from niwaki.design import design
from niwaki.exceptions import NiwakiError, StagedPushError  # noqa: F401

# The ergonomic alias path is resolved at runtime by a MetaPathFinder, which no
# static analyser can see — checking it *works* in the built wheel is this
# script's job, so the unresolved import is the thing under test.
from niwaki.models.fv.fvBD import fvBD  # noqa: F401  # pyright: ignore


def main() -> None:
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
    assert catalog.fault_name("F0467"), "fault index missing from the catalogue"

    # The subscription stack must import from the wheel (its websockets
    # dependency is declared, not vendored — a packaging slip shows here).
    from niwaki.query import EventKind, Subscription  # noqa: F401
    from niwaki.transport._subscription_socket import SubscriptionSocket  # noqa: F401

    assert EventKind.CREATED == "created"

    print(f"wheel smoke OK — niwaki {niwaki.__version__}")


if __name__ == "__main__":
    main()
