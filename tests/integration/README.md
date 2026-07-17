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

Numbered phases, in the order an operator brings a fabric up. Each phase is a
folder of focused walkthroughs; each file names its concern
(`test_00N_<topic>.py`) and carries its exact run command at the top of its
docstring.

- `01_day0/` — fresh-install day-0: node registration, fabric BGP, NTP, syslog,
  SNMP, DNS, management addresses, RADIUS.
- `02_fabric-access/` — access policies: pools, domains, AAEPs, policy groups,
  leaf / spine / interface profiles, interface policies.
- `03_fabric/` — fabric policies: ports, protocols, monitoring, system,
  firmware and config management.
- `04_tenant/` — the tenant world: VRFs, bridge domains, application EPGs,
  subnets, endpoints, micro-segmentation.
- `05_contracts/` — contracts: filters, subjects, labels, taboo, `vzAny`, QoS.
- `06_l3out/` — external connectivity: L3Outs (OSPF / EIGRP / BGP / static),
  route-maps, external EPGs, L2Outs, SR-MPLS.
- `07_services/` — L4-L7 service graphs, PBR / redirect, logical devices, VMM.
- `08_observability/` — SPAN / VSPAN, NetFlow, syslog, monitoring policies.
- `09_management/` — in-band / out-of-band management, node groups.

## Running them on your lab — step by step

These walkthroughs **talk to a real controller and change it**, so they are
opt-in and never run in the offline test suite. Point them at a **lab** APIC or
the APIC simulator — never production (see the warning above).

### 1. Install the SDK

```bash
uv sync
```

### 2. Point the suite at your fabric

The target is read from the **environment** — never a hardcoded address. Put your
lab's credentials in a `.env` file at the repository root:

```dotenv
APIC_HOST=https://your-apic.example.com
APIC_USERNAME=admin
APIC_PASSWORD=...
```

If any of the three is missing, or the controller is unreachable, every
integration test **skips** instead of failing — so it is always safe to run.

### 3. Run the whole suite in one pass

`pytest` recurses into every phase folder, so a single command runs them all —
no need to invoke files one by one:

```bash
uv run pytest tests/integration -m integration -s
```

- `-m integration` selects the opt-in walkthroughs (they carry that marker);
- `-s` streams the output so you watch each design compile and push live.

Expect it to take a while and to create **thousands of objects** on your fabric.
Run it **serially** — do **not** use `pytest-xdist` (`-n`): the phases share
fabric-wide singletons (fabric and infra policies), and parallel workers would
race on them.

### 4. Or run a narrower slice

```bash
# one phase
uv run pytest tests/integration/06_l3out -m integration -s

# one file
uv run pytest tests/integration/06_l3out/test_004_bgp.py -m integration -s
```

Each phase lands in its **own tenant and VLAN lane**, and each file **wipes what
it owns at the start of its run** — so re-running a phase, or the whole suite, is
safe and repeatable.

### 5. Verify what landed

The walkthroughs push configuration and **never tear it down** — the state stays
on the fabric for inspection. Confront what is live against what the designs
declared:

- open the APIC **GUI** and browse the `niwaki-it-*` tenants and policies; or
- read the fabric back through an **independent, read-only** path and compare —
  for example the [`aci-mcp`](https://github.com/k3l0-dev/aci-mcp) read-only
  oracle over the APIC.

### 6. Clean up (operator-only)

Nothing cleans up automatically. When you want the objects gone, run the manual
wipe — it calls each file's `wipe()` and is deliberately kept out of pytest so it
can never fire on its own:

```bash
# wipe one phase
uv run python tests/integration/wipe.py 06_l3out

# wipe a single file, or several targets at once
uv run python tests/integration/wipe.py 06_l3out/test_004_bgp.py
uv run python tests/integration/wipe.py 06_l3out 04_tenant
```
