"""Schema-derived naming and type-kind policy — the shared runtime authority.

The single home of the rules that turn APIC schema metadata into Python
surface: readable-name derivation (:mod:`niwaki._schema.naming`) and
base-type classification (:mod:`niwaki._schema.kinds`).  Both the code
generators (:mod:`niwaki._codegen`, dev-only) and the runtime (the read
catalogue) import from here, so a name or a kind can never be derived two
different ways again — N copies of a naming convention drifting apart is
exactly the failure mode this package exists to end.

Stdlib-only by design: nothing here may import ``niwaki`` proper, keeping
the package importable from every layer (codegen scripts, extraction
scripts, the lazily-loaded catalogue) without cycles and without touching
the cold-start budget.
"""
