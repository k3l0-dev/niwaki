# Integration walkthroughs

These are **live walkthroughs**, not unit tests. Each file drives the niwaki SDK
against a real Cisco APIC — or the APIC simulator — exactly as an operator would:
declare a design, push it, and let the controller accept or reject it. They exist
so that **anyone can evaluate the SDK on their own lab** and watch it configure a
fabric end to end.

## What they are for

- **Confirm the code runs correctly against a real controller** — that the
  vocabulary compiles to a payload the APIC accepts, that references resolve, and
  that the push engine, plan mode, and error handling behave against live objects.
- **Show the SDK in an operator's hands** — the scripts read like a runbook, one
  concern per file, so you can see how a design is expressed and applied.
- **Give you a starting point to try niwaki on your fabric** — point them at your
  lab and run them.

## What they are NOT

> [!WARNING]
> **These are not production configuration.**
>
> - They are **not** copy-paste snippets for a production fabric.
> - They are **not** a set of best-practice configurations to adopt.
> - The values — subnets, ASNs, policy settings, names — are **illustrative**,
>   chosen to exercise the SDK, not to model a well-designed network.
>
> Their job is to confirm that the code **executes correctly** and that the APIC
> accepts what the SDK produces — nothing more. Design your own fabric to your
> own standards.

## Layout

Numbered phases, in the order an operator brings a fabric up:

- `01_day0/` — fresh-install day-0: node registration and fabric BGP, NTP,
  syslog, SNMP, DNS, tenant management addresses, update groups, RADIUS, …
- `02_fabric-access/` — fabric provisioning: switch profiles, policy groups, and
  switch policies.

Each file names its concern (`test_00N_<topic>.py`) and carries its exact run
command at the top of its docstring.

## Running them

They talk to a real controller, so they are **opt-in** (marked
`pytest.mark.integration`) and read the target from the **environment** — never a
hardcoded address. Put your lab in a `.env` file:

```dotenv
APIC_HOST=https://your-apic.example.com
APIC_USERNAME=admin
APIC_PASSWORD=...
```

Then run a phase (or a single walkthrough):

```bash
uv run pytest tests/integration/01_day0 -m integration -s
uv run pytest tests/integration/01_day0/test_002_ntp.py -m integration -s
```

If `APIC_HOST` / `APIC_USERNAME` / `APIC_PASSWORD` are unset, or the controller is
unreachable, the suite **skips** — so it is safe to keep in the tree and is
excluded from the offline CI. The walkthroughs **mutate** the target fabric (that
is the point) and never tear down, so run them against a **lab**, not production.
