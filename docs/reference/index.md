# Reference

Three doors, three questions.

**« What can I write at this position? »** → the {doc}`DSL reference
<vocabulary/index>`.  Generated from the curated vocabulary: one page per
position, with every maker, every **keyword argument** (type, allowed values,
default, Cisco's own definition), every `bind()` alias and verb.  This is
where you find the fields of `.bd()`.

**« What does this class/function do? »** → the {doc}`API reference
<api/index>`.  The hand-written surface: clients, design roots, `Cursor`,
query builder, exceptions, transport, utilities.

**« Will it work on my fabric? »** → {doc}`compatibility`.  Runtimes, APIC
schema release, and the semantic limits the SDK deliberately keeps.

```{toctree}
:maxdepth: 2
:hidden:

vocabulary/index
api/index
compatibility
```
