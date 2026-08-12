# The design DSL

```{eval-rst}
.. automodule:: niwaki.design
   :no-members:
```

## Root factories

Each root opens a curated, fully typed surface: the makers, their keyword
arguments, the `bind()` aliases and the verbs available at every position are
generated and documented in the {doc}`DSL reference <../vocabulary/index>`.

```{eval-rst}
.. autofunction:: niwaki.design.design

.. autofunction:: niwaki.design.tenant

.. autofunction:: niwaki.design.infra

.. autofunction:: niwaki.design.fabric

.. autofunction:: niwaki.design.controller

.. autofunction:: niwaki.design.aaa
```

## References that carry configuration

A `bind()`, a `bind_dn()` or a verb usually takes a plain name; wrap the target
in `ref()` when the relationship object itself carries configuration (a domain
attachment's immediacy, a subject filter's log directive, a node's management
address).

```{eval-rst}
.. autofunction:: niwaki.design.ref

.. autoclass:: niwaki.design.Ref
```

## Cursor

Every position is a typed cursor subclass of `Cursor` — the makers and the
`set()` / `bind()` signatures are generated per position (see the
{doc}`DSL reference <../vocabulary/index>`).  The base class below is the
behaviour they all share.

```{eval-rst}
.. autoclass:: niwaki.design.Cursor
```

## Push results

```{eval-rst}
.. autoclass:: niwaki.design.PushReport

.. autoclass:: niwaki.design.PlanResult

.. autoclass:: niwaki.design.ExternalRef

.. autoclass:: niwaki.design.RefCheck
```

## Reverse import

The inverse of `push()`: rebuild a design from a
{class}`~niwaki.snapshot.Snapshot` — the whole fabric or any scope under
`uni` — or from a raw payload envelope, preferring the curated vocabulary
(makers, `bind()`, the contract verbs) and falling back to the wire-name
escape hatches so the design compiles to the same wire payload the source
describes.

```{eval-rst}
.. autofunction:: niwaki.design.to_design

.. autofunction:: niwaki.design.from_payload

.. autoclass:: niwaki.design.ImportProblem
```

## Walking a design

```{eval-rst}
.. autoclass:: niwaki.design.DesignView

.. autoclass:: niwaki.design.DesignViewNode

.. autoclass:: niwaki.design.DesignViewBind
```

## Composition

Carving is a cursor method — `Cursor.slice("uni/tn-prod")` returns a fresh
design holding that subtree over an attribute-less ancestor chain;
recombining is a function:

```{eval-rst}
.. autofunction:: niwaki.design.merge
```

## Emitting code

```{eval-rst}
.. autofunction:: niwaki.design.to_code
```

## Reconciliation

The other half of drift, beside `plan`: what the fabric carries that the
design does not declare. Read-only — nothing is ever proposed for deletion.

```{eval-rst}
.. autofunction:: niwaki.design.reconcile

.. autoclass:: niwaki.design.Reconciliation
```
