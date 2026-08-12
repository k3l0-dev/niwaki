# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[semver](https://semver.org/).  From 1.0.0 the configuration API is stable:
breaking changes ship in a new major version with a migration note.

## [2.0.0] — 2026-08-12

The fabric, reclaimed.  Since 1.0 the SDK has written configuration in one
direction: you describe, it pushes.  A real fabric starts at the other end —
years of GUI clicks, other tools and other people already live on the
controller.  2.0 closes the loop: **capture** the fabric
(`niwaki.snapshot`, since 1.10), **turn the capture into a design**
(`to_design`), **carve out** the part you own (`slice`), **render it as
Python** you can review, commit and diff (`to_code`), **combine** it with
what you already maintain (`merge`), then plan, push, and `reconcile` what
lives beside it.  An existing fabric becomes maintained code.

Every step of that circle is proven against a live fabric: a whole
controller captured and imported plans back with **zero reported changes**
(twice, deterministically), a scoped import and a carved slice plan clean,
the emitted Python **replays the real controller byte-for-byte**, and
`reconcile` reads it clean — five live acceptance scenarios on an APIC
6.0(9c) simulator, on top of a unit suite past 21,000 tests.

### Breaking

- **One naming policy for every generated field — 568 renames, with an
  automatic migration.**  A readable name is now a *name*: at most four
  words.  Where Cisco's schema label reads as prose, the 1.x field name
  transliterated the whole sentence
  (`realm_subtype_that_can_be_default_or_duo`); the concise form now wins
  (`realm_sub_type`).  And where a field was stuck with its wire spelling,
  it gains its readable name at last: `rtr_id` → `router_id`, `addr` →
  `ip_address`, `enforce_rtctrl` → `enforce_route_control`.  568 of the
  15,158 generated model fields change.

  **The wire never moves.**  Wire aliases are untouched, so payloads,
  filters (`where(arpFlood=...)`), item access (`mo["arpFlood"]`), snapshots
  and anything else speaking APIC names are unaffected — this renames what
  your Python says, never what the fabric hears.

  The migration is mechanical.  The full rename table ships in the
  repository (`tools/niwaki_migrate/migration_2_0.json` — the 568 model
  fields plus 59,336 catalogue readable names), and the `niwaki-migrate`
  codemod (repository tooling, deliberately outside the wheel) rewrites
  every call site it can prove and flags the ones it cannot, printing its
  own confidence ratio.  Measured on the SDK's own integration suite
  replayed from the last 1.x commit: 131 rewrites, 6 flags — and the flags
  covered exactly the sites a human had to touch anyway.  A real downstream
  project came through with **zero manual edits**, its entire test suite
  green against 2.0.

- **Every write door now fails loud on an unknown property.**  Constructing
  a model, assigning an attribute, and `surgical()` refuse a property the
  class does not carry, instead of silently dropping it — the misspelled
  kwarg that used to vanish into a "successful" push now fails at the call
  site.  Assigning under a wire alias is refused too, with a hint naming
  the readable field.  Reads stay tolerant, unchanged: `from_apic` and
  `from_event` accept whatever the controller serves.  `model_copy(update=)`
  remains the one documented escape hatch, and a test pins that contract.

### Added

- **`to_design(snapshot)` — the reverse import, the inverse of `push`.**
  Rebuilds a design from any capture, preferring the curated vocabulary
  (makers, `bind()`, the contract verbs — a captured `fvRsCtx` comes back
  as the `bind(vrf=...)` that would have created it) and falling back to
  the wire-name doors whenever a curated inversion cannot be made provably
  equivalent — a fallback, never a guess.  A live capture carries the wire's
  spelling of "not configured" (`""`, `vmac="not-applicable"`); the importer
  normalises it so the strict models never see it, and any value the model
  refuses but the fabric genuinely served rides the wire channel verbatim —
  the controller stays the judge.  Two opt-in policies:
  `on_unknown="raw"` carries classes and properties from a newer firmware
  on the wire channel instead of raising; `redacted="skip"` drops the
  secret values the capture elided (the right call on any real fabric —
  they all carry some).  A scoped snapshot (`"uni/tn-prod"`) imports with
  its ancestor chain rebuilt as attribute-less day-2 upserts.

- **`SnapshotImportError` / `ImportProblem`** — when an import cannot
  complete, every offending item across the whole tree is collected and
  reported at once (DN, kind, detail), never first-fail.

- **`raw()` / `raw_set()` — the wire-name doors, on every cursor.**
  Declare a child by ACI class name, set attributes under their wire names —
  for the classes outside the generated model set and for the moments only
  the wire spelling is at hand.  Validated by the shipped catalogue (the
  class must exist, be a legal child, carry its naming properties; an
  unknown wire property fails loudly) — an escape hatch, not a validation
  bypass.  And plan-faithful: configuration declared through the doors
  diffs in `mode="plan"` exactly like typed configuration.

- **`from_payload(payload)`** — the other way in: a `polUni` envelope —
  `to_payload()` output, or configuration JSON exported from other tooling —
  back into a design, through the same importer.

- **`DesignView` and `Cursor.view()`** — a design finally walks.  A frozen
  projection of the whole tree: iterate parents-first, look up by DN
  (`view["uni/tn-prod/BD-web"]`), filter with `by_class("fvBD")`.  Later
  edits do not move a taken view; take another.

- **`Cursor.slice(dn)`** — carve a fresh design holding one subtree, hung
  off its ancestors rebuilt as attribute-less upserts.  References crossing
  the cut follow the wire-footprint rule: a relation landing inside the
  slice is kept — as-is when its target is inside too, pinned wire-faithful
  otherwise (`bind_dn` for a DN-flavoured relation, an explicit relation
  child carrying the exact `tn*` name for a name-flavoured one) — never
  silently dropped.

- **`merge(*designs)` and `MergeConflictError`** — combine designs into a
  fresh one, union by DN.  An object declared in several sources must
  agree: same class, no attribute set to two different values — compared
  after coercion (`True` vs `"true"` is agreement) and across channels (a
  typed field and a `raw_set` of the same property must match).  Every
  contradiction across the whole merge is collected before raising.

- **`to_code(view)` — the emitter.**  Any design — hand-built, imported,
  composed — renders as reviewable, replayable Python DSL source.  The
  contract: executing the emitted source yields a design whose payload is
  canonically byte-identical to the source's, and it holds against reality
  — the code emitted from a live fabric import replays that fabric.
  Everything outside the curated vocabulary renders through the wire-name
  doors, including the tag and annotation objects every fabric touched by
  other tooling carries.

- **`reconcile(design, aci)` / `Reconciliation`** — the half of drift that
  `plan` does not look at: what the fabric carries that the design does not
  declare.  `extra` lists undeclared objects someone *created*; `implicit`
  separates the objects the fabric materialises on its own (schema-driven:
  a class marked non-creatable cannot be anyone's leftover — without the
  split, every declared BD would drown the report in its default
  relations); `orphan_subtrees` gives the minimal roots of the foreign
  regions.  One capture per call, read-only, and deliberately not a delete
  engine: a design still never removes what it does not declare.

- **Containment gains a third authority: the DN grammar.**  The schema's
  containment tables miss parent/child edges that real controllers serve —
  measured live on three of them.  A class's own DN formats state who its
  parent can be, so the shipped catalogue's DN grammar now backs the
  containment check: `raw()` and the reverse import accept every edge the
  fabric can prove about itself.

### Fixed

- **Named numbers no longer report phantom plan changes.**  A named-number
  field declared as the string spelling of a plain number
  (`maximum_power="30000"`) kept the string while the value read back from
  the fabric coerced to the integer — so every plan reported
  `'30000' → 30000`, forever.  Comparison now treats a string and an
  integer as equal when the string parses to that exact integer (booleans
  excluded).  Pre-existing defect, reachable without any import.

- **`mode="plan"` sees everything a design can declare.**  The plan read
  collected its class list from generated models only, so nodes declared
  through the wire-name doors were never read — and always reported as
  creates.  Separately, some real objects never answer a class query rooted
  at themselves (measured live: `quotaCont`, `maintLocalInstall`), so even
  a correct class read misses them.  The class collection now covers every
  node whatever its channel, and a subtree the class read did not return is
  confirmed absent by one bare `GET` at its root before being reported as a
  create — one extra request per absent subtree, not per object.

- **A legal direct child of the configuration root is no longer refused.**
  The internal containment table indexes the root under a private key; the
  containment check looked it up under its class name and could reject an
  edge the schema allows.

- **`niwaki.utils` API documentation repaired** — two broken
  cross-references (`mo_diff`, `parse_imdata`) in the rendered docs.

## [1.10.0] — 2026-08-10

### Added

- **`niwaki.snapshot` — deterministic, git-diffable captures of a fabric's
  configuration.** `snapshot.take(aci, scope)` reads every exportable object
  under a scope DN — the whole fabric (`"uni"`), one tenant, or any config
  subtree — and produces a document where the same fabric state always yields
  the same bytes: commit it to git and an unchanged fabric diffs empty, a
  config change diffs as exactly that change. Proven on a live fabric: 2,500+
  objects captured twice **across two separate logins**, byte-identical.

  What goes in is decided by the catalogue, never by a hand-maintained list:
  objects the schema marks exportable (Cisco's own definition of a config
  export — runtime state such as login sessions and faults stays out),
  properties the schema marks configurable (the operational halo of
  timestamps, statuses and computed backpointers falls away), in **wire
  format** (APIC names), so a capture outlives any renaming of the SDK's
  readable surface. The curated DSL vocabulary plays no role: an object the
  DSL cannot express is still configuration, and a backup that ignored it
  would lie. `Snapshot.coverage` counts what the capture contains, per class.

  **Secrets never ship.** Values the schema flags `secure` never read back
  from an APIC in the first place. On top of that, a curated policy catches
  the material the flag forgot — measured on a live fabric, a L4-L7 device
  credential echoes back in cleartext, and an SNMP trap destination's
  community string is a plain readable property — and redacts it to
  `"<redacted>"`. Objects whose *DN* carries a secret (an SNMP community
  profile is literally named by its community string) cannot be redacted in
  place: they surface in `Snapshot.warnings` so you decide, instead of a
  secret landing in git silently. The policy is closed by a drift guard that
  sweeps the whole schema three different ways; a future firmware's new
  password-shaped property fails the build until a human triages it.

- **`snapshot.diff(a, b)` — drift, structurally.** Two captures in, one
  verdict out: added, removed and changed DNs with per-attribute before/after
  values. Two moments of one fabric give config drift; the same scope on two
  fabrics gives divergence. Live: one added bridge domain diffs as exactly
  that subtree, fabric-wide, zero false positives.

- **`catalog.rn_format(class_name)`** — the template of a class's own DN
  segment (`"BD-{name}"`, `"subnet-[{ip}]"`), the inverse key for turning a
  DN read back from a fabric into its naming values. Verified round-trip on
  all 13,497 RN formats the catalogue carries, and against 2,400+ DNs read
  from a live fabric.

- **`catalog.prop_flags(class_name)` / `PropFlags`** — the fourteen per-
  property schema flags (configurable, read-only, secure, naming, create-only,
  implicit, …), unpacked from the shipped catalogue and memoised per class.
  The raw material of data-driven tooling: what is configuration, what the
  controller computes, what never echoes back.

### Internal

- Every generated artifact is now covered by a corpus-free freshness guard: a
  fingerprint manifest written at regeneration time lets any machine —
  including CI without the 1.7 GB schema corpus — detect a generator edited
  without a regeneration or an artifact edited by hand.

## [1.9.1] — 2026-08-09

### Fixed

- **`mode="plan"` no longer fails on a large design.** The plan read scoped
  each request to the classes the design declares — the right idea, carried
  the wrong way: the whole class list rode in a single query string, and past
  a few hundred declared classes the APIC front end refuses the request
  outright (*HTTP 414 Request-URI Too Large* — observed at a 19.7 KB class
  list against the controller's 4-8 KB request-line ceiling). The plan died
  before reading a single object, precisely on the fabric-sized designs it
  exists to check.

  The read is now split into as many smaller class-scoped requests as the URL
  budget requires — each sized to the *encoded* wire form, since a comma
  ships as `%2C` — and the SDK reassembles the object hierarchy from each
  object's DN before diffing. Small designs keep issuing one read per
  declared domain, same as before; only designs that would have died now
  issue several. Verified on a live fabric: the same class list that answers
  414 in one request reads back whole across 7, and a plan forced through
  the split path returns a verdict identical to the single-request read.

  Two side effects of the rework, both strict improvements:

  - The plan read now goes through the transport's pagination, so one shard
    returning more objects than the APIC result ceiling no longer risks a
    *result dataset is too big* refusal — the response side is bounded as
    well as the request side.
  - A malformed read response raises `DeserializationError` (a
    `NiwakiError`) instead of a bare `ValueError`, keeping the promise that
    one `except NiwakiError` catches every SDK failure.

  Visible wire change, for anyone inspecting traffic: the plan read is now
  `query-target=subtree` + `target-subtree-class` (flat, paginated) instead
  of `rsp-subtree=full` + `rsp-subtree-class` (nested). The diff results are
  unchanged.

## [1.9.0] — 2026-08-09

### Added

- **Certificate authentication, without a password anywhere** —
  `pip install niwaki[x509]`, then `Niwaki(host, user, private_key=..., cert_dn=...)`.
  Each request carries its own RSA-SHA256 signature instead of trading a
  password for a token, so CI has nothing to put in an environment variable
  and the fabric's audit trail names a certificate rather than a shared
  account. A signed session never logs in, holds no token, and has nothing to
  refresh.

  `cryptography` is an optional extra rather than a dependency: it is compiled,
  it has its own release cadence, and most callers authenticate with a
  password. Using the feature without it raises `MissingDependencyError`
  naming the extra, at import rather than at the first request.

  The half-configured case — a key without a DN, or a DN without a key — is
  refused at construction. Accepting it would silently fall back to password
  authentication, which is the opposite of what was asked for.

- **A push says what it is doing.** Ten thousand objects used to go out in
  silence; when one was refused the report named the DN, but nothing said how
  far the push had got. Start and finish are logged at `INFO`, each wave at
  `DEBUG`, and a partial push escalates its summary to `WARNING` so an
  application showing only warnings still hears about it.

  The SDK never configures logging — it attaches a `NullHandler` and emits
  under the `niwaki` logger; where records go is the application's decision.
  And **no payload is ever logged**: a design carries passwords, community
  strings and pre-shared keys, so the SDK records what it did and where, never
  what it sent. A canary asserts that through a *failing* push, since the
  branch that logs a refusal exists only on that path.

- **Bring your own `httpx` client.** `Niwaki.with_client()` and its async and
  session-level twins take a client you configured — an outbound proxy, mutual
  TLS, a pinned CA bundle, a custom transport, per-route timeouts. All of that
  is already expressible in `httpx`, so rather than mirror each as a parameter
  the SDK accepts the whole client and owns it thereafter.

  Construction is otherwise identical, and that is load-bearing rather than
  incidental: the injected path runs the same constructor and replaces one
  attribute, so a session built either way carries the same state. A test
  compares the two shapes rather than enumerating them, because the state has
  grown by three attributes in as many releases and an enumeration is a list
  someone forgets to update.

- **An on-disk token cache, opt-in.** `token_cache=TokenCache()` lets a
  short-lived process reuse a token instead of minting a session per run —
  the difference between one audit-log entry and twenty for an operator
  running twenty commands.

  It is a bearer token on disk, so it is treated as one. The file and its
  directory are created owner-only rather than chmod'ed afterwards, since
  between creation and a later chmod there is a window in which the token is
  world-readable. Entries are keyed by a hash of host and user, so a directory
  listing does not enumerate which fabrics an operator touches. And it refuses
  what it cannot trust — a file others can read, one that does not parse, one
  whose token expires within thirty seconds — rather than repairing it: the
  cost of being wrong is a security story, the cost of refusing is one login.

- **"Will this work against my fabric?" now has a programmatic answer.** The
  APIC states its firmware in the login envelope, and the SDK read that
  envelope for the token and discarded the rest. `Niwaki.apic_version` and
  `AsyncNiwaki.apic_version` hold it, refreshed for free on every token
  refresh, and `None` before connecting or when a controller names none.

  The other half is `catalog.schema_version()` — the firmware the models,
  vocabulary and filter grammar were generated from, read from the shipped
  catalogue's own manifest rather than a constant that could drift from the
  data it describes. Comparing the two is the whole feature.

  Nothing warns on a mismatch: connecting is not the moment to editorialise
  about firmware, and a warning on every connection to a 5.x lab would be
  noise. Reads stay tolerant, writes stay fail-loud, and the caller decides.

- **A busy controller no longer ends a push.** The transport retried network
  failures but never a status, so a `503` from an APIC that was merely
  occupied killed a staged push mid-flight — the failure most worth
  surviving. Retryable statuses are now retried, and the sets are **disjoint
  by request kind**: a read may replay `502`, `503` and `504`, a write only
  `503`. A gateway answering `502` or `504` has already forwarded the request,
  so the APIC may hold the object; replaying would double-apply it or 404
  against something the first attempt created. That mirrors the rule the SDK
  already applied to network errors. `429` is deliberately absent — nothing
  observed on a 6.0(9c) controller emits it, and a retry set is a claim about
  a controller rather than a wish.

  `Retry-After` is honoured when the controller sends one, in either spelling
  (seconds or HTTP date), and clamped by the new
  `RetryConfig.retry_after_max` (default 30 s) so one busy answer cannot park
  a push for as long as the controller asks. A header the SDK cannot parse
  reads as absent rather than raising inside the retry loop.

  When the attempts run out, the response is handed back rather than an
  internal signal, so the error a caller sees is the `HTTP 503` it always was.

- **`push(max_concurrent=...)` — the staged fan-out is the engine's own
  promise now, not an accident of the session you injected.** The wave engine
  is typed against a bare writer protocol, so its only bound came from the
  semaphore an `AsyncApicSession` happens to carry: any other conforming
  writer got the whole wave at once, measured 500 operations entering the
  writer simultaneously. The engine now pulls each wave through a fixed pool
  of workers, which also stops it materialising one coroutine and one retained
  payload per operation — a 10 000-op wave costs about 0.8 MB this way against
  15 MB before.

  The keyword **throttles down and never up**: the effective bound is the
  smaller of it and the client's own `max_concurrent`, so asking a default
  client for fifty still runs ten. Omitted, a push inherits the client's limit
  — which means a client built with a raised limit keeps it, and the wire
  behaviour without the keyword is byte-identical to previous releases. On a
  sync client it is accepted and inert: that engine writes one object at a
  time.

  `PushReport.dns` and `StagedPushError.failures` were always ordered by the
  design rather than by how fast the controller answered, but only because
  `asyncio.gather` happens to preserve argument order. That is now stated in
  the docstrings and guarded by a test, because the cheapest way to write a
  worker pool silently flips it to completion order. The same docstring now
  also says which DNs each mode reports: `staged` lists one entry per
  operation, so a class that ships its subtree atomically has no entries for
  its children, and a carrier class has none at all.

- **A non-positive concurrency limit is refused where it is written.**
  `AsyncNiwaki(..., max_concurrent=0)` used to build a semaphore with no
  permits — not a throttle but a wait for a slot that is never released, so
  the first write hung. It now raises `ValueError` at construction, naming the
  value, and the engine refuses the same at its own boundary.

- **`APIError.apic_code` — the controller's own error code, preserved.** The
  APIC answers every failure with a machine-readable code beside the English
  text, and the SDK used to drop it, so telling a malformed DN from a missing
  naming property meant matching on prose. Every `APIError` — and every
  subclass, including the one the subscribe path rebuilds — now carries it as
  `str | None`.

  It says *what went wrong*, never *whether to retry*: measured on a 6.0(9c)
  fabric, many distinct codes arrive under HTTP 400 and none of the observed
  ones is retryable, so retryability stays a question for the exception type.
  `None` is the honest answer when the response carried no APIC error envelope,
  or when the SDK raised without one — as it does for a DN that reads back
  empty. The value stays a string: the SDK will not fail to report an error
  because a controller sent a code it did not expect.

  `RefCheck` (external-reference verification) carries it too — that is the one
  place a failure is flattened to text rather than kept as an exception.

## [1.8.0] — 2026-08-08

### Documentation

- **The published exception hierarchy listed a class the SDK does not ship.**
  `ApicVersionMismatchWarning` appeared in the tree that `help(niwaki.exceptions)`
  and the error reference both render, so importing it — the exact use its
  sibling `DesignHintWarning` advertises — earned an `ImportError`. The tree is
  now checked against the package in both directions: it can no longer promise
  a name that does not exist, nor omit an error that does.
- **The counting idiom the SDK avoids is no longer taught as an example.**
  `execute_raw`'s docstring showed `rsp-subtree-include=count`, which asks the
  APIC for its own tally — a number that disagrees with reality on a scoped
  query while the request succeeds, so the answer looks legitimate. Measured on
  a 6.0(9c) fabric, five tenants out of twenty-eight reported zero bridge
  domains while holding up to a hundred and ninety-two. `count()` has always
  used the reliable idiom; the example now points at it, and `SubtreeInclude`
  says why its `COUNT` member behaves unlike every other facet — it replaces
  the result with a tally envelope rather than enriching it.

### Added

- **A published versioning and deprecation policy.** What each version
  number promises, where the public API stops, what carries no promise
  (exception message text, log records, `repr()` output), and how a name is
  retired — announced with its replacement, warned on your own line, removed
  no earlier than the next minor release.

- **A warning when a design guarantees a fault.** Attaching a domain to a
  floating SVI without an address leaves it at `0.0.0.0`, outside the subnet
  the SVI serves, and the APIC raises a major `F3744` every time. The push is
  legal, so this is a `DesignHintWarning` — its own category, so a pipeline
  can turn it into a failure with `simplefilter("error", DesignHintWarning)`
  without touching every other warning in the process. It lands on your line,
  not on a file inside the SDK.

- **`catalog.dn_formats(class_name)` — every DN shape a class can take.**
  A class is rarely reachable at one place in the tree: a subnet is the same
  class under a bridge domain, an EPG, a tenant, an L2Out external EPG and
  several service-graph nodes — twelve shapes for one class. The templates now
  ship in the catalogue and read back offline, verbatim from the schema. They
  are meant to be quoted rather than rebuilt: chaining parent RNs reproduces
  neither the shapes the APIC mints nor only those. Costs 3.4 MB on the
  catalogue and 2.5 MiB on the wheel.

### Fixed

- **`RetryConfig`'s docstring promised something it could not keep.** It said
  every parameter is forwarded verbatim to `stamina`; `retry_after_max` is
  not, and cannot be — it bounds a delay the controller asks for, which
  `stamina` knows nothing about.

- **The read catalogue no longer hands one thread another thread's rows.** Its
  sqlite connection is shared, and the driver caches prepared statements on the
  connection keyed by SQL text: two threads running the same lookup traded one
  statement object and overwrote each other's bindings mid-flight. Nothing
  raised — a lookup for one class simply came back holding another's data.
  Measured on the shipped catalogue, eight threads over four query shapes
  produced 2,751 wrong answers. This affected every catalogue read
  (`describe`, `class_meta`, `fault_name`, …), and the compatibility guide
  claimed the opposite. Statement caching is now off; a concurrency test fails
  if it comes back.
- **`nlb_endpoint` is offered only where the APIC accepts it.** An NLB
  endpoint is contained by a subnet, but a subnet hangs off a bridge domain,
  an L2Out external EPG or an in-band management EPG as readily as off an
  application EPG — and everywhere but the last the controller answers *"NLB
  MO should be contained only by fvAEPg"*. The maker now exists on the EPG
  subnet alone, in the typed surface and at runtime alike. Nothing that
  worked is lost: a push that used to be rejected by the fabric is now
  rejected in your editor.
- **`custom_qos_policy` is offered only where the APIC writes it.** The
  relation is declared under every EPG class in the schema, and on an
  application EPG, an ESG, an L2Out or an L3Out external EPG it is created,
  reaches `state=formed` and re-pushes idempotently. Under an in-band
  management EPG it does none of that: the first push is accepted and writes
  no relation at all, and a second one fails *"object not found"*. The bind
  is gone from that one position, in the typed surface and at runtime alike.
  The other binds on an in-band EPG — including `taboo_contract` and
  `imported_contract` — are untouched and keep working.
- **A colour no longer reads back as a change.** `pol:Color` and
  `health:ColorT` list `cyan`/`aqua` and `magenta`/`fuchsia` against the same
  value — the X11 pairs — and the APIC answers with one spelling whichever you
  write. A contract label declared `magenta` came back `fuchsia`, so every
  later `mode="plan"` reported a change that was not there. Only the spelling
  the fabric stores is canonical now, so a design and the fabric agree. Nothing
  is removed: `PolColor.MAGENTA` and `HealthColorT.CYAN` remain — as aliases of
  the stored member, so `PolColor.MAGENTA is PolColor.FUCHSIA` and writing
  either name, or either string, puts the stored spelling on the wire.
- **PTP intervals and ZR optical power are expressible again.** Cisco states
  the bounds of a signed property as *magnitudes*: ZR transmit power declares
  a minimum of 190 and a maximum of 50, meaning −190 to −50 hundredths of a
  dBm. Copied literally that is a range no value satisfies, and seven fields
  across `ptpProfile`, `ptpCfgDef`, `latencyPtpMode` and the four `xcvrZR*`
  interface policies rejected every value — including their own schema
  default. Signed bounds are now read as the range they describe.
- **`AsyncNiwaki` no longer strands a session.** Entering the context manager
  on a client returned by `connect()` built a second session and overwrote the
  first, leaving its HTTP client — and any subscription socket it owned — with
  nothing left to close it. Entering a connected client is now the no-op it
  always was on the sync side; re-entering after `close()` still reconnects.
- The supported-versions table advertised a `0.x` line; the compatibility
  reference claimed the sync session was not thread-safe, contradicting the
  guide (it is: the HTTP client is thread-safe and token refresh is
  serialised); and the navigation deprecation warning named fixed release
  numbers instead of the rule it enforces.

## [1.7.0] — 2026-07-28

### Removed

- **The pre-1.5.0 navigation aliases are retired.** The 773 deprecated
  navigation names introduced as compatibility shims by the 1.5.0 naming
  unification (announced for removal no earlier than 1.7.0) no longer
  resolve — navigating by an old name now raises the standard
  `AttributeError`. Migrate with the 1.5.0/1.6.x `DeprecationWarning`
  messages, which named each replacement. The shim mechanism itself stays
  in place and re-fills automatically if a future release renames
  navigation entries.

### Added

- **`push(verify_refs=True)` — external references checked before the
  wire.** A design's raw-DN references (`bind_dn` targets, the literal-DN
  makers such as `static_path`) were pushed on faith, and the APIC accepts
  a relation whose target does not exist — the config lands and the
  relation stays unformed; on the 6.0(9c) simulator, measured live, **no
  fault is ever raised**, making that field the only trace. The new
  opt-in verification reads every external DN (deduplicated, ~60 ms per
  unique DN, bounded concurrency in async) before anything is written: in
  `strict`/`staged` mode a missing or wrong-class target raises the new
  `DanglingReferenceError` carrying the complete failure list — nothing
  pushed; in `plan` mode the per-reference statuses land on the new
  `PlanResult.external_refs` and nothing raises. Expected target classes
  come from the read catalogue's own accept-sets, so read-only targets
  (fabric path endpoints) verify correctly. Without the flag, wire
  behavior is byte-identical to previous releases.
- **`Cursor.external_refs()`** — enumerate a design's external references
  without a transport (the `to_payload` inspection pattern); the GitOps
  cookbook's CI gate now verifies references in its plan and apply steps.
- **Guide: "Working with an existing fabric".** The brownfield contract,
  stated plainly and proven by executed examples: declaration is the
  boundary (not provenance), declaring an existing object takes it over,
  attribute-merge leaves every undeclared field and object untouched,
  deletion is never a push effect, and `bind_dn` + `verify_refs` is the
  safe way to lean on objects you do not own.

## [1.6.1] — 2026-07-27

### Added

- **Compatibility page.** The documentation now answers the evaluator's
  first question precisely: what niwaki is validated against (APIC
  6.0(9c), live), how it behaves on older and newer firmware (tolerant
  reads, fail-loud writes), the Python version posture, and the thread/
  task-safety contract of the clients.

### Internal

- The transport documentation now states what is actually consumed: the
  design engine's wave runner uses the async writer protocol; the facade
  and query builders drive the concrete sessions, and tests fake the HTTP
  layer. The previous docs implied a protocol-typed injection point that
  never existed. Finishing the boundary (a real query-transport protocol
  and typed accessors) is recorded as a design candidate for a planned
  cycle.

- Freshness guards extended to the navigation tables and the generated
  models (the catalogue already had one): a corpus-gated
  rebuild-and-compare of `_child_map.py`'s six tables, and a sampled
  re-render of fifteen naming-sensitive model classes compared against
  the committed tree. A generator or input change without a regen now
  breaks the build instead of shipping stale artifacts — the failure
  mode that lived three releases before 1.3.2.

## [1.6.0] — 2026-07-26

### Added

- **Subscription backpressure.** Each subscription's buffer is now bounded:
  past `max_pending` unconsumed events (default 10,000, a new keyword on
  `subscribe()` at every layer) incoming events are dropped — never
  blocking the shared socket or other subscriptions, and never touching
  control items (close/gap/refresh markers are exempt, so `close()` can
  never wedge on a full buffer). The stream receives one
  `SubscriptionOverflow` marker (`EventKind.OVERFLOW`) per overload
  episode — the episode ends once the consumer drains below half the
  bound — with the same contract as a gap: keep going, reconcile with a
  fresh read. `SubscriptionInfo` gains live `pending` and `dropped`
  counters, surfaced by `aci.subscriptions.list()`.
- **Vanished-consumer safety net.** A subscription garbage-collected
  without `close()` is now reaped automatically (with a warning) instead
  of accumulating events forever — previously its delivery queue grew
  unbounded with no consumer attached. Async subscriptions hop onto the
  socket's event loop to do this safely.
- **Flow-table-event telemetry reaches the design DSL.**
  `flow_collector_policy().fte_events_ext(...)` and
  `.fte_event_tcp_flags(...)` — validated live on APIC 6.0(9c) — and
  VSPAN destinations gain the `virtual_port_def` bind (`bind_dn` to a
  VMM-discovered vPort definition).

### Fixed

- **Three curation gaps closed.** All three were children of curated
  parents hidden behind the navigation edges the pre-1.5.0 generator
  silently dropped; 1.5.0's fail-loud generator recovered the edges and
  the coverage audit immediately flagged them (the two telemetry makers
  and the VSPAN bind above). The interim 1.5.0 auto-derived navigation
  names of the two telemetry classes (`telemetry_fte_events_ext`,
  `telemetry_fte_event_tcp_flags`) became their curated forms.
- **A GC-context deadlock in the subscription safety net, caught in
  review before release.** The first implementation of the
  vanished-consumer reaper acquired locks from garbage-collector context,
  which could freeze the event reader — and cyclic collection
  process-wide — if collection landed at the wrong moment. The reaper now
  defers all work out of GC context (a lock-free handoff to the refresh
  sweep).
- **Overflow marker flapping, caught in review before release.** A
  consumer hovering exactly at the buffer bound would have received up to
  half its stream as overflow markers (and one warning log each). The
  overflow flag now has hysteresis: one marker per overload episode.

## [1.5.0] — 2026-07-26

### Changed

- **The facade navigates by the curated design vocabulary.** At every
  curated position the read-side navigation name is now the exact design
  maker name — `aci.tenant("t").vrf("v").pim()` where navigation used to
  say `pim_ctx` — and auto-derived names elsewhere drop their
  sentence-length labels for the pkg-prefixed class name (`dwdm_if_pol`,
  not `profiles_for_dwdm_to_be_applied_at_the_interface_level`). 773
  navigation names change in total; every old name keeps resolving — to
  the same class — with a `DeprecationWarning` naming its replacement
  (removal no earlier than 1.7.0). The full table is the
  [deprecated navigation names](https://k3l0-dev.github.io/niwaki/reference/vocabulary/deprecated-navigation.html)
  reference page. Design-DSL maker names, `read()`, `query()` and DNs are
  untouched.
- **Navigation gaps closed.** 20 containment edges the old generator
  silently dropped on name collisions are navigable again (the generator
  now breaks the build rather than dropping an edge), and two misspelled
  Cisco labels leave the navigation surface: `catalog_maitenance_policy`
  is now `catalog_maintenance_policy`, and `vmm_host_availibility_policy`
  resolves to the curated `host_availability_policy` (both misspellings
  remain as deprecated aliases).

- **Catalogue reads now name fields exactly like the typed models.** The
  shipped `catalog.db` gains a `name_override` table freezing the six
  properties (the `l3ext` family) where the catalogue's derived names
  diverged from the generated models — introspected from the models
  themselves at build time, so `describe()`/`class_meta()`/dynamic reads
  agree with what typed code sees (`enforce_rtctrl`, not
  `enforce_route_control`). Five documented divergences remain by design:
  their model name collides with a wire property only the catalogue
  serves. The build now breaks on any new unfrozen divergence, and the
  db's content hash covers the frozen names.

### Added

- **`AsyncNiwaki.connect()`** — the async twin of `Niwaki.connect()`, for
  long-lived services that own the lifecycle instead of using
  `async with`: `aci = await AsyncNiwaki.connect(...)`, paired with
  `await aci.close()`. Same options, same login path as the context
  manager. Validated live against APIC 6.0(9c).
- **"Ask the docs" chat on the documentation site.** Every page of
  <https://k3l0-dev.github.io/niwaki/> now carries a floating chat widget
  answering questions from the library's indexed documentation (Context7).
  The library is also indexed for AI coding agents — Context7 id
  `/k3l0-dev/niwaki` (with 20 maintainer rules injected into every agent
  context) and an AI architecture wiki on DeepWiki — completing the
  AI-onboarding surface started in 1.4.1.

### Internal

- Naming unification, lot A: the name-derivation and base-type
  classification rules move from the code generators into
  `niwaki._schema` (stdlib-only), the single authority both the
  generators and the runtime catalogue consume. `_codegen` no longer
  ships in the wheel.

## [1.4.1] — 2026-07-25

### Fixed

- **`from niwaki import aaa` raised `ImportError`.** The `aaa` design root
  (AAA/authentication configuration) was exported by `niwaki.design` but
  missing from the package's lazy top-level roots, while every sibling
  (`tenant`, `infra`, `fabric`, `controller`) worked. Also added to the API
  reference, which omitted it.
- **`to_apic()` on catalogue-served objects produced an unusable envelope.**
  An object read back through a string query on a class with no generated
  model serialised as `{'': {'attributes': {}}}` — empty class, empty
  attributes, no error — and a field assigned after the read (documented as
  the read-modify-write idiom) was silently dropped, while reading it back
  returned the stale pre-assignment value. Such objects now honor the same
  surgical contract as typed models: the envelope carries the wire class
  they were read as, naming props are resolved through the catalogue,
  explicitly-assigned readable names serialise (translated and coerced),
  and reads return what was assigned.
- **Catalogue lookups now raise a typed error.** `catalog.describe()`,
  `class_meta()` and `prop_meta()` raised a bare `KeyError`, contradicting
  the error guide's promise that every SDK error is a `NiwakiError`. They
  now raise `UnknownClassError` — also a `KeyError`, so existing handlers
  keep working unchanged.
- Two error messages now point at the right surface: a write verb on the
  read-only facade (`.create(...)` — the first thing every ORM or cobra
  user tries) steers to the design DSL instead of deeper into observation,
  and an unavailable verb no longer claims verbs are contract-only.
- Documentation accuracy: the DSL internals page no longer claims the DSL
  and facade always agree on names (curation deliberately shortens
  write-side names; the navigation reference is the mapping), and
  `vocabulary.yaml`'s header again describes its actual sections and the
  tables that validate it.

### Internal

- The wheel smoke test now probes the read catalogue (describe,
  `generated_classes()`, fault index) and the subscription stack from the
  installed wheel — the packaging near-miss that class of defect slipped
  through once before.
- The public export preflight now builds the documentation (nitpicky
  Sphinx) before anything ships — a broken cross-reference fails the
  export instead of debuting as a red X on the public repository.
- `context7.json` (Context7) and `.devin/wiki.json` (DeepWiki) added:
  curated metadata, folder scoping, usage rules and an explicit page plan
  for the AI documentation indexes — fully curated by policy, mirroring
  the DSL's own hand-curated vocabulary.

## [1.4.0] — 2026-07-25

### Added

- **`catalog.generated_classes()`** — the wire names of every class the SDK
  generates a typed model for, as a sorted tuple. Offline, deterministic,
  and derived from the code generator's own shipped index, so it cannot
  drift from the model files. Every returned name resolves through
  `catalog.describe()`/`class_meta()` without `KeyError`, and every returned
  class is concrete and non-stat — both pinned by tests. The intended use
  is systematic sweeps: auditing what a fabric actually uses, one `count()`
  per configurable class, with this as the candidate list.
- **`ClassMeta.has_model`** — the per-class form: whether the SDK ships a
  generated model for the class (results then deserialize through the typed
  model rather than the catalogue). Recomputed from the same index, never
  stored in the `.db`.
- **Provenance statement in `NOTICE`.** The ACI schema-derived metadata the
  package ships — class and property names, types, formats, labels, and the
  descriptive text in the read catalogue, model docstrings, and reference
  documentation — derives from the Cisco APIC Management Information Model.
  `NOTICE` now states this explicitly: that content remains the property of
  Cisco Systems, Inc., is reproduced solely for interoperability with and
  documentation of the Cisco ACI API, and is not covered by this project's
  Apache-2.0 license. (`NOTICE` ships in the wheel via the project's
  `license-files`.)

## [1.3.2] — 2026-07-25

### Fixed

- **Eleven `faultThrValue*` classes were missing from the read catalogue.**
  They ship a generated model, yet `catalog.describe()` / `catalog.class_meta()`
  raised `KeyError` on them, and `catalog.concrete_subclasses("faultAThrValue")`
  silently returned an empty list. The catalogue build skipped every class
  whose schema carries an empty `readAccess` list — 151 classes in total,
  including genuinely readable ones (the `rmon*` interface counters among
  them). An empty `readAccess` means Cisco documents no per-privilege RBAC
  mapping for the class, not that it is unreadable: the APIC serves class
  reads for these names and rejects only unknown classes. The catalogue now
  indexes the full class corpus (15,452 classes), and the build fails loudly
  if any generated-model class ever lacks a catalogue row.
- **Deprecated classes advertised as navigable children then crashed.**
  The config/navigation table carried 18 deprecated classes that have no
  generated model, so reachable facade navigation — e.g.
  `aci.fabric().communication_policy(...).telnet_service(...)` — raised a
  raw `ModuleNotFoundError` instead of the designed "no child accessor"
  `AttributeError`. The table's generator now applies the same
  deprecated/hidden filter as the model generator.

### Changed

- Removing the deprecated shadow classes re-ran name disambiguation, so a
  few auto-derived facade navigation accessors are renamed to their now
  unambiguous form: `in_band_management_epg` (was
  `mgmt_in_band_management_epg`, same target), `out_of_band_management_epg`
  (was `mgmt_out_of_band_management_epg`, which crashed — it pointed at a
  deprecated class; it now reaches `mgmtRsOoB`), and
  `relation_to_a_set_of_concrete_interfaces_from_the_device_in_the_cluster`
  (was prefixed `vns_`, same target). `fabricSetupP` gains a
  `pod_subnets_in_addition_to_setupp` child accessor that was previously
  unreachable, and seven references that a deprecated class used to shadow
  now resolve automatically in `bind()`.

### Internal

- `domain._child_map.CLASS_PKG` (private) shrinks from 2,239 to 2,221
  entries and now matches the generated-model set exactly, minus `faultInst`
  (deliberately excluded from the config surface). Code importing this
  private module should migrate to the public API.
- New parity tests pin the model tree, its `_PKG_MAP` index, `CLASS_PKG`,
  and the shipped catalogue against each other, so neither gap can silently
  reopen; the naming-parity test no longer swallows `KeyError`, which is how
  the missing catalogue rows went unnoticed.

## [1.3.1] — 2026-07-21

### Fixed

- **Subscription push silently never delivered.** `login()`/`_refresh_token()`
  (sync and async) explicitly set the `APIC-cookie` cookie on the httpx
  client, on top of the cookie httpx already stores automatically from the
  APIC's own `Set-Cookie` response header. The explicit call didn't pin a
  `domain`, so it landed as a second, distinct jar entry (same name, empty
  domain) rather than overwriting the first. The APIC accepts the resulting
  malformed, duplicated `Cookie: APIC-cookie=...; APIC-cookie=...` header for
  ordinary reads/writes and even for the subscribe GET itself (a valid
  `subscriptionId` still comes back) — but it silently breaks the APIC's
  internal link between that request and the caller's already-open
  WebSocket, so no push ever arrives, and the subscribe response's initial
  `imdata` can come back empty even when matching objects exist. Fixed by
  pinning the cookie to the session's own host on every `login()`/
  `_refresh_token()` call, so it overwrites in place instead of duplicating.
  Only `subscribe()` was affected — every other read/write path was already
  correct, which is why this went unnoticed until a live subscription
  investigation traced the exact `Cookie` header sent on the wire.
  Re-validated live end to end on a freshly provisioned fabric: create,
  modify, delete, filtered subscriptions, forced reconnect, and refresh
  escalation all deliver correctly now.

## [1.3.0] — 2026-07-20

Native APIC object-subscription: a query becomes a live push stream instead
of a one-off read, over the same WebSocket mechanism the APIC GUI itself
uses. Purely additive — the configuration API and the query surface are
unchanged.

### Added

- **`Query.subscribe()` / `AsyncQuery.subscribe()`.**  Any single-class query
  can be subscribed instead of fetched: `.initial` gives the synchronous
  snapshot, then the returned `Subscription`/`AsyncSubscription` iterates
  live push events for as long as it stays open. One shared WebSocket per
  session multiplexes every subscription; refresh and reconnect run
  automatically in the background — nothing here needs a caller-driven loop.

- **Typed events.**  Each item is a `SubscriptionEvent`: `.kind`
  (`EventKind.CREATED`/`MODIFIED`/`DELETED`/`GAP`/`REFRESH_FAILED`), `.mo`
  deserialised through the same readable field names a normal read uses,
  with `.mo.model_fields_set` reporting exactly what that push carried (the
  APIC sends sparse deltas on `MODIFIED`, `dn` only on `DELETED`).

- **Automatic recovery, never silent.**  The APIC has no replay mechanism at
  all, so a reconnect resubscribes everything from scratch and delivers a
  `GAP` event rather than continuing as if nothing happened. Two consecutive
  missed refreshes trigger the same kind of recovery for that one
  subscription. `SubscriptionLostError` — with `.reason` — is raised only
  once recovery itself has been tried and failed.

- **Bulk and single-subscription tools.**  `aci.subscriptions.list()` /
  `.refresh_all()` / `.close_all()` manage every subscription open on a
  session at once (`close_all()` stops them without tearing down the shared
  socket); `sub.info` / `sub.refresh_now()` do the same for one subscription.

- Validated live against a real fabric: genuine create/modify/delete push
  payloads, a real `subscriptionId`, and the `subscriptionRefresh` endpoint
  accepting a real id.

## [1.2.0] — 2026-07-20

Discovery for the ~15,300 ACI classes the SDK does not generate a model for —
learned endpoints, stats, hardware, routing runtime. Purely additive: the
configuration API and the 1.1.0 query surface are unchanged.

### Added

- **The read catalogue.**  A shipped, lazily-opened sqlite store (~31 MB) of
  read metadata for every readable ACI class, not just the 2,239 with generated
  models — so any class can be searched, described, and read with readable
  field names.

  Readable names are recomputed with the code generator's own naming logic, so
  a catalogue-served class reads with the same field names its model would
  use — **except for ~0.07 % of properties on a handful of generated classes**
  (11 properties across 7 of 2,211 on APIC 6.0(9c)), where the catalogue
  resolves name collisions over a class's whole readable property set while
  the model resolves over its configurable subset, so the two can pick
  different names (e.g. ``l3extOut.enforceRtctrl`` → ``enforce_route_control``
  vs ``enforce_rtctrl``).  This never affects a result object — a generated
  class is served by its typed model, never the catalogue — and is visible
  only when introspecting those classes.

- **`niwaki.catalog`** — the public door to it: `search(term)`,
  `describe(class_name)`, `prop_meta(class_name, name)`, `find_prop(term)`,
  `concrete_subclasses(class_name)`, `class_meta(class_name)`, and
  `fault_name(code)` (a fault code to its rule name, independent of which class
  raised it). Entirely offline — no APIC connection needed.

- **Readable field access on any result object.**  A `ManagedObject` built
  from a class with no generated model now exposes readable attribute names
  (`ep.infrastructure_ip`, not just `ep["address"]`) via the catalogue, with
  the same per-property coercion (`bool`/`int`/`float`/`flags`/…) the wire
  boundary uses for generated models — reading is uniform across all ~15,300
  classes, generated or not.

- Validated live: real non-generated classes (`topSystem`, `fabricNode`,
  `lldpAdjEp`, `eqptSensor`, `faultInst`), the abstract-class query fan-out
  (`aci.query("fvEPg")` resolving server-side to its concrete descendants), and
  `fault_name` against every fault code a live fabric actually raised.

## [1.1.0] — 2026-07-19

The **observation / query** surface — which 1.0.0 declared still-evolving — gets
its read foundation: the full APIC query grammar, expressed as a fluent, typed
API and validated against a live controller.  The **configuration** API (design
DSL, push modes) is unchanged and remains stable; the breaking changes below are
confined to reading.

### Added

- **Filter operators.**  `anybit`, `allbit` (emulated as `and_(anybit, …)`),
  `xor` and `raw` join `eq`/`ne`/`lt`/`le`/`gt`/`ge`/`bw`/`wcard` and
  `and_`/`or_`/`not_`.  `anybit` closes the write-but-not-filter gap on bitmask
  (`Flags`) fields — you can now filter "where this flag is set".
- **Smart values in `where(...)`.**  A list or tuple means membership, a `*` in a
  string means wildcard, a `set` stays bitmask equality; explicit wrappers
  `any_of(...)`, `like(pattern)` and `between(start, end)` remove any ambiguity.
- **Response shaping — full GET grammar.**  `self_only()`, `also(...)`,
  `subtree_full()`, and `include_subtree(...)` with the `SubtreeInclude` facets
  (faults, health, stats, audit/event/fault/health records, tasks, count, …);
  multi-key `order_by`.
- **Executors and Python protocols.**  A query is a lazy iterable
  (`for mo in q`, `list(q)`); `q[:n]` sets a server-side limit; `.one()` and
  `.exists()`; `execute_raw()`.  `count()` and `exists()` honor the limit.  New
  typed `NoResultError` / `MultipleResultsError`.
- **Uniform result access** on every object, generated model or operational
  class alike: `.dn`, `mo["wireName"]`, and `.attrs` (the full wire view).

### Changed

- **`with_faults()` no longer filters.**  It embeds faults on each object without
  restricting the result; chain `only_faulted()` to return only faulted objects.
  (Previously `with_faults()` implicitly restricted to faulted objects.)
- **`bool(query)` / `if query:` now raises `TypeError`.**  A query is lazy, so
  truthiness would hide a network call — use `.exists()`.
- **`subtree_where(prop=value)` qualifies the property with the included subtree
  class.**  `include(fvSubnet).subtree_where(scope=...)` filters `fvSubnet.scope`,
  not the queried class; when several classes are included, pass an explicitly
  qualified expression (`subtree_where(eq("fvSubnet.scope", …))`).

### Removed

- `contains()` and `isdigit()` filter operators — the APIC has no such filter
  types (verified on 6.0(9c)).  Use `like("*x*")` (subject to the property's
  format) or `raw(...)`.

## [1.0.0] — 2026-07-17

Niwaki leaves beta.  The design-first **configuration** surface is proven against
a live Cisco APIC: the SDK expresses the ACI configuration model in depth, and a
real controller accepts what it produces.

### Out of beta

- **Stable configuration API.**  The design DSL (`design`, `tenant`, `infra`,
  `fabric`, `controller`, `aaa`, the makers, `bind`, `bind_dn`, `ref`, the verbs),
  the push modes (`strict`, `staged`, `plan`) and the observation façade are now
  stable; breaking changes will land in a new major version with a migration note.
- Development-status classifier moved from **4 - Beta** to **5 -
  Production/Stable**.

### Changed

- **`bind()` no longer climbs to an ancestor.**  A relation attaches to the
  object the cursor is on — declare each alias on the level that owns it
  (`.bd("web").bind(vrf="prod").subnet(...)`, not
  `.bd("web").subnet(...).bind(vrf="prod")`).  Binding an alias the current level
  does not curate now fails loud — at the type level (the typed cursor no longer
  exposes the alias) and at runtime (`DesignError`) — instead of silently placing
  the relation on a parent object.
- **Typed cursor `bind()` signatures expose only the aliases curated on that
  level**, never an ancestor's, so the editor and type-checker reject a bind the
  controller would never accept.
- **Two references that resolve to the same relation now coexist** instead of
  raising: `vrf.bind(l3out="x")` and `l3out("x").bind(vrf="v")` build the same
  `l3extRsEctx` and collapse to one; a same-relation collision with *conflicting*
  attributes still raises.

### Proven on a live fabric

An exhaustive integration suite drives the SDK against a live APIC simulator,
organised as eight domain walkthroughs an operator would recognise —
fabric/access, fabric, tenant, contracts, external connectivity (L3Out / L2Out),
service graphs, observability and management.  Together they:

- push **more than 10,000 configuration objects** across **101 walkthrough files**
  and **225 test functions** — every one **accepted by the controller, with zero
  rejections**;
- sweep each object's configuration surface in depth — every enum value, the
  combinations of interacting fields, and **every curated child of every parent**,
  with mutually-exclusive settings factored across separate tenants, VRFs and
  bridge domains so both sides of each exclusion are covered;
- **encode the controller's real cross-field rules** that the schema does not
  express (one SPAN destination per session, NetFlow v9-only, OSPF/EIGRP mutual
  exclusion on an L3Out, redistribution route-maps permit-only, a backup policy
  serving a single redirect, …), so every pushed object is accepted *in context*,
  not merely syntactically valid;
- confirm that **every declared object** is present on the fabric with the
  attributes and children its design declared, and that every relation reads
  `state=formed`.

Residual faults a lab run may show are **deployment-layer**, not configuration
defects — a static path whose VLAN no domain binds yet, a routing peer with no
neighbour on the simulator, a VMM infra port-group without the fabric-wide
infra-VLAN scaffolding — and each is documented in its domain's walkthrough
README.  The SDK expresses every knob; the controller accepts every object.

### Scope

The walkthroughs run on a simulator, so they prove the **configuration surface** —
the SDK expresses the ACI model and a real controller accepts it — not hardware
or data-plane behaviour.  The **configuration** side of the SDK is
production-ready; the read / query and observation surfaces continue to grow.

### Verifying it yourself

Point the suite at your own lab and watch it configure a fabric end to end
(`uv run pytest tests/integration/<phase> -m integration -s`), then confront the
result through an **independent read path** — for example a read-only oracle over
the APIC such as [`aci-mcp`](https://github.com/k3l0-dev/aci-mcp) — to confirm
each object landed.  See
[`tests/integration/README.md`](https://github.com/k3l0-dev/niwaki/blob/main/tests/integration/README.md).

## [0.14.16] — 2026-07-17

### Added

- **`tests/integration/README.md`** — explains what the live walkthroughs are
  for (evaluating the SDK against a lab APIC / simulator) and states plainly that
  they are **not** production configuration and **not** best-practice snippets:
  their values are illustrative and their job is to confirm the code runs against
  a real controller.

### Fixed

- The generated coverage-matrix intro no longer hardcodes the number of design
  domains — it read "the four design domains" after further roots (`aaa`,
  `vmm_provider`) were added.

## [0.14.15] — 2026-07-17

### Added

- **Fabric switch profiles, module cards and vPC protection.**  The fabric root
  gains `leaf_path_selector`, `spine_path_selector`, `override_leaf_selector` and
  `override_spine_selector` makers; leaf and spine switch profiles bind their
  `module_profile` (FEX card); the vPC explicit protection group binds its
  `vpc_policy`; and switch event/fault/health retention policies are declarable.
- **Management and DHCP node-group associations.**  Management, DHCP and
  deployment-zone pod groups bind their node groups (`management_group`,
  `node_group`); DHCP node groups bind their `dhcp_relay` policy; DNS server
  groups bind their `epg`; and bridge domains gain a `dhcp_relay_label` maker.
- **Observability — monitoring sources, syslog destinations, SPAN and
  retention.**  EPG monitoring policies gain the SNMP/syslog/callhome/TACACS
  sources plus `lifecycle_policy` and `stats_limit_pol` (the latter two across
  every monitoring policy); syslog groups gain `console`, `file` and
  `protocol_profile` destinations; tenants declare VSPAN sessions and destination
  groups, whose destinations bind an EPG, path, APIC node or virtual port.

### Coverage

Declarable-surface coverage after these fixes — curated positions against the
remaining **in-scope** backlog, by design domain:

| Domain     |  Curated | In-scope gaps | Coverage |
| ---------- | -------: | ------------: | -------: |
| tenant     |      553 |           141 |     80 % |
| access     |      212 |            29 |     88 % |
| fabric     |      206 |            36 |     85 % |
| controller |       12 |             1 |     92 % |
| aaa        |       14 |             1 |     93 % |
| vmm        |       33 |             1 |     97 % |
| **Total**  | **1030** |       **209** | **83 %** |

The 322 detected gaps split into **209 in-scope**, **26 deferred** (VMM,
Intersight and on-switch third-party integrations — these need a live backend to
verify) and **87 out of scope** (imperative actions, Cloud Network Controller,
Nexus Dashboard Orchestrator / multi-site, and SD-WAN).  Anything outside the
curated vocabulary stays reachable through `.mo(Class, ...)` and
`bind_dn(alias=dn)`.

## [0.14.14] — 2026-07-17

### Added

- **VRF, bridge-domain and external-EPG protocol relations are now declarable
  (tenant).**  Bridge domains bind `fhs`, `nd_policy`, `igmp_snoop`, `mld_snoop`,
  `dhcp_relay`, `endpoint_retention`, `netflow_monitor`, `monitoring_policy` and
  `flood_filter`; VRFs bind `bgp_timers`, `bgp_address_family`,
  `eigrp_address_family`, `endpoint_retention`, `route_tag`,
  `route_control_profile`, `vrf_validation`, `monitoring_policy` and the
  `ospf_timers` verb (per-address-family).  L3Out external EPGs gain
  `imported_contract` and the `intra_epg` verb; in-band management EPGs gain
  `taboo_contract` and `intra_epg`.
- **L3Out route-control, QoS and SR-MPLS relations (tenant).**  The L3Out gains
  the `dampening`, `interleak` and `redistribute` verbs (three route-control
  profiles that automatic resolution cannot tell apart); logical interface
  profiles gain `ingress_dpp` / `egress_dpp` (data-plane policing); node
  attachments gain the SR-MPLS `node_sid_profile` maker; path attachments gain
  `secondary_ip_address` and `member_node_configuration` makers.
- **Contract service-graph attachment and in-band management contract labels.**
  Contracts and subjects bind `service_graph` (`vzRsGraphAtt` /
  `vzRsSubjGraphAtt`); in-band management EPGs gain the six contract and subject
  labels (`consumer_contract_label`, `provider_label`, …).

## [0.14.13] — 2026-07-17

### Changed

- **SNMP trap-forward-server maker renamed to `trap_forward_server`.**  Under
  `fabric().snmp_policy(...)`, the maker that declares an `snmpTrapFwdServerP`
  was called `client_entry` — the same word as the SNMP client-group's client
  entry, which was misleading.  Use `.trap_forward_server(<address>)`.

### Added

- **Local AAA & security objects are now declarable under `aaa()`.**  Alongside
  `radius()`, the `uni/userext` root makes local users (`local_user`), remote
  users (`remote_user`), roles (`aaa_role`), security domains (`security_domain`)
  and login domains (`login_domain`), plus the fabric-security singletons
  `password_strength_policy`, `block_user_logins_policy`, `pre_login_banner`,
  `fabric_sec` and `service_node_cluster_settings`.
- **Interface policies attach to PC/vPC policy groups, and switch selectors to
  their policy groups (access-binds).**  `infra().func_profile().port_channel(...)`
  now binds `fc_interface`, `l2_mtu`, `port_authentication`, `port_security`,
  `link_flap`, `monitoring`, `netflow_monitor`, `optics`, `slow_drain`, `synce`,
  `span_destination_group` and `span_source_group`, plus the `ingress_dpp` /
  `egress_dpp` verbs for data-plane policing (two relations to one `qosDppPol`).
  Leaf and spine switch selectors bind `policy_group` (`infraRsAccNodePGrp` /
  `infraRsSpineAccNodePGrp`) — the link that attaches a switch profile to its
  switch policy group.
- **Associated management EPG is now declarable on SNMP client groups, SNMP
  trap destinations, syslog remote destinations, NTP providers, and DNS
  profiles.**  `snmp_client_group_profile(...)`,
  `snmp_monitoring_destination_group(...).snmp_trap_destination(...)`,
  `syslog_group(...).remote_destination(...)`,
  `datetime_policy(...).ntp_provider(...)`, and `fabric().dns_profile(...)`
  accept `bind_dn(management_epg="uni/tn-mgmt/mgmtp-default/oob-default")` — the
  `snmpRsEpg` / `fileRsARemoteHostToEpg` / `datetimeRsNtpProvToEpg` /
  `dnsRsProfileToEpg` relation to the associated (OOB or in-band) management EPG.
- **AAA / RADIUS is now declarable.**  A new top-level `aaa()` root — a sibling
  of `controller()`, mirroring the MIT's `uni/userext` branch — with `radius()`,
  `radius_provider(...)`, `radius_provider_group(...)` and its provider members.
  The 802.1x node-auth policy can point at a group with
  `dot1x_node_authentication(...).bind_dn(radius_provider_group=<dn>)`.
- **Maintenance groups can reference their maintenance policy** —
  `fabric().maintenance_group(...).bind(maintenance_policy=<name>)` creates the
  `maintRsMgrpp` relation to a `maintMaintP`.
- **NTP authentication keys are now declarable** —
  `datetime_policy(...).ntp_auth_key(<id>, key=..., type_of_authentication_key=...)`
  creates a `datetimeNtpAuthKey`, and
  `datetime_policy(...).ntp_provider(...).authentication_key(<id>)` trusts one on
  a provider (the `datetimeRsNtpProvToNtpAuthKey` relation, keyed by the key id).

## [0.14.12] — 2026-07-16

### Fixed

- **A staged push now isolates a failure to its own subtree.**  When one object
  failed, `push(mode="staged")` marked *every* deeper object as "not attempted"
  — keyed on DN depth alone — so an unrelated sibling branch whose parent had
  succeeded was left half-built (e.g. a failure on `BD-a` skipped `BD-b`'s
  subnet).  The engine now skips only the descendants of a failed object;
  independent branches run to completion, and `StagedPushError.not_run` lists
  only objects whose ancestor genuinely failed.

## [0.14.11] — 2026-07-16

### Fixed

- **Concurrent mid-session 401s no longer stampede re-logins (async).**  When
  many coroutines shared an `AsyncApicSession` whose token was revoked, each
  received a 401 and called `login()` directly — outside the token lock and
  semaphore — so *N* coroutines raced *N* concurrent re-logins on the shared
  token state and cookie jar.  Reactive re-login now goes through a
  lock-guarded path that re-authenticates only once (the first coroutine in;
  the rest see the fresh token and replay), and the replay runs under the
  concurrency semaphore.

## [0.14.10] — 2026-07-16

### Fixed

- **References resolve to the nearest scope, not globally.**  A name reused
  across tenants (a `bd("web")` or `vrf("prod")` in two tenants) previously made
  that name unbindable — every `bind()` targeting it raised `AmbiguousBindError`,
  even though ACI namespaces object names per parent.  A reference now resolves
  to the same-named target sharing the deepest enclosing scope with its owner
  (a BD in tenant *a* binds tenant *a*'s VRF); only two candidates at the *same*
  scope remain a genuine ambiguity.

## [0.14.9] — 2026-07-16

### Fixed

- **Writes are no longer retried after a timeout.**  A read/write timeout can
  mean the APIC already accepted the `POST`/`DELETE`, so retrying risked a
  double-apply or a spurious `NotFoundError` on a delete that actually
  succeeded.  Writes now retry only on pre-send errors (connection/pool); reads
  retry on any transport error as before.

## [0.14.8] — 2026-07-16

### Fixed

- **Query filter values are escaped.**  A `"` in a `where(prop=value)` / `wcard`
  value is now escaped instead of breaking the `eq(prop,"...")` filter grammar
  (which could 400 or silently match the wrong set).
- **Pagination no longer stops after page 0 when `totalCount` is absent.**  A
  missing/zero `totalCount` is treated as "unknown" and pages continue until an
  empty page, instead of being read as "no more pages".
- **`PushReport.request_count` on a failed staged push** now counts the requests
  actually issued, not the full op list.
- **`gather()` docstring** corrected to `TaskGroup` semantics — the first raise
  cancels in-flight siblings, so it must not be used for concurrent writes.

## [0.14.7] — 2026-07-16

### Fixed

- **`plan` no longer reports a change `push` never makes.**  `to_apic()` drops an
  empty string on a non-naming field (sending `""` would clobber the APIC value),
  so `push` never sends it — but `mo_diff` still compared it, so a design with a
  field set to `""` produced a plan `update` that never applied and never
  converged.  `mo_diff` now mirrors the `to_apic` rule (found in a runtime audit).

## [0.14.6] — 2026-07-16

### Fixed

- **`protocol="icmpv6"` filter-entry sugar** now defaults `ethernet_type` to
  `ipv6` (was `ip`), since ICMPv6 exists only over IPv6.  `tcp=` / `udp=` keep
  the generic `ip` ether-type, which already matches both IPv4 and IPv6 — no
  extra `ethernet_type` is needed for an IPv6 port filter.

## [0.14.5] — 2026-07-16

### Fixed

- **Readable names on the high-traffic classes** (batch 3, from a deep audit):
  `fvSubnet.virtual` / `.preferred`, `vzEntry.apply_to_frag` / `.match_dscp`,
  `l3extSubnet.aggregate` / `.scope`, and `fvBD.optimize_wan_bandwidth` /
  `.intersite_bum_traffic_allow` / `.service_bd_routing_disable`.  The
  `l3extSubnet.scope` rename also makes `scope` consistent across the three
  subnet-like classes (it was `scope_of_the_external_subnet`).

## [0.14.4] — 2026-07-16

### Fixed

- **Readable names for 45 more sentence-labelled fields** (batch 2): the
  telemetry FTE event fields (`telemetryFteEventSetP.drop_flow_count`, …),
  `fvBD.limit_ip_learn_to_subnets` / `.mcast_arp_drop`, `fvTagSelector.match_key`
  / `.match_value`, `fvVmAttr.value`, `l3extRogueExceptMacP.enable_all_macs`,
  `l3extVrfValidationPol.enable_vrf_validation_*`, `ptpProfile.node_profile_override`
  / `.delay_intvl`, `qosLlfcIfPol.llfc_rcv_admin_st` / `.llfc_send_admin_st`, and
  `bfdIpv4InstPol` / `bfdIpv6InstPol.echo_src_addr` — the last also sheds a
  mislabelled "ipv4" that the schema stamped on the *ipv6* policy.  Cryptic wire
  names (`qiqL2ProtTunMask`, the flash counters) are deliberately left as-is.

## [0.14.3] — 2026-07-16

### Fixed

- **Readable names for 20 everyday config knobs.**  Fields whose ACI schema
  label is a full sentence (which slipped under the codegen's length cap) now
  take their wire prop name instead: `bgpPeerP.weight` / `.connectivity_type`,
  `bgpInfraPeerP.weight`, `bgpCtxAfPol.max_local_ecmp`, `l2IfPol.vlan_scope`,
  `l2PortSecurityPol.maximum` / `.timeout`, `hsrpGroupP.mac`,
  `hsrpGroupPol.preempt_delay_reload`, `l3extDefaultRouteLeakP.scope`,
  `infraPortTrackPol.minlinks`, `l4VxlanInstPol.udp_port`, `ospfCtxPol.max_lsa_num`,
  `isakmpKeyring.address`, `mplsNodeSidP.loopback_addr`,
  `fvCepNetCfgPol.start_ip` / `.end_ip` / `.dns_suffix` / `.dns_search_suffix`,
  `infraSetPol.enforce_subnet_check`.

## [0.14.2] — 2026-07-16

### Fixed

- **Dropped the `dscp_translation_policy` tenant maker.**  `qosDscpTransPol` is a
  `never`-creatable global singleton that exists only at
  `uni/tn-infra/dscptranspol-default`, so the per-tenant maker always failed
  (HTTP 400) under any user tenant.  Configure the infra default via
  `.mo(qosDscpTransPol, ...)` if needed.

## [0.14.1] — 2026-07-16

### Added

- **VRF route targets.**  `vrf(name).route_target_profile(af)` (per address
  family, `ipv4-ucast` / `ipv6-ucast`) with `route_target(value, "import")` /
  `route_target(value, "export")` — the BGP route targets that map a VRF into an
  MPLS-VPN, EVPN or SR-MPLS hand-off.  Completes the tenant-side SR-MPLS VRF
  L3Out: consumer label, route targets, and import/export route maps.

### Fixed

- **SR-MPLS VRF L3Out.**  `l3out(name, mpls_enabled=True)` now marks an L3Out as
  a SR-MPLS VRF L3Out (the `mplsEnabled` flag) rather than a classic L3Out.
- **Field name.**  `l3extOut`'s MPLS flag is now `mpls_enabled` (its schema
  label — a full sentence — had produced an unusable name).

## [0.14.0] — 2026-07-15

L4-L7 service graphs join the vocabulary — the last large domain of the ACI
configuration plane.

### Added

- **Service graph templates.**  `tenant(name).service_graph(...)` with function
  nodes (+ function connectors carrying config folders and parameters, copy
  connectors), connections, and consumer/provider terminal nodes.
- **Logical devices.**  `logical_device` (the L4-L7 cluster) with concrete
  devices (+ their interfaces and parameters), logical interfaces, credentials
  and management interface; the graph's function node binds a logical device.
- **Device context.**  `logical_device_context` (keyed by contract/graph/node)
  selecting a device and router configuration, with per-connector interface
  contexts mapped to bridge domains and their virtual IPs.
- **Function profiles.**  Profile container → group → profile with function,
  device and group shared configs, and the abstract folder/parameter model.
- **Device manager, chassis and instance config.**  Device manager and chassis
  (with credentials), the deployed L4-L7 policy container with folder/parameter
  instances, and normalized firewall parameters.

Service graphs define the ACI-side topology and configuration; rendering a graph
needs a real L4-L7 appliance — see the "Hardware-dependent integrations" note.
The device-package metamodel and normalized LB/NAT requests stay uncurated.

### Coverage

The declarable config plane across the five domains (operational, diagnostic and
out-of-scope families — cloud, multi-site/NDO, device-package meta — excluded):

| Domain             | Declarable | Curated |        % |
| ------------------ | ---------: | ------: | -------: |
| Tenant             |        368 |     318 |     86 % |
| Access (`infra`)   |        164 |     141 |     85 % |
| Fabric             |        179 |     145 |     81 % |
| Controller         |         20 |       9 |     45 % |
| VMM                |         22 |      15 |     68 % |
| **Global (union)** |    **753** | **628** | **83 %** |

790 curated positions across 652 distinct classes.

## [0.13.0] — 2026-07-15

VMM domains join the vocabulary, and the push engine learns to fold the
plugin-managed path prefixes they need.

### Added

- **VMM domains.**  `design().vmm_provider(vendor).vmm_dom(name)` with its
  vCenter/SCVMM controller (cluster controller, host-availability with
  protect-VM group and host-desired-state, EP-validator), credentials, vSwitch
  policy group (enhanced LACP) and uplink container/policies, plus the domain's
  EPG aggregators.  The domain and vSwitch container bind their default
  interface policies and pools; the AAEP's abstract `domain` bind now resolves
  to a declared VMM domain, closing the access-domain loop.
- **Carrier classes.**  A curated `carrier` set names non-creatable, path-only
  classes the APIC rejects on a standalone POST or `rsp-subtree` read (a VMM
  provider, `uni/vmmp-VMware`).  The staged push emits no op for them — their
  declared children post at their full DNs and the APIC materialises the path —
  and the plan diffs those children instead.

### Notes

- Pushing a VMM domain lands the APIC-side config and re-plans cleanly, but a
  reachable vCenter / SCVMM controller is required before inventory syncs — see
  the new "Hardware-dependent integrations" note in the design-first guide.  The
  VMM orchestrator provider (NDO) stays out of scope.

## [0.12.0] — 2026-07-15

The fabric-policy (`fabric`) and controller (`controller`) domains join the
tenant and access-policy planes as first-class, live-verified vocabulary, and
non-creatable default singletons finally read as the singletons they are.

### Added

- **Fabric policies (six waves).**  The fabric-internal ports charpente
  (leaf/spine switch, interface and module profiles, selectors, policy groups,
  pod profile); fabric interface and protocol policies (link-level, link-flap,
  L3, L2 MTU, MACsec fabric, ISIS, COOP, fabric VXLAN, vPC domain, PSU
  redundancy, WWN, load-balance, ZR/ZRP/DWDM optics, node control); fabric
  monitoring (callhome/SNMP/TACACS destination groups, SNMP policy, fabric and
  common monitoring policies with their sources); system and global policies
  (communication services, geo-location hierarchy, proxy, datetime format,
  connectivity preference, admin-down, deployment, out-of-service); firmware,
  maintenance and config management (policies, groups, catalogs, export/import/
  snapshot/rollback, scheduler, license); telemetry, analytics, TWAMP, core/
  techsupport export, latency modes and fabric VSPAN.
- **Controller policies.**  Cluster, audit-log retention, controller firmware
  and maintenance, DRR, fabric first-time setup (+ per-pod), scheduler, cores
  and CIMC-node policies, alongside the existing fabric membership.
- **Singleton-aware makers.**  APIC creatability is baked into every generated
  model (`_is_creatable`).  A maker whose target is a non-creatable, name-only
  default singleton now defaults its name to `"default"` — `.qos_instance_
  policy()`, `.communication_policy("default")`, `.coop_group_policy()` read as
  the singletons they are, configuring the existing instance in place.  Spanning
  Tree (MST) rejoins the access vocabulary on this basis.

### Notes

- Deprecated or feature-restricted classes are omitted where the 6.0 APIC
  rejects them (telnet service, telemetry server groups, SD-WAN SLA).  Kafka
  policy and multi-domain (NDO) stay out of scope.

## [0.11.0] — 2026-07-15

The access-policy (`infra`) configuration surface is now substantially complete.
The fabric's physical side — pools, domains, policy groups, interface and switch
policies, QoS and control-plane protection, fabric-wide system settings, and
observability — is first-class vocabulary, typed and live-verified against a
6.0(9c) fabric.

### Added

- **Pools and the Fibre Channel domain.**  VXLAN, VSAN and multicast-address
  pools with their ranges; the FC domain binding its VLAN/VSAN/address pools and
  VSAN attributes.
- **Policy groups and profiles.**  Leaf/spine switch groups, the spine access
  group, PC/vPC override, the FC port/PC/PC-override groups, breakout group and
  modular-card group; the spine interface profile with its port selector; FEX,
  pod and access-module profiles with their selectors and blocks.
- **Interface policies.**  L2 interface, LACP member, PoE, FC, MACsec (container
  with parameters/keychain/key policies), SyncE, link-flap and 802.1x
  port/node authentication; the PoE/FC/SyncE instance and fabric policies.
- **QoS and control-plane protection.**  The QoS instance policy and its six
  classes with per-class buffer, congestion, priority-flow-control, queue,
  schedule and microburst policies; interface LLFC/PFC/slow-drain; CoPP
  leaf/spine and per-interface policies; the CoPP prefilter with its ACL entries.
- **Fabric-wide and system policies.**  CP/controller MTU, TCP MSS, fabric-wide
  settings, port tracking and status, forwarding-scale profile, USB
  configuration, fast link-failover, flash configuration, remote-leaf pod
  redundancy, system GIPo, infrastructure zoning; DHCP relay node/pod groups,
  node/pod management addresses and the managed-node connectivity group.
- **Observability and timing.**  The monitoring policy with syslog/SNMP/callhome/
  smart-callhome/TACACS sources and fault/event severity assignment; PTP node
  policy, profile, domain and template; the four global BFD policies; the
  NetFlow node policy; VSPAN sessions and destination groups.
- **Policy-group wiring.**  The interface and switch policy groups now bind every
  relevant policy above (CoPP, QoS, MACsec, BFD, PTP, monitoring, and the rest).

### Notes

- Non-creatable fabric singletons — the QoS instance/classes, CP MTU, TCP MSS,
  fabric-wide settings, port tracking/status, system GIPo, and the zoning
  profile — are configured in place through their makers rather than created.

## [0.10.0] — 2026-07-15

The tenant's configuration surface is now substantially complete.  A large body
of tenant protocol and policy configuration that previously needed the `.mo()`
escape hatch is first-class vocabulary — typed makers, per-position reference
pages, live-verified against a 6.0(9c) fabric.

### Added

- **Multicast.**  VRF-level PIM (`.pim`), IPv6 PIM (`.pim6`) and IGMP (`.igmp`)
  with the full RP, pattern and filter policies; BD-level PIM with its route-map
  filters; the PIM/IGMP interface-policy filters; IGMP/MLD snooping groups.
- **Route-control and leaking.**  Route-map `match_*` and `set_*` clauses on the
  match rule and action profile, inter-VRF route leaking (`leak_routes`), and
  static routes with their next hops.
- **L3Out.**  External connectivity labels, node loopbacks and infra nodes,
  path-level forwarding and rogue-exception MAC, VRF validation and a global VRF
  name.
- **Security and VPN.**  Host protection (microsegmentation) with its subject →
  rule tree, and site-to-site IKE/IPsec (`isakmp_*`, `ipsec_phase1/2`); port
  security on a static path.
- **Protocol policies.**  DNS server groups, tenant AAA server groups, SNMP
  contexts and communities, QoS class mappings, Fibre-Channel uplink pinning,
  SR-MPLS node SIDs and SRGB, ND RA subnets, HSRP secondary VIPs, micro-BFD,
  PTP, BGP data-plane, DHCP relay gateway IP, and virtual SPAN.
- **Endpoints and pools.**  Anycast and NLB endpoints, IP address-management
  pools, VRF route summarization and deployment, uSeg BD associations, ESG
  LIfCtx selectors.

Curation coverage of the tenant's declarable configuration rose from roughly 40%
to 85%, and the reference now documents 462 curated positions (up from 293).

### Notes

- Multi-site / intersite objects (managed by Nexus Dashboard Orchestrator),
  Cloud APIC classes, orchestrator-injected config, learned endpoints and L4-L7
  service graphs remain out of the tenant vocabulary by design; the `.mo()` and
  `bind_dn()` escapes keep the rest of the 2,222 generated classes one call away.
- A handful of tenant classes the APIC auto-manages and refuses to create
  (`extdevSDWanPolCont`, `fvConnInstrPol`) are deferred until the push engine
  can upsert such carriers.

## [0.9.0] — 2026-07-15

The models now carry the **right Python type** for every field, and the
type checker sees it.  Before this release the code generator knew only a
handful of schema types and quietly rendered everything else as `str`; that
made numbers into text, made bitmasks unusable, and hid the readable field
names from your IDE.

### Changed

- **Numbers are numbers.**  A field the schema declares numeric is now `int`
  or `float`, with the schema's own bounds — not a string.  A field the APIC
  stores under a *name* (a filter port, a BGP stale interval) accepts the
  number and canonicalises the way the APIC does: `vzEntry(destination_from_port=80)`
  round-trips as `"http"`, so `push(mode="plan")` finally converges on it.
- **A bitmask is a set.**  A field that is a subset of a closed set (a subnet
  `scope`, `vzEntry` `tcp_rules`, a LACP `ctrl`) is now `frozenset[SomeEnum]`.
  It accepts everything reasonable — the wire string, a set of names, a set of
  members, a single flag — and order never matters, so the phantom drift the
  APIC's own re-ordering used to cause is gone:

  ```python
  fvSubnet(ip="10.1.1.1/24", scope="public,shared")  # the wire form
  fvSubnet(ip="10.1.1.1/24", scope={"public", "shared"})  # a set of names
  vzEntry(name="ssh", tcp_rules="syn,ack")  # was rejected before
  ```

- **Addresses are validated.**  IPv4 and IPv6 fields carry an address pattern
  instead of accepting any string.
- **Readable names reach your IDE.**  A renamed field (`arp_flooding` for
  `arpFlood`) is now accepted *by that name* by type checkers — Pylance and
  pyright no longer flag `fvBD(name="web", arp_flooding=True)` while accepting
  the wire spelling.  The wire name still works on reads and in query filters.

### Added

- `niwaki.query(cls)` is overloaded: a model class in gives typed instances
  out; a class *name* string in gives base `ManagedObject`s (their attributes
  in `model_extra`).
- `ref()` is accepted by `bind_dn()` and by the contract verbs, not only by
  `bind()`.
- The generated reference now documents every field's real type and, for an
  enum or a set of flags, its allowed values and default.

### Migration

- A field you *read back* may now be a number or a `frozenset` where it used
  to be a string — compare against `80`, not `"80"`, and against
  `{"public", "shared"}`, not `"public,shared"`.  Construction is unchanged:
  the wire string is still accepted everywhere.
- A bitmask default is a `frozenset`; a numeric field's default is a number.
- One long-standing default was corrected: `bgpBestPathCtrlPol.ctrl` defaulted
  to `as-path-multipath-relax` **enabled** and now defaults to no flags, as the
  schema declares.  `ospfExtP` regained its `area_ctrl` field, previously
  dropped by a name collision.

### Internal

- pyright now type-checks the whole repository (the generated tree excepted) in
  the commit gate and CI, alongside mypy — it reads the constructor signature
  Pydantic synthesises, which mypy does not.  A cold-start budget and a
  documentation type-column guard were added.

## [0.8.0] — 2026-07-14

### Added

- **The EPG/ESG world enters the vocabulary.**  An application EPG now reaches
  everything the APIC hangs under it:
  `subnet()` (with its `l3out` and `nd_ra_prefix_policy` binds),
  `static_endpoint()` (plus `static_ip()`, and the path/node it lives on),
  `criterion()` — the uSeg selector, with `ip_attribute()`, `mac_attribute()`,
  `vm_attribute()`, `dns_attribute()` and nested `sub_criterion()` —
  `virtual_ip()` for L4-L7 VIPs, and `fc_path()` for Fibre-Channel paths.
- **Endpoint security groups**: `app().esg()` with its selectors
  (`ep_selector()`, `epg_selector()`, `tag_selector()`), its mandatory
  `vrf` bind, and the contract verbs.
- New EPG binds: `contract_master` (contract inheritance — one alias, EPG or
  ESG alike), `imported_contract`, `taboo_contract`, `custom_qos_policy`,
  `dpp_policy`, `monitoring_policy`, `trust_control_policy`; and the tenant
  objects they point at: `taboo_contract()` (with its `subject()`),
  `imported_contract()` and `monitoring_policy()`.
- A third contract verb, `intra_epg()` (`fvRsIntraEpg`), on EPGs and ESGs.
  Contract verbs are now fully data-driven: curating one in the vocabulary is
  enough — the runtime no longer hardcodes the list.

- **The contract world completes** (229 curated positions).  `vrf().vzany()`
  arrives — contracts for a whole VRF, reached through relation classes of its
  own (`vzRsAnyToProv` / `vzRsAnyToCons` / `vzRsAnyToConsIf`), which the
  data-driven verbs absorb without a line of engine code.  A subject that stops
  applying both ways gets one filter per direction with `in_term()` and
  `out_term()`; `exception()` excludes an EPG from a contract, on the contract
  or on the subject; `oob_contract()` covers out-of-band (management) contracts
  with their own subjects.
- The six contract labels (`provider_label()`, `consumer_label()`,
  `provider_subject_label()`, `consumer_subject_label()`,
  `provider_contract_label()`, `consumer_contract_label()`) are curated
  wherever the MIT hangs them — EPG, ESG, vzAny, subject and external EPG — so
  the `provider_label_match_criteria` attribute finally has labels to compare.

- **`ref()` — a reference can configure the relationship itself.**  Most
  relations are pure edges, but 26 curated binds resolve to a class that
  carries fields of its own, and they were unreachable: the resolution
  immediacy of an EPG-to-domain attachment, the `directives` of a filter under
  a subject (this is where contract logging lives), the `direction` of a
  route-control profile, a node's management address, an ERSPAN collector's
  IP.  Wrap the target — `epg.bind(domain=ref("prod-phys",
  resolution_immediacy="immediate"))` — anywhere a plain name goes, including
  `bind_dn()` and the contract verbs.  The fields are validated against the
  relation class at declaration time.

- **Observability**: SPAN (`span_source_group()` with its sources, label and
  filter group; `span_destination_group()` with its destinations), NetFlow
  (`netflow_monitor()`, `netflow_exporter()`, `netflow_record()`) and QoS
  requirements (`qos_requirement()`, with `ingress_dpp()`/`egress_dpp()` and
  the EPG bind that reaches it).  SPAN and NetFlow are curated under the
  tenant, under `infra` and under `fabric` alike.
- **The L2 edge and management**: `l2out()` complete (node profile, interface
  profile, static path, external EPG with labels and contract verbs), the
  in-band and out-of-band management EPGs — which give the out-of-band
  contract someone to provide and consume it — endpoint tags (what an ESG
  `tag_selector` matches), IP address pools, and fallback route groups.
- **The closed world is closed**: every curated `bind()` now has a declarable
  target, except the ones the fabric discovers for you (`fabricNode`,
  `fabricPathEp`), which is what `bind_dn()` is for.  293 curated positions,
  up from 176 at the start of the wave.

### Changed

- **Renamed, on an L3Out external EPG**: the two subject-label makers were
  curated before those classes had a name of their own and carried a generated
  one.  They now speak the same word as everywhere else:
  `.vz_prov_subject_label(...)` → `.provider_subject_label(...)` and
  `.vz_cons_subject_label(...)` → `.consumer_subject_label(...)`.  Their
  reference pages moved with them.
- A verb's parameter is named after what it points at (`provide(contract)`,
  `ingress_dpp(dpp_policy)`), and its flavor is read off the relation class
  rather than assumed to be name-flavored.  Existing call sites are unaffected:
  `provide(contract)` keeps its exact signature.

## [0.7.0] — 2026-07-13

### Added

- The models now carry the APIC's own catalog of accepted-but-inconsistent
  configuration states: 98 classes declare a read-only `configIssues` enum
  in the schemas (~2,500 codes — `fvBD` alone lists
  `FHS-enabled-on-l2-only-bd`), previously invisible to users.  Each such
  class exposes `_config_issues` (`{code: description}`) and lists the
  codes in its docstring — the states your IDE can warn you about before
  the APIC flags them.  Descriptions come from the value's `comment`
  (rich prose) with the schema `label` as fallback — every code is
  described.
- Two more declared constraint channels reach the models: `_fault_codes`
  (659 classes, 739 F-codes — `fvBD` carries
  `fltFvBDMulticastEnabledOnL2BD`) and `_relation_info` on relation
  classes (cardinality, enforceable, resolvable).  Both are guarded by
  an anti-drift integrity suite that re-derives them from the raw
  schemas for all 2,222 generated classes.

- **The DSL reference**: the generated vocabulary book becomes a full
  reference — one page per curated position with an attribute table
  (parameter, wire alias, type, allowed values, default, Cisco's
  description), the children/binds/verbs it reaches, and the APIC
  diagnostics (config issues, fault codes) it can raise.  Plus a page of
  the 106 enums the vocabulary uses (each value with Cisco's meaning) and
  the read-side navigation vocabulary.  The typed keyword arguments of
  every maker — the SDK's core surface — were previously visible only by
  hovering in an IDE.

### Changed

- The transport boundary is public: `niwaki.transport` exports the four
  structural protocols (`MoWriter`, `MoReader`, and their async mirrors)
  and both sessions (`ApicSession`, `AsyncApicSession`) — the extension
  point the testing guide relies on no longer lives in a private module.
- The API reference renders objects under their real import path
  (`niwaki.Niwaki`, not `niwaki.facade.Niwaki`), no longer exposes the
  models' private ClassVars as public attributes, and gained the entries
  it was missing: the root package, `mo_diff` / `parse_imdata`,
  `REGISTRY`, the filter operators, and vocabulary navigation.
- Deep anchors of the vocabulary pages moved: each position now has its
  own page (`reference/vocabulary/tenant/tenant-bd.html`) instead of an
  anchor on the domain page.

## [0.6.0] — 2026-07-13

A professional documentation overhaul, and the enterprise-CA answer.

### Added

- `verify_ssl` accepts a **path to a CA bundle** (PEM) on `Niwaki`,
  `AsyncNiwaki` and both sessions — TLS verification against a private
  or enterprise CA no longer requires disabling verification (the
  bundle loads eagerly into an `ssl.SSLContext`; a wrong path fails at
  construction).
- Three documentation pages the adopting coder was missing: **Testing
  your automation** (payload asserts, plan as a convergence test, a
  fake APIC at the httpx boundary, transport-protocol stubs — all
  executable), **Compatibility & limits**, and **Troubleshooting
  connection & auth** (the exception → question → knob ladder).
- The documentation home is a real landing page (orientation, a
  "Start here" path, the Diátaxis compass), every guide page ends with
  next steps, and deletion semantics have a canonical section.

### Fixed

- The `plan` documentation described pre-0.3.0 behaviour: plan reads
  are scoped with `rsp-subtree-class` to the classes the design
  declares — the stale "avoid planning large domains" advice is gone,
  and the write-only-secrets caveat now lives where `plan` is taught.
- One maxim ("structure is literal, vocabulary is translated"), one
  term per concept, position counts generated straight from the
  vocabulary — the terminology and numbers can no longer drift.
- Cisco placeholder comments (the literal text "null", on 621 schema
  properties) no longer leak into maker Args sections and field
  descriptions — those fields simply stay undescribed.

## [0.5.0] — 2026-07-13

Cisco's own definitions, everywhere the IDE looks.

### Added

- The APIC schema comments — Cisco's human-written definitions, covering
  79% of configurable properties and 84% of classes — now flow through
  the entire generated surface:
  - every described model field carries `Field(description=...)`: IDE
    hover, Pydantic error context and Sphinx autodoc all show Cisco's
    definition;
  - model class docstrings carry the class definition;
  - enum members carry per-value docstrings (`OspfNwT.BCAST` —
    "Broadcast interface");
  - every DSL maker exposes a generated Args section: field definition,
    allowed enum values and non-empty defaults, straight from the
    schemas.
- Wire behaviour is untouched (golden payloads pass unchanged) and
  cold-start stays at ~90 ms — models remain lazily loaded.

## [0.4.0] — 2026-07-12

The vocabulary triples and the whole delivery pipeline matures.

### Added

- **L3Out, complete** (wave 1): node and interface profiles, node/path
  attachments as literal-DN makers, BGP peers with ASN and prefix
  policies, OSPF/EIGRP/HSRP/PIM/IGMP/BFD/MPLS interface profiles,
  floating SVIs, external EPGs with subnets and contract verbs,
  route-control profiles and contexts.
- **Tenant > Policies > Protocol, 28/28 GUI folders** (wave 1bis): BGP and
  EIGRP address-family contexts, OSPF timers, data-plane policing, DHCP
  relay (provider carries the server address) and options, endpoint
  retention, external bridge group profiles, First Hop Security with RA
  guard, IGMP/MLD snooping, IP SLA with ICMP/TCP probes, track
  lists/members, PIM route maps with entries, route tags, tenant-level
  route maps, keychains with key tables — plus the standalone L4-L7
  policies (PBR with destinations, backup, health groups, service EPG).
  The vocabulary grows from 57 to 176 curated positions.
- `propose_vocabulary` codegen tool: assisted-curation candidates
  (makers from the navigation jargon, binds from the reference map,
  contract-verb detection, review flags) — the vocabulary now grows in
  reviewed waves, and contributions need no fabric.
- Write-only schema properties (passwords, pre-shared keys) are tracked
  as `_secure_props` on the models and excluded from `plan`/diff
  comparison — a pushed secret no longer reports phantom drift.
  Consequence: rotating a secret requires a push; `plan` cannot see it.
- Documentation: hosted site (GitHub Pages) with an executable-docs
  suite, a cookbook of operator recipes, the generated coverage matrix,
  the cobra comparison gallery, and the *Inside the DSL* page; offline
  wheelhouse (niwaki + all dependencies) attached to every GitHub
  Release for restricted networks.
- The full unit-test suite (14,200+) ships with the repository and runs
  in the public CI on Python 3.12 and 3.13.

### Changed

- The generated cursor layout scales: one module per design domain,
  loaded lazily; ancestor makers are inherited through per-position
  mixins (nearest level wins, like the runtime) — 25k generated lines
  became 4.3k at 57 positions, ~75 lines per position since.
- Cursor class names disambiguate with as many ancestor labels as
  needed (`bgpPeerP` under two positions yields distinct cursors).

## [0.3.0] — 2026-07-11

First PyPI release.

### Added

- Published on PyPI: `pip install niwaki` / `uv add niwaki` (trusted
  publishing with provenance attestations, from the public repository).
- Fabric ASN as a curated position (`bgp_instance().autonomous_system()`),
  per-port interface-profile convention support proven in the live
  walkthrough (one selector per port, reserved ports never profiled).

### Fixed

- `plan` reads are scoped with `rsp-subtree-class` to the classes the design
  declares — an unscoped full read of `uni/fabric` exceeds the APIC query
  limit ("result dataset is too big").
- Field comparison in `plan` is numeric-aware: the APIC canonicalises
  numeric strings ("80.0" reads back as "80.000000"); designs carrying
  float-like values stay idempotent.
- `fvSubnet.scope` carries its operator name (was
  `visibility_of_the_subnet`).

## [0.2.0] — 2026-07-11

### Changed

- **The SDK is now named `niwaki`** (庭木 — the Japanese art of sculpting
  full-size, living garden trees).  The former working name collided with an
  existing PyPI package; `niwaki` is free as both a distribution and an
  import name, and says exactly what the SDK does to the APIC Management
  Information Tree.
- Everything follows the new name: the import package (`import niwaki`),
  the clients (`Niwaki`/`AsyncNiwaki`), the nodes
  (`NiwakiNode`/`AsyncNiwakiNode`) and the exception root (`NiwakiError`).
  No behavioural change.

## [0.1.0] — 2026-07-10

Initial private milestone, under the project's former working name.

- Design-first architecture: the design DSL describes the whole
  `uni` subtree (tenant, access, fabric, controller), `push()` applies
  (`strict` / `staged` / `plan`), the facade observes (navigation, typed
  reads, queries, delete).
- 2,222 generated Pydantic models (APIC v6.0 schemas), 558 enums,
  human-readable field names with wire aliases.
- Curated vocabulary (`domain/vocabulary.yaml`), typed cursors generated per
  position, unified reference resolver (name + DN flavors, abstract targets),
  `bind_dn()` escape, atomic staged classes.
- Sync + async transport with proactive token refresh, retry, pagination.
- Sphinx documentation with a generated vocabulary book; 13,700+ unit tests,
  mypy strict.
