"""Live diagnostic: does the public Query.subscribe() surface reproduce the
failure the pytest integration test still shows, even after the cookie fix?

``diagnose_token_match.py`` proved the fix works at the raw
``ApicSession.subscribe()`` level (a clean cookie jar, populated initial
imdata, a real push delivered) against a small, isolated debug tenant.
``tests/integration/test_subscribe_live.py::test_created_modified_deleted_events_are_typed_live``
still fails after the same fix, against the real integration tenant
(``niwaki-it-subscribe``) via ``aci.query(fvBD).under(tn_dn).subscribe()``.

This script reproduces that exact call shape standalone (no pytest, no
`_with_timeout` helper thread) to isolate: is this a Query-layer issue, a
tenant-clutter issue, or a pytest-harness artifact?

Run:
    uv run python tests/debugging/subscription/repro_query_layer.py
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.models.fv.fvBD import fvBD

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

TN = "niwaki-it-subscribe"
WAIT_SECONDS = 20.0


def main() -> None:
    print("Logging in...", flush=True)
    aci = Niwaki.connect(
        os.environ["APIC_HOST"],
        os.environ["APIC_USERNAME"],
        os.environ["APIC_PASSWORD"],
        verify_ssl=False,
    )
    print(f"Logged in. Using tenant scope {TN!r}.", flush=True)

    tenant(TN, description="live subscription integration coverage").push(aci)
    tn_dn = f"uni/tn-{TN}"
    bd_name = f"niwaki-it-repro-{int(time.time())}"

    sub = aci.query(fvBD).under(tn_dn).subscribe()
    print(f"Subscribed via Query surface. initial={len(sub.initial)} object(s)", flush=True)

    print(f"\nPushing a BD create: {bd_name} ...", flush=True)
    tenant(TN).bd(bd_name, description="Query-layer repro: created.").push(aci)

    print(f"Waiting up to {WAIT_SECONDS:.0f}s for a push (bounded, non-blocking)...", flush=True)
    box: queue.Queue[tuple[str, object]] = queue.Queue()

    def _wait_for_one() -> None:
        try:
            box.put(("ok", next(sub)))
        except Exception as exc:
            box.put(("err", exc))

    thread = threading.Thread(target=_wait_for_one, daemon=True)
    thread.start()
    try:
        kind, value = box.get(timeout=WAIT_SECONDS)
    except queue.Empty:
        print("\nNOTHING arrived within the wait window.", flush=True)
    else:
        if kind == "ok":
            print(f"=== EVENT: kind={value.kind}, mo.dn={value.mo.dn} ===", flush=True)  # type: ignore[attr-defined]
            print("\nGOT AN EVENT", flush=True)
        else:
            print(f"\nsubscription iterator raised: {value!r}", flush=True)

    sub.close()
    aci.close()


if __name__ == "__main__":
    main()
