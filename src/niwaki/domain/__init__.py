"""Niwaki domain layer — curated vocabulary and schema-derived tables.

Hosts the generated :mod:`~niwaki.domain._child_map` (``CHILD_MAP`` and
``RS_TARGET_PROP`` for the facade's read navigation; ``REFERENCE_MAP`` and
``TARGET_SUBCLASSES`` for the design resolver; ``CLASS_PKG`` for lazy class
loading), plus the hand-curated ``vocabulary.yaml`` that drives the design
surface and the jargon overrides.  Import the tables from
``niwaki.domain._child_map`` directly (lazily, at use time) — re-exporting
them here would defeat the cold-start budget.
"""
