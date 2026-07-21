"""Live diagnostic: reproduce https://unofficialaciguide.com's exact flow, verbatim.

Not a pytest test -- a standalone script for manual troubleshooting. Bypasses
niwaki's transport layer *entirely* (plain ``requests`` + ``websocket-client``,
the same libraries the community guide uses) to rule out any niwaki-specific
bug in how the WebSocket URL/cookie/token is built. If this literal,
community-blessed flow also gets zero pushes, the anomaly is not in niwaki's
implementation.

Sequence, matching the guide exactly:
    1. POST aaaLogin.json -> token
    2. Open wss://<apic>/socket<token>  (BEFORE any subscribe)
    3. GET .../fvBD.json?subscription=yes&refresh-timeout=60 under our tenant
    4. Background thread prints every raw ws.recv()
    5. Modify the existing BD via a plain POST (also raw requests, no niwaki)
    6. Wait and report whether anything was printed

Run (pulls in the two extra deps for this one-off script without touching
pyproject.toml):
    uv run --with websocket-client --with python-dotenv \
        python tests/debugging/subscription/try_unofficial_guide_flow.py
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

APIC = os.environ["APIC_HOST"].removeprefix("https://").removeprefix("http://")
USER = os.environ["APIC_USERNAME"]
PASSWORD = os.environ["APIC_PASSWORD"]

TN = "niwaki-it-subdebug-a"
BD = "bd-a"
WAIT_SECONDS = 20.0

received: list[str] = []


def printws(ws: websocket.WebSocket) -> None:
    while True:
        try:
            msg = ws.recv()
        except Exception as exc:  # socket closed/torn down -- stop quietly
            print(f"[reader] stopped: {exc!r}")
            return
        print(f"\n>>> WS FRAME: {msg!r}\n", flush=True)
        received.append(msg)


def main() -> None:
    # 1. Login -- verbatim from the guide.
    url = f"https://{APIC}/api/aaaLogin.json"
    body = {"aaaUser": {"attributes": {"name": USER, "pwd": PASSWORD}}}
    login_response = requests.post(url, json=body, verify=False)
    login_response.raise_for_status()
    response_body_dictionary = json.loads(login_response.content)
    token = response_body_dictionary["imdata"][0]["aaaLogin"]["attributes"]["token"]
    cookie = {"APIC-cookie": token}
    print(f"Logged in. token={token[:12]}...")

    # 2. Open the websocket -- BEFORE subscribing, exactly as the guide does.
    websocket_url = f"wss://{APIC}/socket{token}"
    ws = websocket.create_connection(websocket_url, sslopt={"cert_reqs": ssl.CERT_NONE})
    print(f"WebSocket connected: status={ws.status}")

    reader = threading.Thread(target=printws, args=(ws,), daemon=True)
    reader.start()

    # 3. Subscribe -- class query scoped to our tenant, matching the guide's
    #    plain `subscription=yes&refresh-timeout=60` shape.
    bd_url = (
        f"https://{APIC}/api/mo/uni/tn-{TN}.json"
        f"?query-target=subtree&target-subtree-class=fvBD"
        f"&subscription=yes&refresh-timeout=60"
    )
    sub_response = requests.get(bd_url, verify=False, cookies=cookie)
    sub_response.raise_for_status()
    sub_json = sub_response.json()
    subscription_id = sub_json["subscriptionId"]
    initial = sub_json.get("imdata", [])
    print(f"Subscribed. subscriptionId={subscription_id}, initial imdata count={len(initial)}")
    for obj in initial:
        print(f"  initial object: {obj}")

    # 5. Modify the BD via a plain POST -- no niwaki involved at all.
    import uuid

    new_descr = f"guide-flow-diagnostic-{uuid.uuid4().hex[:8]}"
    post_url = f"https://{APIC}/api/mo/uni/tn-{TN}/BD-{BD}.json"
    post_body = {"fvBD": {"attributes": {"descr": new_descr}}}
    print(f"\nModifying BD descr to {new_descr!r} via plain POST...")
    post_response = requests.post(post_url, json=post_body, verify=False, cookies=cookie)
    post_response.raise_for_status()
    print(f"POST status: {post_response.status_code}")

    # 6. Wait and report.
    print(f"\nWaiting up to {WAIT_SECONDS:.0f}s for a push frame...")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and not received:
        time.sleep(0.5)

    print()
    if received:
        print(f"*** GOT {len(received)} FRAME(S) -- the guide's literal flow WORKS. ***")
    else:
        print("!!! NOTHING received -- even the literal community-guide flow gets zero pushes.")
        print("!!! This points at the APIC/simulator side, not at niwaki's implementation.")

    ws.close()


if __name__ == "__main__":
    main()
