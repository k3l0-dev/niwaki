"""Model and deserialization exceptions for the niwaki SDK."""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class DeserializationError(NiwakiError):
    """
    The APIC payload cannot be parsed into a typed niwaki model.

    Raised by the model layer when the response structure returned by the APIC
    does not match the expected Pydantic schema. This can happen if:

    - The APIC firmware version returned a field that the SDK schema does not
      recognise (forwards-compatibility issue).
    - A required field is missing from the APIC response (schema drift).
    - A field value has an unexpected type.

    Attributes:
        args[0]: Human-readable description including the class name and the
                 Pydantic validation error.

    Example::

        try:
            tenant = session.get_mo("uni/tn-Prod", fvTenant)
        except DeserializationError as exc:
            logger.error("Schema mismatch for fvTenant: %s", exc)
    """
