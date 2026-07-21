"""Live diagnostic: is the APIC delivering ANY WebSocket push frame at all?

Not a pytest test -- a standalone script for manual troubleshooting. The
symptom under investigation: subscribe() succeeds (a real subscriptionId
comes back, no exception), a subsequent create() lands on the fabric (visible
via a direct read/audit-log), but the SDK's own typed event layer never
observes a push for it -- next(sub) just times out.

This bypasses every layer above the raw transport socket (no Query, no
Subscription, no SubscriptionEvent) and monkeypatches SubscriptionSocket
._dispatch to print every raw frame recv() actually returns -- straight off
the wire, before any demux/routing logic runs. That separates two very
different root causes:

  (a) the APIC never sends anything on the socket at all (an event-manager/
      push-delivery problem on the APIC/simulator side) -- no "RAW FRAME"
      lines print at all, or
  (b) frames DO arrive but something in the SDK's demux/dispatch logic
      fails to route them to this registration's queue -- "RAW FRAME" lines
      print, but nothing ever reaches the RawSubscription's own queue.

Run:
    uv run python tests/debugging/subscription/diagnose_no_push.py

Owns nothing persistent: the BD it creates is left on the fabric for
inspection (same tenant `niwaki-it-subscribe` the integration test uses) --
delete it manually once the diagnosis is done.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.transport import _subscription_socket

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TN = "niwaki-it-subscribe"
BD_NAME = f"niwaki-it-sub-diag-{int(time.time())}"
WAIT_SECONDS = 20.0

# Wrap _dispatch so every raw frame recv() returns is visible, regardless of
# whether the SDK's own demux logic finds a matching registration for it.
_original_dispatch = _subscription_socket.SubscriptionSocket._dispatch


def _logging_dispatch(self: _subscription_socket.SubscriptionSocket, raw: str | bytes) -> None:
    print(f"\n>>> RAW FRAME RECEIVED ({len(raw)} bytes):\n{raw!r}\n", flush=True)
    _original_dispatch(self, raw)


_subscription_socket.SubscriptionSocket._dispatch = _logging_dispatch  # type: ignore[method-assign]


def main() -> None:
    aci = Niwaki(
        host=os.environ["APIC_HOST"],
        username=os.environ["APIC_USERNAME"],
        password=os.environ["APIC_PASSWORD"],
        verify_ssl=False,
    )
    aci.__enter__()
    print(f"Logged in to {os.environ['APIC_HOST']}.")

    tn_dn = f"uni/tn-{TN}"
    session = aci._sync_session  # type: ignore[reportPrivateUsage]  -- bypass Query/Subscription on purpose
    print(f"Subscribing to fvBD under {tn_dn} (raw transport layer, no typed wrapper) ...")
    raw_sub = session.subscribe(
        f"/api/mo/{tn_dn}.json", {"query-target": "subtree", "target-subtree-class": "fvBD"}
    )
    print(
        f"Subscribed. initial snapshot has {len(raw_sub.initial)} object(s), "
        f"wire subscriptionId={raw_sub.subscription_id}"
    )

    socket = session._subscription_socket  # type: ignore[reportPrivateUsage]
    assert socket is not None
    print(f"Socket connection object: {socket._ws!r}")  # type: ignore[reportPrivateUsage]
    reader = socket._reader_thread  # type: ignore[reportPrivateUsage]
    refresher = socket._refresh_thread  # type: ignore[reportPrivateUsage]
    print(f"Reader thread alive: {reader.is_alive() if reader else None}")
    print(f"Refresh thread alive: {refresher.is_alive() if refresher else None}")

    print(f"\nPushing a BD create: {BD_NAME} ...")
    tenant(TN).bd(BD_NAME, description="Diagnostic: does any push frame arrive at all?").push(aci)
    print(
        f"Push sent. Waiting up to {WAIT_SECONDS:.0f}s for ANY item on the subscription queue...\n"
    )

    deadline = time.monotonic() + WAIT_SECONDS
    got_anything = False
    while time.monotonic() < deadline:
        try:
            item = raw_sub._queue.get(timeout=1.0)  # type: ignore[reportPrivateUsage]
        except Exception:  # queue.Empty -- keep polling until the deadline
            continue
        got_anything = True
        print(f"=== ITEM ON THE SUBSCRIPTION QUEUE: {item!r} ===")

    print()
    if not got_anything:
        print(f"!!! NOTHING arrived on the subscription queue within {WAIT_SECONDS:.0f}s.")
        print("!!! Check the '>>> RAW FRAME RECEIVED' lines above:")
        print("!!!   - none printed  => the APIC never sent anything on the WebSocket at all")
        print("!!!   - some printed  => frames arrived but were not routed to this registration")
    else:
        print("At least one item arrived on the queue -- see above for its type/content.")

    raw_sub.close()
    aci.close()


if __name__ == "__main__":
    main()
