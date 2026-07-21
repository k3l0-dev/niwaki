"""Live diagnostic: does niwaki's REAL subscribe() send the same token on the
WS URL and the HTTP subscribe cookie -- and does a raw frame ever reach
_dispatch at all?

``try_unofficial_guide_flow.py`` and ``try_websockets_lib.py`` both proved
pushes work fine when login/subscribe/modify are all done with a single,
shared token variable via plain ``requests`` (no httpx cookie jar, no
niwaki ``ApicSession`` token-state object). This script instruments
niwaki's *actual* ``ApicSession``/``SubscriptionSocket`` to check the one
thing those two scripts couldn't: whether niwaki's own token-state object
and httpx's cookie jar stay in lockstep with what got embedded in the
WS URL, and whether _dispatch ever sees a frame.

Run:
    uv run python tests/debugging/subscription/diagnose_token_match.py
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from niwaki import Niwaki
from niwaki.transport import _subscription_socket

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

TN = "niwaki-it-subdebug-a"
BD = "bd-a"
WAIT_SECONDS = 20.0

_original_open = _subscription_socket.SubscriptionSocket._open_socket_locked
_original_do_subscribe = _subscription_socket.SubscriptionSocket._do_subscribe
_original_dispatch = _subscription_socket.SubscriptionSocket._dispatch

_ws_token: str | None = None


def _logging_open(self: _subscription_socket.SubscriptionSocket) -> None:
    global _ws_token
    _original_open(self)
    _ws_token = self._session._token_state.token  # type: ignore[union-attr]
    print(
        f"[open] WS opened. token_state.token repr (first/last 20 chars): "
        f"{_ws_token[:20]!r}...{_ws_token[-20:]!r}"
    )
    cookie_now = self._session._client.cookies.get("APIC-cookie")
    assert cookie_now is not None
    print(
        f"[open] httpx cookie jar APIC-cookie (first/last 20): "
        f"{cookie_now[:20]!r}...{cookie_now[-20:]!r}"
    )


def _logging_do_subscribe(self, path, params, refresh_timeout):  # type: ignore[no-untyped-def]
    cookie_now = self._session._client.cookies.get("APIC-cookie")
    match = cookie_now == _ws_token
    print(f"[subscribe] about to GET. cookie == ws-url-token? {match}")
    if not match:
        print(f"[subscribe] MISMATCH -- cookie={cookie_now[:20]!r}...{cookie_now[-20:]!r}")
    result = _original_do_subscribe(self, path, params, refresh_timeout)
    print(f"[subscribe] wire_id={result[0]}, initial count={len(result[1])}")
    return result


def _logging_dispatch(self, raw):  # type: ignore[no-untyped-def]
    print(f"\n>>> _dispatch called with RAW FRAME ({len(raw)} bytes): {raw!r}\n", flush=True)
    _original_dispatch(self, raw)


_subscription_socket.SubscriptionSocket._open_socket_locked = _logging_open  # type: ignore[method-assign]
_subscription_socket.SubscriptionSocket._do_subscribe = _logging_do_subscribe  # type: ignore[method-assign]
_subscription_socket.SubscriptionSocket._dispatch = _logging_dispatch  # type: ignore[method-assign]


def main() -> None:
    aci = Niwaki(verify_ssl=False)
    aci.__enter__()
    print("Logged in via niwaki.")

    tn_dn = f"uni/tn-{TN}"
    session = aci._sync_session  # type: ignore[reportPrivateUsage]
    raw_sub = session.subscribe(
        f"/api/mo/{tn_dn}.json", {"query-target": "subtree", "target-subtree-class": "fvBD"}
    )
    print(f"Subscribed via niwaki. initial={len(raw_sub.initial)} object(s)")

    new_descr = f"token-match-diagnostic-{uuid.uuid4().hex[:8]}"
    print(
        f"\nModifying BD descr to {new_descr!r} via niwaki's own session.update-equivalent "
        f"(plain POST through the same authenticated session)..."
    )
    post_path = f"/api/mo/uni/tn-{TN}/BD-{BD}.json"
    resp = session._client.post(  # type: ignore[reportPrivateUsage]
        post_path, json={"fvBD": {"attributes": {"descr": new_descr}}}
    )
    print(f"POST status: {resp.status_code}")

    print(f"\nWaiting up to {WAIT_SECONDS:.0f}s for anything on the queue...")
    deadline = time.monotonic() + WAIT_SECONDS
    got = False
    while time.monotonic() < deadline:
        try:
            item = raw_sub._queue.get(timeout=1.0)  # type: ignore[reportPrivateUsage]
        except Exception:
            continue
        got = True
        print(f"=== QUEUE ITEM: {item!r} ===")

    print("\nGOT SOMETHING" if got else "\nNOTHING arrived on the queue.")
    raw_sub.close()
    aci.close()


if __name__ == "__main__":
    main()
