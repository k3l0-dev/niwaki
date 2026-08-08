# Versioning and deprecation

niwaki follows [semver](https://semver.org/).  This page states what a
version number promises, where the public API stops, and how a name is
retired — so that you can pin the SDK with confidence and know in advance
what an upgrade can do to your code.

## What each number promises

| Change | Meaning |
| --- | --- |
| **MAJOR** | A published API changed in a way that can break working code. Always accompanied by a migration note in the {doc}`changelog <CHANGELOG>`. |
| **MINOR** | New capability, backwards compatible. Existing code keeps working unchanged. |
| **PATCH** | Fixes only — including a curated position that was unreachable or wrong. |

A minor release may add parameters, methods, curated vocabulary and
generated classes.  It never removes a name you could reach, and never
changes what an existing call writes to the fabric.

One exception, and it cuts the other way: a **curated position the
controller refuses** is a defect, not a capability.  The vocabulary offers a
maker in a place the APIC rejects — the call has never written anything and
never could — so it is corrected rather than carried, and the changelog says
which one.  You lose nothing that worked; a push that used to fail against
the fabric now fails in your editor.

## The public API

Everything reachable from these import paths, with no leading underscore
in any segment:

```python
import niwaki  # Niwaki, AsyncNiwaki, catalog, exceptions, __version__
import niwaki.design  # design(), tenant(), infra(), fabric(), controller(), Cursor, ref()
import niwaki.models  # ManagedObject and the generated classes
import niwaki.query  # Query, AsyncQuery, filter expressions
import niwaki.transport  # ApicSession, AsyncApicSession, RetryConfig
import niwaki.exceptions  # the whole error hierarchy
import niwaki.catalog  # search, describe, prop_meta, class_meta, …
```

A single underscore anywhere in the path — `niwaki.design._engine`,
`niwaki.models._generated`, `ApicSession._request_with_retry` — is
internal.  It can change in any release, including a patch.

The curated vocabulary of the design DSL (maker names, bind aliases, verbs)
is part of the public API: those names appear in your code.

### What carries no promise

- **Message text of exceptions.**  Catch the type, read `.apic_code` or the
  structured attributes; never match on the sentence.
- **Log records.**  Format, level and logger names may change; nothing
  parses them as an interface.
- **Human-readable output** of `repr()` and of the plan summary.
- **Wire payload ordering** — the APIC does not care, and neither should a
  test. Compare parsed structures, not serialised strings.

## Deprecation

A name that is going away is never removed in the same release that
announces it:

1. The replacement ships, and the old name keeps working.
2. Using the old name emits a `DeprecationWarning` that **names its
   replacement** and is attributed to *your* line, not to a file inside the
   SDK.
3. The old name is removed no earlier than the **next minor release**, and
   the changelog says so when the warning first appears.

To surface these early in your own test suite:

```python
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    # your code here — a deprecated name now raises instead of warning
    pass
```

### Two tiers, deliberately

The write surface and the read surface do not move at the same speed:

- **The curated write surface** — everything you type to describe
  configuration — is frozen until a major release.
- **Schema-derived navigation and catalogue names** may be corrected in a
  minor release behind the deprecation shim above, because they are
  derived from Cisco's own schema and inherit its occasional mistakes.

## Supported versions

Security fixes land on the current minor release.  When a new major
version ships, the previous major keeps receiving security fixes for
**90 days**, so that an upgrade is planned rather than forced.

See {doc}`SECURITY` for how to report a vulnerability.

## Pinning

```text
niwaki>=1.8,<2      # recommended: every fix and feature, no breaking change
niwaki==1.8.0       # reproducible builds; upgrade deliberately
```

The SDK targets a specific APIC schema release, which is a separate axis
from its own version — see {doc}`../guide/compatibility`.
