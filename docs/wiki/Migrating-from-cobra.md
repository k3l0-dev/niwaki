# Migrating from cobra

Porting `cobra` automation?  The documentation has both the argument and
the map:

- [Why niwaki — the comparison](https://k3l0-dev.github.io/niwaki/why.html)
- [Side by side — eight tasks in both SDKs](https://k3l0-dev.github.io/niwaki/comparison.html)
- [The migration guide](https://k3l0-dev.github.io/niwaki/cookbook/migrate-from-cobra.html) —
  concept map, three worked ports, pitfalls

The short version: `LoginSession`/`MoDirectory` becomes a context manager,
MO-plus-relation-class plumbing becomes a design with `bind()`, and
`commit()` becomes `push()` with a first-class dry run (`mode="plan"`).
