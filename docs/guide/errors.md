# Errors & recovery

Every error the SDK raises is a subclass of
{class}`~niwaki.exceptions.NiwakiError`, so one broad handler is always
possible — and each branch of the hierarchy answers one operational
question:

```text
NiwakiError
├── AuthError                      "can I log in?"
│   ├── LoginError                     wrong credentials
│   ├── TokenRefreshError              /aaaRefresh.json failed
│   └── SessionExpiredError            token dead, re-login also failed
├── TransportError                 "can I reach the APIC?"
│   ├── ConnectionError                host unreachable
│   ├── TimeoutError                   request too slow
│   └── TLSError                       certificate problem
├── APIError                       "what did the APIC answer?"
│   ├── UnauthorizedError              401 — token rejected
│   ├── ForbiddenError                 403 — insufficient privileges
│   ├── NotFoundError                  404 — MO does not exist
│   └── ServerError                    5xx — APIC internal error
├── DeserializationError           "can I type this response?"
├── NoResultError                  .one() matched nothing
├── MultipleResultsError           .one() matched more than one
├── UnknownClassError              catalogue lookup on an unknown class (also a KeyError)
├── DesignError                    "is my design coherent?"
│   ├── UnknownMakerError              no such maker at this position
│   ├── DuplicateDeclarationError      same object declared twice
│   ├── UnresolvedReferenceError       bind() target not in the design
│   ├── AmbiguousBindError             bind() matches several declarations
│   ├── StagedPushError                staged push partially applied
│   └── DanglingReferenceError         verify_refs: external target absent/wrong class
└── SubscriptionError              "what went wrong with a live subscription?"
    ├── StatsClassNotSubscribableError    stats class — the APIC never pushes for it
    ├── SubscribeRejectedError            the APIC rejected subscription=yes
    └── SubscriptionLostError              could not recover — see .reason
```

## What to catch when

| You are writing… | Catch | Typical reaction |
| --- | --- | --- |
| a CLI / one-shot script | `NiwakiError` | print and exit non-zero |
| a retry-around-auth loop | `AuthError` | rotate credentials, alert |
| network-sensitive automation | `TransportError` | back off, try the standby APIC |
| a read that may miss | `NotFoundError` | treat as absence, not failure |
| permission-scoped tooling | `ForbiddenError` | report the missing privilege |
| any push pipeline | `DesignError` | fix the design — do not retry |
| a staged rollout | `StagedPushError` | see the playbook below |
| a verified push (`verify_refs=True`) | `DanglingReferenceError` | fix or create the referenced objects; nothing was pushed |
| a live subscription's stream | `SubscriptionLostError` | resubscribe, or exit the watcher |

## Design errors are eager

Everything that can be checked before the wire **is** checked before the
wire.  A reference typo fails at resolution time, with the declared world
and a did-you-mean in the message:

```python
from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import UnresolvedReferenceError

config = tenant("prod")
config.vrf("main")
config.bd("web").bind(vrf="mian")  # typo — no such VRF declared

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
try:
    config.push(aci)
except UnresolvedReferenceError as exc:
    print(exc)  # …no fvCtx named 'mian' is declared… Did you mean 'main'?
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`connection`.

No request was sent: the design never left the process.  The same eagerness
applies to unknown makers (`UnknownMakerError` lists the available makers),
duplicate declarations, and attribute values (the Pydantic models validate
at the call site — see {doc}`models`).

## APIC answers as typed exceptions

HTTP status codes arrive as exception types, with the status and the APIC's
own message attached:

```python
from niwaki.exceptions import NotFoundError

try:
    aci.tenant("ghost").read()
except NotFoundError as exc:
    print(exc.status_code)  # 404
    print(exc)  # HTTP 404: MO not found at DN: 'uni/tn-ghost'
```

`APIError` exposes three attributes on every branch: `status_code`,
`apic_message` and `apic_code`.  Log all three — the message usually names the
offending attribute, and `apic_code` is the controller's own error code, the
machine-readable half.

`apic_code` is a `str | None`: `None` when the response carried no APIC error
envelope, or when the SDK raised the error itself (the 404 above is
synthesised — the APIC answered 200 with an empty result).  It says *what went
wrong*, never *whether to retry*: many distinct codes share HTTP 400, so decide
retryability from the exception type and the cause from the code.

```python
from niwaki.exceptions import APIError

# what a rejected delete looks like when you catch it
error = APIError(400, "Cannot delete object, not deletable", apic_code="107")

assert error.apic_code == "107"  # the controller's own cause
assert error.status_code == 400  # the HTTP status, independently
assert str(error) == "HTTP 400 (APIC code 107): Cannot delete object, not deletable"
```

## The `StagedPushError` playbook

A `strict` push cannot half-apply — the APIC rolls the whole envelope back.
A **staged** push can: it executes one operation per object, in DN-depth
waves, and a mid-flight failure leaves earlier waves applied.  The exception
carries the full picture in plain DNs:

```python
from niwaki.exceptions import StagedPushError

rollout = tenant("prod")
rollout.vrf("main")
rollout.bd("web").bind(vrf="main")

try:
    rollout.push(aci, mode="staged")
except StagedPushError as exc:
    print("applied :", exc.report.dns)  # what landed
    print("failed  :", [dn for dn, _ in exc.failures])
    print("skipped :", exc.not_run)  # never attempted
```

Recovery is declarative, like everything else:

1. **Read the first failure** — its APIC message names the real problem;
   later failures are usually collateral.
2. **Fix the design**, not the fabric: the applied objects are exactly what
   the design describes, so there is nothing to undo.
3. **Push again.**  Pushes are upserts — re-running the same design
   converges; already-applied objects are simply confirmed.
4. When in doubt, `mode="plan"` first: it shows precisely what a new push
   would still change (see {doc}`push-modes`).

## A subscription's stream ends with `SubscriptionLostError`

Every other subscription condition — a missed refresh, a reconnect that
*did* recover — is delivered as data in the event stream, not raised (see
{doc}`subscribing`). Only a subscription that could not be recovered at all
raises, and `.reason` says which recovery path was exhausted:

```python
from niwaki.exceptions import SubscriptionLostReason

assert SubscriptionLostReason.RECONNECT_EXHAUSTED == "reconnect_exhausted"
```

## Transport errors and retries

`ConnectionError`, `TimeoutError` and `TLSError` surface **after** the retry
policy is exhausted (see {doc}`connection`).  If you catch them, you are
seeing a genuine outage, not a blip — prefer alerting over looping another
retry around the SDK's own.

## Next steps

- {doc}`../cookbook/troubleshooting` — the push-failure ladder
- {doc}`../cookbook/troubleshooting-connection` — the connection ladder
- {doc}`testing` — asserting on failure behaviour
