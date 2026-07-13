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
```
