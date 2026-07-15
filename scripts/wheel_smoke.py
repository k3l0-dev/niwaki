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
    tn.bd("web", unicast_routing=True).subnet("10.0.1.1/24").bind(vrf="main")

    payload = json.dumps(cfg.to_payload())
    assert "fabricInst" in payload, "fabric domain missing from the envelope"
    assert '"tnFvCtxName": "main"' in payload, "closed-world bind did not resolve"

    print(f"wheel smoke OK — niwaki {niwaki.__version__}")


if __name__ == "__main__":
    main()
