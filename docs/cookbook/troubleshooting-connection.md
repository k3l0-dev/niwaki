# Troubleshooting connection & auth

**Problem** — `Niwaki(...)` raises before you get to do anything, or a
long-running job dies mid-flight with an auth error.  This is the diagnostic
ladder from "cannot reach the APIC" to "the session expired", one exception
at a time.  All of these are children of
{class}`~niwaki.exceptions.NiwakiError`, so a single `except` can catch the
lot while you branch on the type ({doc}`../guide/errors`).

## Rung 1 — `ConnectionError`: nothing answered

The host never responded: wrong URL, DNS, a proxy in the way, or the APIC
management address not reachable from where the code runs.

- Check the scheme and host: `https://apic.example.com` (no trailing API
  path — the SDK adds it).
- Can this machine reach the APIC's OOB/inband address at all?  A plain
  `curl -k https://apic.example.com` from the same shell settles it.
- Corporate proxies: httpx honours `HTTPS_PROXY`/`NO_PROXY` — an APIC on a
  management network usually belongs in `NO_PROXY`.

## Rung 2 — `TLSError`: answered, but not trusted

The socket opened and certificate verification failed.  Three cases, three
fixes — in this order:

1. **Enterprise CA** (most production fabrics): point the client at the CA
   bundle — `verify_ssl="/etc/ssl/certs/corp-ca.pem"` or the
   `SSL_CERT_FILE` environment variable ({doc}`../guide/connection`).
2. **Wrong hostname**: the certificate is valid but you connected by IP or
   a non-SAN alias — connect by the name in the certificate.
3. **Self-signed lab**: `verify_ssl=False`, loud and lab-only.

Never reach for `verify_ssl=False` to work around case 1 or 2.

## Rung 3 — `LoginError`: reached, refused

The APIC answered and rejected `aaaLogin`: wrong credentials, a locked
account, or a remote-auth (TACACS/RADIUS) domain that needs the
`apic:domain\\username` form.

When credentials come from the environment, check the fallback rules first:
a **set-but-empty** variable is used as-is (empty password → `LoginError`),
while a **missing** variable raises `KeyError` at construction.  Print what
the process actually sees:

<!--- skip: next --->
```python
import os

print({k: bool(os.environ.get(k)) for k in ("APIC_HOST", "APIC_USERNAME", "APIC_PASSWORD")})
```

## Rung 4 — `UnauthorizedError` / `SessionExpiredError` mid-run

Login worked, then a later request lost its session.  The client already
refreshes proactively (`refresh_threshold`, default 60 s) and retries one
transparent re-login on a mid-session 401 — if the error still surfaces,
something outside the process invalidated the token (APIC failover, admin
clearing sessions) or a single request outlived the refresh margin.  Widen
`refresh_threshold` for workloads with very long individual requests.

## Rung 5 — `TimeoutError`: too slow

The retries are already spent when this reaches you.  Scope the request
before reaching for knobs — a filtered, class-scoped query beats a raised
timeout ({doc}`../guide/observing`).  Then: `timeout=` (per request,
default 30 s) and {class}`~niwaki.RetryConfig` for flaky links.

## The ladder at a glance

| Exception | The question to ask | The knob |
| --- | --- | --- |
| `ConnectionError` | Can this machine reach that URL at all? | URL, DNS, `NO_PROXY` |
| `TLSError` | Who signed the APIC's certificate? | `verify_ssl=<CA bundle>` / `SSL_CERT_FILE` |
| `LoginError` | What credentials did the process really use? | env vars, auth domain prefix |
| `UnauthorizedError` / `SessionExpiredError` | What killed the token mid-run? | `refresh_threshold` |
| `TimeoutError` | Is the request scoped as tightly as it could be? | query scoping, `timeout`, `RetryConfig` |

A connection that works ends up here — everything after that is
{doc}`troubleshooting` (push failures) territory:

```python
from niwaki import Niwaki
from niwaki.exceptions import NiwakiError

try:
    with Niwaki("https://apic.example.com", "admin", "secret") as aci:
        print(len(aci.query("fvTenant").fetch()), "tenants visible")
except NiwakiError as exc:
    print(f"{type(exc).__name__}: {exc}")
```
