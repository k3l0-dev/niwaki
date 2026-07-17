# Side by side — cobra and niwaki

The same eight everyday tasks, written in both SDKs.  The cobra snippets
follow the [official cobra documentation](https://cobra.readthedocs.io/)
(APIC-hosted wheels, checked 2026-07-11); since cobra is not installable
from an index they are shown for reference, while **every niwaki block on
this page executes** as a test in the SDK's suite.

```{note}
cobra in this documentation — three pages, three jobs: {doc}`why` is the
argument (including where cobra remains the right tool); this page shows the
tasks side by side; {doc}`cookbook/migrate-from-cobra` is the migration
how-to.
```

## 1 — Open a session

<!--- skip: next --->
```python
from cobra.mit.access import MoDirectory
from cobra.mit.session import LoginSession

ls = LoginSession('https://apic.example.com', 'admin', 'secret')
moDir = MoDirectory(ls)
moDir.login()
# … your code …
moDir.logout()
```

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
```

Token refresh, mid-session re-login, retries and timeouts are the client's
job, not yours ({doc}`guide/connection`).  The `with` form closes the
session for you; `connect()` is used here so the rest of the page can share
the client.

## 2 — A BD with a subnet, in a VRF

<!--- skip: next --->
```python
from cobra.model.fv import Tenant, Ctx, BD, RsCtx, Subnet
from cobra.mit.request import ConfigRequest

uniMo = moDir.lookupByDn('uni')
tnMo = Tenant(uniMo, 'acme')
Ctx(tnMo, 'prod')
bdMo = BD(tnMo, 'web')
Subnet(bdMo, '10.30.1.1/24')
RsCtx(bdMo, tnFvCtxName='prod')

req = ConfigRequest()
req.addMo(tnMo)
moDir.commit(req)
```

```python
from niwaki.design import tenant

config = tenant("acme")
config.vrf("prod")
config.bd("web", unicast_routing=True).bind(vrf="prod").subnet("10.30.1.1/24")
config.push(aci)
```

No parent lookup, no relation class, no `tnFvCtxName` — and the `bind()` is
checked against the design before anything is sent.

## 3 — Contract wiring

<!--- skip: next --->
```python
from cobra.model.fv import Ap, AEPg, RsBd, RsProv, RsCons
from cobra.model.vz import Filter, Entry, BrCP, Subj, RsSubjFiltAtt

apMo = Ap(tnMo, 'shop')
webMo = AEPg(apMo, 'web')
RsBd(webMo, tnFvBDName='web')
RsProv(webMo, tnVzBrCPName='web-api')
filterMo = Filter(tnMo, 'http')
Entry(filterMo, 'e1', etherT='ip', prot='tcp', dFromPort='8080', dToPort='8080')
brcpMo = BrCP(tnMo, 'web-api')
subjMo = Subj(brcpMo, 's1')
RsSubjFiltAtt(subjMo, tnVzFilterName='http')
```

```python
epg = config.app("shop").epg("web")
epg.bind(bd="web").provide("web-api")
config.filter("http").entry("e1", tcp=8080)
config.contract("web-api").subject("s1").bind(filter="http")
config.push(aci)
```

Five relation classes and their target-prop spellings become two `bind()`s
and a verb; `tcp=8080` compiles to `etherT/prot/dFromPort/dToPort`.

## 4 — A filtered query

<!--- skip: next --->
```python
bds = moDir.lookupByClass(
    "fvBD", propFilter='and(eq(fvBD.arpFlood, "no"))'
)
```

```python
bds = aci.query("fvBD").where(arpFlood="no").fetch()
```

Same wire attribute names (that is the APIC's language), no hand-quoted
filter strings — and `and_` / `or_` / `gt` expressions compose when kwargs
run out ({doc}`guide/observing`).

## 5 — Everything under a subtree

<!--- skip: next --->
```python
from cobra.mit.request import DnQuery

dq = DnQuery('uni/tn-acme')
dq.queryTarget = 'subtree'
dq.classFilter = 'fvBD'
bds = moDir.query(dq)
```

```python
bds = aci.tenant("acme").query("fvBD").fetch()
assert [bd.name for bd in bds] == ["web"]
```

## 6 — Large result sets

<!--- skip: next --->
```python
from cobra.mit.request import ClassQuery

cq = ClassQuery('fvCEp')
cq.pageSize = 1000
cq.page = 0
endpoints = []
while True:
    page = moDir.query(cq)
    endpoints.extend(page)
    if len(page) < 1000:
        break
    cq.page += 1
```

```python
for endpoint in aci.query("fvCEp").stream():
    ...                        # pages fetched transparently as you iterate
```

## 7 — Dry run and failure modes

cobra's write path has one verb and one feedback channel — commit, then
read the APIC's answer:

<!--- skip: next --->
```python
try:
    moDir.commit(req)
except Exception as ex:        # error arrives from the APIC, after the POST
    print(ex)
```

`probe` below is a fresh design carrying a deliberate typo; `config` is the
design from task 2, already pushed to the fabric.

```python
from niwaki.exceptions import DesignError

probe = tenant("acme")
probe.vrf("prod")
probe.bd("web").bind(vrf="prdo")            # typo

try:
    probe.to_payload()                      # fails offline, before any request
except DesignError as exc:
    print(exc)                              # …Did you mean 'prod'?

# config: the task-2 design, already on the fabric
plan = config.push(aci, mode="plan")        # and the dry run is first-class
assert plan.has_changes is False
```

## 8 — Concurrent reads

cobra predates asyncio — fan-out means threads around a shared session.
The niwaki mirror is first-class ({doc}`guide/async`):

```python
import asyncio

from niwaki import AsyncNiwaki


async def snapshot() -> None:
    async with AsyncNiwaki("https://apic.example.com", "admin", "secret") as aci:
        tenants, bds, faults = await aci.gather(
            aci.query("fvTenant").fetch(),
            aci.query("fvBD").fetch(),
            aci.query("faultInst").fetch(),
        )
        print(len(tenants), len(bds), len(faults))


asyncio.run(snapshot())
```

---

The scoreboard is not one-sided everywhere: cobra guarantees write parity
with *your exact firmware* and carries Cisco's official support — when you
need those, cobra remains the reference ({doc}`why`).  For configuration as
code — reviewable, diffable, converging, typed — the eight tasks above are
the argument.
