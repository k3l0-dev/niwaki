"""Live diagnostic: isolate one variable -- is it the ``websockets`` library?

``try_unofficial_guide_flow.py`` proved the community-guide flow (raw
``requests`` + the ``websocket-client`` package) works end to end: populated
initial imdata, a real push frame after a modify. niwaki's own transport uses
a *different* WebSocket library (``websockets.sync.client``, not
``websocket-client``) to open the socket. Everything else in niwaki's flow
(HTTP subscribe/refresh via ``requests``-equivalent httpx calls) mirrors the
guide closely.

This script swaps in ONLY the ``websockets`` library for the socket-open step
-- login and the HTTP subscribe/modify calls stay on plain ``requests``,
exactly as in the guide -- to isolate whether ``websockets.sync.client.connect``
handshakes differently (e.g. re-encodes the token embedded in the URL path)
in a way that breaks the APIC's session/socket linkage even though the
protocol-level handshake itself succeeds.

Run:
    uv run --with python-dotenv python tests/debugging/subscription/try_websockets_lib.py
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import uuid
from pathlib import Path

import requests
import websockets.sync.client as ws_client
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

APIC = os.environ["APIC_HOST"].removeprefix("https://").removeprefix("http://")
USER = os.environ["APIC_USERNAME"]
PASSWORD = os.environ["APIC_PASSWORD"]

TN = "niwaki-it-subdebug-a"
BD = "bd-a"
WAIT_SECONDS = 20.0

received: list[str | bytes] = []


def printws(ws: ws_client.ClientConnection) -> None:
    while True:
        try:
            msg = ws.recv()
        except Exception as exc:
            print(f"[reader] stopped: {exc!r}")
            return
        print(f"\n>>> WS FRAME: {msg!r}\n", flush=True)
        received.append(msg)


def main() -> None:
    url = f"https://{APIC}/api/aaaLogin.json"
    body = {"aaaUser": {"attributes": {"name": USER, "pwd": PASSWORD}}}
    login_response = requests.post(url, json=body, verify=False)
    login_response.raise_for_status()
    response_body_dictionary = json.loads(login_response.content)
    token = response_body_dictionary["imdata"][0]["aaaLogin"]["attributes"]["token"]
    cookie = {"APIC-cookie": token}
    print(f"Logged in. token repr={token!r}")
    print(f"Token length={len(token)}, contains '/':{'/' in token}, '+':{'+' in token}")

    # Open with the ``websockets`` library -- exactly what niwaki does
    # (``websockets.sync.client.connect``), same URL shape as the guide.
    websocket_url = f"wss://{APIC}/socket{token}"
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ws = ws_client.connect(websocket_url, ssl=ssl_ctx)
    print(f"WebSocket connected via `websockets` lib: {ws!r}")

    reader = threading.Thread(target=printws, args=(ws,), daemon=True)
    reader.start()

    bd_url = (
        f"https://{APIC}/api/mo/uni/tn-{TN}.json"
        f"?query-target=subtree&target-subtree-class=fvBD"
        f"&subscription=yes&refresh-timeout=60"
    )
    sub_response = requests.get(bd_url, verify=False, cookies=cookie)
    sub_response.raise_for_status()
    sub_json = sub_response.json()
    print(
        f"Subscribed. subscriptionId={sub_json['subscriptionId']}, "
        f"initial imdata count={len(sub_json.get('imdata', []))}"
    )

    new_descr = f"websockets-lib-diagnostic-{uuid.uuid4().hex[:8]}"
    post_url = f"https://{APIC}/api/mo/uni/tn-{TN}/BD-{BD}.json"
    post_body = {"fvBD": {"attributes": {"descr": new_descr}}}
    print(f"\nModifying BD descr to {new_descr!r} via plain POST...")
    post_response = requests.post(post_url, json=post_body, verify=False, cookies=cookie)
    post_response.raise_for_status()
    print(f"POST status: {post_response.status_code}")

    print(f"\nWaiting up to {WAIT_SECONDS:.0f}s for a push frame...")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and not received:
        time.sleep(0.5)

    print()
    if received:
        print(f"*** GOT {len(received)} FRAME(S) -- the `websockets` lib itself is fine. ***")
    else:
        print("!!! NOTHING received via the `websockets` lib socket.")
        print("!!! This isolates the bug to the `websockets`-based socket-open step.")

    ws.close()


if __name__ == "__main__":
    main()
