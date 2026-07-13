# Connection & transport

{class}`~niwaki.Niwaki` (sync) and {class}`~niwaki.AsyncNiwaki`
(async) own the whole session lifecycle: authentication, token refresh,
retries, pagination.  This page covers everything between your code and the
APIC socket.

## Constructing a client

The context-manager form authenticates on entry and closes the session on
exit — it is the right default:

```python
from niwaki import Niwaki

with Niwaki("https://apic.example.com", "admin", "secret") as aci:
    tenants = aci.query("fvTenant").fetch()
```

When a `with` block does not fit the shape of your program (long-lived
services, interactive sessions), `connect()` returns an authenticated client
and `close()` ends it:

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
tenants = aci.query("fvTenant").fetch()
aci.close()
```

## Credentials from the environment

Every constructor argument you omit falls back to an environment variable —
the natural fit for CI jobs and containers, where secrets arrive from the
runner and never live in code:

```python
import os

os.environ["APIC_HOST"] = "https://apic.example.com"
os.environ["APIC_USERNAME"] = "admin"
os.environ["APIC_PASSWORD"] = "secret"

with Niwaki() as aci:            # everything comes from the environment
    ...
```

| Argument | Environment variable |
| --- | --- |
| `host` | `APIC_HOST` |
| `username` | `APIC_USERNAME` |
| `password` | `APIC_PASSWORD` |

## TLS and timeouts

Certificate verification is **on by default** and should stay on for
anything that is not a lab.  Three situations, three settings:

**Publicly trusted CA** — the default (`verify_ssl=True`) verifies against
the system CA store.  Nothing to do.

**Private or enterprise CA** — most production APICs.  Point `verify_ssl`
at your CA bundle; never disable verification to work around a private CA:

<!--- skip: next --->
```python
with Niwaki(
    "https://apic.example.com",
    "admin",
    "secret",
    verify_ssl="/etc/ssl/certs/corp-ca.pem",  # PEM bundle, loaded eagerly
) as aci:
    ...
```

The bundle is loaded when the session is constructed, so a wrong path fails
immediately.  No-code alternative: set
`SSL_CERT_FILE=/etc/ssl/certs/corp-ca.pem` in the environment — the default
SSL context honours it.

**Lab APIC with a self-signed certificate** — opt out explicitly; the flag
is loud on purpose:

```python
with Niwaki("https://apic.example.com", "admin", "secret", verify_ssl=False) as aci:
    ...   # lab only — never disable verification against production
```

A failed verification surfaces as {class}`~niwaki.exceptions.TLSError` —
the diagnostic ladder is in {doc}`../cookbook/troubleshooting-connection`.

`timeout` (seconds, default `30.0`) applies per request.  Raise it for slow
answers on very large reads; prefer scoping the query first (see
{doc}`observing`).

```python
slow_fabric = Niwaki("https://apic.example.com", "admin", "secret", timeout=120.0)
```

## Retries

Transient transport failures are retried with exponential backoff and
jitter.  The policy is a frozen value object,
{class}`~niwaki.transport.RetryConfig`, passed at construction:

```python
from niwaki import RetryConfig

fail_fast = RetryConfig(attempts=1)                    # no retries at all
patient = RetryConfig(attempts=5, wait_max=30.0)       # unreliable WAN link

with Niwaki("https://apic.example.com", "admin", "secret", retry=patient) as aci:
    ...
```

| Field | Default | Meaning |
| --- | --- | --- |
| `attempts` | `3` | total tries (first call + retries); `1` disables retries |
| `wait_initial` | `0.5` | backoff before the first retry, seconds |
| `wait_max` | `5.0` | cap on the exponential backoff, seconds |
| `wait_jitter` | `0.5` | random jitter added to each wait, seconds |

Only transport-level failures are retried.  An APIC **4xx answer is not a
transient condition** — it raises immediately as a typed exception (see
{doc}`errors`).

## Token lifecycle

APIC sessions authenticate once (`aaaLogin`) and then live on a refreshable
token.  The client refreshes **proactively**: when a request finds the token
within `refresh_threshold` seconds of expiry (default `60`), it refreshes
before sending, so a long-running loop never trips over a dead session.  A
mid-session `401` triggers one transparent re-login before surfacing an
error.

There is nothing to manage — but two knobs exist:

- `refresh_threshold` — widen it if single requests in your workload can
  outlive the margin (very large exports over slow links);
- for the async client, refreshes are serialised internally so concurrent
  requests never race a re-login.

## Pagination

Reads are paginated transparently: `fetch()` returns the complete result
whatever its size, and `stream()` yields objects as pages arrive — same
query, different executor.  Nothing to configure; see {doc}`observing` for
when to prefer streaming.

## Next steps

- {doc}`errors` — what each transport failure raises
- {doc}`../cookbook/troubleshooting-connection` — the diagnostic ladder
