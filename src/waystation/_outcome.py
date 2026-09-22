"""The Outcome contract, derived from the one type a run validates against.

``flow.run()`` and ``run_agent`` both refuse a type here, so the two doors
never disagree on what an Outcome may be (ADR-0038).
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from pydantic.errors import (
    PydanticInvalidForJsonSchema,
    PydanticSchemaGenerationError,
    PydanticUserError,
)

__all__ = ["outcome_schema"]


def outcome_schema(outcome_type: type[Any]) -> dict[str, Any]:
    """The JSON schema a provider delivers for ``outcome_type``.

    Raises ``TypeError`` unless the type is object-shaped: an Outcome is a
    JSON object (ADR-0021).
    """
    try:
        schema = TypeAdapter(outcome_type).json_schema()
    except PydanticUserError as exc:
        # Every pydantic refusal of the type itself — no schema, a field with
        # none, a name it cannot resolve — but not misuse of pydantic (#141).
        schemaless = (PydanticSchemaGenerationError, PydanticInvalidForJsonSchema)
        if not isinstance(exc, schemaless) and exc.code != "class-not-fully-defined":
            raise
        msg = (
            f"outcome type {outcome_type!r} has no JSON schema; use a BaseModel, "
            "dataclass or TypedDict whose fields all have one"
        )
        raise TypeError(msg) from exc
    # A root that is only a ref names its shape in $defs; judge that shape.
    root = schema
    if "$ref" in root and "$defs" in schema:
        root = schema["$defs"][root["$ref"].rsplit("/", 1)[-1]]
    if root.get("type") != "object" and "properties" not in root:
        msg = f"outcome type must be an object-shaped type, got {outcome_type!r}"
        raise TypeError(msg)
    return schema
