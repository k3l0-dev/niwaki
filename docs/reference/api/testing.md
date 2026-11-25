# Testing — an in-memory APIC

```{eval-rst}
.. automodule:: niwaki.testing
   :no-members:
```

The guide page {doc}`../../guide/testing` walks through it.

## The fake

```{eval-rst}
.. autoclass:: niwaki.testing.FakeApic
   :members:
```

## Store views

```{eval-rst}
.. autoclass:: niwaki.testing.StoredObject
```

## The pytest plugin

```{eval-rst}
.. automodule:: niwaki.testing.pytest
   :no-members:
```

The plugin defines one fixture, `fake_apic`: a fresh, empty
{class}`~niwaki.testing.FakeApic` for each test that requests it.
