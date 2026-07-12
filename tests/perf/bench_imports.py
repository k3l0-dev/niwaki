#!/usr/bin/env python3
"""Niwaki SDK — performance benchmark baseline.

Measures three axes:

1. **Cold-start import times** — how long does ``import X`` take in a fresh
   Python process (subprocess).  Includes both first-run (Pydantic compiles
   the model schema → writes ``.pyc``) and subsequent warm-cache runs.

2. **In-process domain navigation** — latency of the ``__getattr__`` dispatch
   chain once modules are loaded (µs).

3. **DN computation** — cost of assembling the distinguished name from naming
   kwargs (µs).

Results are written to ``tests/perf/results/baseline_YYYYMMDD_HHMMSS.json``
so they can be used as a reference for future optimisation iterations.

Usage::

    uv run python tests/perf/bench_imports.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ── helpers ───────────────────────────────────────────────────────────────────


def _time_subprocess(code: str, *, repeat: int = 5) -> dict[str, Any]:
    """Run *code* in a fresh interpreter subprocess and return timing stats.

    Args:
        code: Python source to execute (single-line or semicolon-separated).
        repeat: Number of subprocess invocations.

    Returns:
        Dict with ``first_ms``, ``min_ms``, ``mean_ms``, ``std_ms``, ``runs``.
        ``first_ms`` is reported separately because the first run may trigger
        Pydantic schema compilation and ``.pyc`` generation.
    """
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            cwd=ROOT,
        )
        times.append((time.perf_counter() - t0) * 1_000)

    return {
        "first_ms": round(times[0], 1),
        "min_ms": round(min(times), 1),
        "mean_ms": round(mean(times), 1),
        "std_ms": round(stdev(times), 1) if len(times) > 1 else 0.0,
        "runs": repeat,
    }


def _time_fn(fn: Callable[[], Any], *, repeat: int = 2_000) -> dict[str, Any]:
    """Time an in-process callable and return stats in microseconds.

    Args:
        fn: Zero-argument callable to benchmark.
        repeat: Number of timed iterations after a 50-call warm-up.

    Returns:
        Dict with ``min_us``, ``mean_us``, ``std_us``, ``runs``.
    """
    for _ in range(50):
        fn()

    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1_000_000)

    return {
        "min_us": round(min(times), 3),
        "mean_us": round(mean(times), 3),
        "std_us": round(stdev(times), 3),
        "runs": repeat,
    }


def _print_import(label: str, stats: dict[str, Any]) -> None:
    print(
        f"  {label:<28}"
        f"  first={stats['first_ms']:>7.1f} ms"
        f"  min={stats['min_ms']:>7.1f} ms"
        f"  mean={stats['mean_ms']:>7.1f} ms"
        f"  std={stats['std_ms']:>5.1f}"
    )


def _print_nav(label: str, stats: dict[str, Any]) -> None:
    print(
        f"  {label:<35}"
        f"  mean={stats['mean_us']:>8.3f} µs"
        f"  min={stats['min_us']:>8.3f} µs"
        f"  std={stats['std_us']:>6.3f}"
    )


# ── benchmark sections ────────────────────────────────────────────────────────


def bench_cold_imports() -> dict[str, Any]:
    """Subprocess import timing — measures true cold-start cost."""
    print("── 1. Cold-start imports (subprocess, n=5) ──────────────────────────")

    cases: list[tuple[str, str]] = [
        # uv overhead baseline
        (
            "baseline_python_pass",
            "pass",
        ),
        # SDK entry point
        (
            "import_niwaki",
            "from niwaki import Niwaki",
        ),
        # Domain map (large generated dict)
        (
            "import_child_map",
            "from niwaki.domain._child_map import CHILD_MAP",
        ),
        # Single model (Pydantic schema compilation)
        (
            "import_1_model_fvBD",
            "from niwaki.models._generated.fv.fvBD import fvBD",
        ),
        # 3 models — typical write script
        (
            "import_3_models",
            (
                "from niwaki.models._generated.fv.fvBD import fvBD; "
                "from niwaki.models._generated.fv.fvTenant import fvTenant; "
                "from niwaki.models._generated.fv.fvAEPg import fvAEPg"
            ),
        ),
        # 10 models — heavier provisioning script
        (
            "import_10_models",
            (
                "from niwaki.models._generated.fv.fvBD import fvBD; "
                "from niwaki.models._generated.fv.fvTenant import fvTenant; "
                "from niwaki.models._generated.fv.fvAEPg import fvAEPg; "
                "from niwaki.models._generated.fv.fvCtx import fvCtx; "
                "from niwaki.models._generated.vz.vzBrCP import vzBrCP; "
                "from niwaki.models._generated.l3ext.l3extOut import l3extOut; "
                "from niwaki.models._generated.fv.fvSubnet import fvSubnet; "
                "from niwaki.models._generated.vz.vzEntry import vzEntry; "
                "from niwaki.models._generated.infra.infraAttEntityP import infraAttEntityP; "
                "from niwaki.models._generated.fv.fvRsBd import fvRsBd"
            ),
        ),
        # Full domain-only session — no model imports, only domain nav
        # (first nav triggers lazy model import via CLASS_PKG)
        (
            "import_niwaki_nav_only",
            (
                "from niwaki import Niwaki; "
                "from unittest.mock import MagicMock; "
                "aci = Niwaki(MagicMock()); "
                "node = aci.tenant('prod').bd('web').vrf_binding('niwaki-prod')"
            ),
        ),
        # Design DSL entry point — typed cursors, no yaml/child_map at import
        (
            "import_niwaki_design",
            "from niwaki.design import tenant",
        ),
        # Design DSL first build — pays lazy yaml + CLASS_PKG on first maker
        (
            "import_design_first_build",
            (
                "from niwaki.design import tenant; "
                "cfg = tenant('prod'); "
                "cfg.bd('web').bind(vrf='prod'); "
                "cfg.vrf('prod')"
            ),
        ),
    ]

    results: dict[str, Any] = {}
    for key, code in cases:
        stats = _time_subprocess(code)
        results[key] = {"type": "cold_import_ms", **stats}
        _print_import(key, stats)

    print()
    return results


def bench_domain_navigation() -> dict[str, Any]:
    """In-process navigation latency — __getattr__ dispatch chain."""
    print("── 2. Domain navigation (in-process, n=2000) ────────────────────────")

    from unittest.mock import MagicMock

    from niwaki import Niwaki
    from niwaki.domain._child_map import CHILD_MAP

    sess = MagicMock()
    aci = Niwaki(sess)

    # Prime lazy imports — first __getattr__ call triggers import_module
    _ = aci.tenant("prod").app("myapp").epg("web").bd_binding("web")

    cases: list[tuple[str, Callable[[], Any]]] = [
        # Pure dict access (lower bound)
        (
            "child_map_dict_lookup",
            lambda: CHILD_MAP["fvTenant"]["bd"],
        ),
        # __getattr__ dispatch, 1 hop
        (
            "nav_1hop_tenant",
            lambda: aci.tenant("prod"),
        ),
        # 2 hops
        (
            "nav_2hop_tenant_bd",
            lambda: aci.tenant("prod").bd("web"),
        ),
        # 3 hops — Rs singleton navigation
        (
            "nav_3hop_tenant_bd_vrf",
            lambda: aci.tenant("prod").bd("web").vrf_binding(),
        ),
        # 3 hops + Rs singleton positional arg
        (
            "nav_3hop_rs_positional",
            lambda: aci.tenant("prod").bd("web").vrf_binding("niwaki-prod"),
        ),
        # 4 hops — EPG with BD binding (read-side Rs navigation)
        (
            "nav_4hop_epg_bd_binding",
            lambda: aci.tenant("prod").app("myapp").epg("web").bd_binding(),
        ),
    ]

    results: dict[str, Any] = {}
    for key, fn in cases:
        stats = _time_fn(fn)
        results[key] = {"type": "nav_us", **stats}
        _print_nav(key, stats)

    print()
    return results


def bench_dn_computation() -> dict[str, Any]:
    """DN assembly cost — ManagedObject.dn property."""
    print("── 3. DN computation (in-process, n=2000) ───────────────────────────")

    from unittest.mock import MagicMock

    from niwaki import Niwaki

    sess = MagicMock()
    aci = Niwaki(sess)

    tenant_node = aci.tenant("prod")
    bd_node = aci.tenant("prod").bd("web")
    epg_node = aci.tenant("prod").app("myapp").epg("web")
    rs_node = aci.tenant("prod").bd("web").vrf_binding("niwaki-prod")

    cases: list[tuple[str, Callable[[], Any]]] = [
        ("dn_tenant", lambda: tenant_node.dn),
        ("dn_bd", lambda: bd_node.dn),
        ("dn_epg", lambda: epg_node.dn),
        ("dn_rs_singleton", lambda: rs_node.dn),
        # Re-navigate + dn in one chain
        ("dn_chain_tenant_bd", lambda: aci.tenant("prod").bd("web").dn),
        ("dn_chain_4hop", lambda: aci.tenant("prod").app("myapp").epg("web").dn),
    ]

    results: dict[str, Any] = {}
    for key, fn in cases:
        stats = _time_fn(fn)
        results[key] = {"type": "dn_us", **stats}
        _print_nav(key, stats)

    print()
    return results


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║           Niwaki SDK — performance benchmark baseline            ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"  Python  : {sys.version.split()[0]}")
    print(f"  Measured: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    payload: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "benchmarks": {},
    }

    payload["benchmarks"].update(bench_cold_imports())
    payload["benchmarks"].update(bench_domain_navigation())
    payload["benchmarks"].update(bench_dn_computation())

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"baseline_{ts}.json"
    out.write_text(json.dumps(payload, indent=2))

    print(f"✓  Results saved → {out.relative_to(ROOT)}")
    print()

    # Print key headline numbers
    b = payload["benchmarks"]
    print("── Summary ───────────────────────────────────────────────────────────")
    print(f"  import Niwaki (cold)          : {b['import_niwaki']['mean_ms']:>7.1f} ms")
    print(f"  import 1 model (cold)         : {b['import_1_model_fvBD']['mean_ms']:>7.1f} ms")
    print(f"  import 10 models (cold)       : {b['import_10_models']['mean_ms']:>7.1f} ms")
    print(f"  domain nav 3-hop (warm)       : {b['nav_3hop_rs_positional']['mean_us']:>7.3f} µs")
    print(f"  full session no model import  : {b['import_niwaki_nav_only']['mean_ms']:>7.1f} ms")
    print()


if __name__ == "__main__":
    main()
