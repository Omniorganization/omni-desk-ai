from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional



def _load_jsonschema_validator():
    """Lazily import the required validator without adding import-time startup cost."""
    try:
        from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError
        return Draft202012Validator, JsonSchemaValidationError
    except Exception as exc:  # pragma: no cover - exercised by a dependency-failure test
        raise StructuredOutputError("JSON schema validation dependency is unavailable") from exc


class StructuredOutputError(ValueError):
    pass


@dataclass(frozen=True)
class SchemaRetryConfig:
    enabled: bool = True
    max_repairs: int = 1


def validate_json_text(text: str, schema: Optional[dict[str, Any]] = None) -> Any:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"invalid JSON: {exc.msg}") from exc
    if schema:
        _validate_payload(payload, schema)
    return payload


def _validate_payload(payload: Any, schema: dict[str, Any]) -> None:
    Draft202012Validator, JsonSchemaValidationError = _load_jsonschema_validator()
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except JsonSchemaValidationError as exc:  # type: ignore[misc]
        path = ".".join(str(p) for p in getattr(exc, "path", [])) or "$"
        raise StructuredOutputError(f"JSON schema validation failed at {path}: {exc.message}") from exc
    except StructuredOutputError:
        raise
    except Exception as exc:
        raise StructuredOutputError(f"invalid JSON schema: {exc}") from exc


def build_repair_prompt(*, original_text: str, error: str, schema: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    schema_text = json.dumps(schema or {}, ensure_ascii=False, sort_keys=True)
    system = "Repair invalid structured model output. Return only valid JSON. Do not include markdown."
    user = (
        "The previous response was invalid for the expected JSON contract.\n"
        f"Validation error: {error}\n"
        f"JSON schema: {schema_text}\n"
        "Previous response:\n"
        f"{original_text}"
    )
    return system, user
