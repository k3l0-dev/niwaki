# niwaki-migrate

Rewrites 1.x niwaki call sites to the 2.0 names. Lives outside the wheel:
this is migration tooling, not runtime API.

```sh
uv run python -m tools.niwaki_migrate.migrate PATH [PATH ...] [--dry-run] [--report out.json]
```

What it does, by confidence:

- **Rewrites** keyword arguments of known calls (curated design makers,
  generated model constructors — the receiving class is certain), including
  dict literals splatted directly into such calls.
- **Rewrites** attribute accesses for old names that map to a single new
  name across every surface *and* that no class serves anymore.
- **Flags** everything it cannot attribute: opaque `**kwargs` splats into
  known makers, retired names as dict/subscript keys or kwargs of unknown
  calls, `getattr` with a literal old name, and attribute accesses whose old
  name is still live on another class (`rtr_id` moved on L3Out node
  attachments but lives on `vnsRtrCfg`).

The acceptance metric is `flags / (rewrites + flags)` — printed on every
run. Measured on niwaki's own integration suite replayed from the last 1.x
commit: 131 rewrites, 6 flags, and the flags cover exactly the sites the
manual migration had to touch by hand. Measured on niwashi-mcp: zero of
both — a catalogue-only consumer has nothing to rewrite.

`migration_2_0.json` is the committed rename table (model fields, catalogue
readable names, maker context, safety sets). Rebuild it after a naming
change with:

```sh
uv run python -m tools.niwaki_migrate.build_table
```

(dev repo only — the model surface diffs the generated tree against the
last 1.x commit; `_legacy_naming.py` is the frozen 1.x policy).
