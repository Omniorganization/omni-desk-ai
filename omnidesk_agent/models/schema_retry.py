from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


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
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list) and isinstance(payload, dict):
            missing = [str(k) for k in required if k not in payload]
            if missing:
                raise StructuredOutputError("JSON missing required fields: " + ", ".join(missing))
        expected_type = schema.get("type") if isinstance(schema, dict) else None
        if expected_type == "object" and not isinstance(payload, dict):
            raise StructuredOutputError("JSON schema expected object")
        if expected_type == "array" and not isinstance(payload, list):
            raise StructuredOutputError("JSON schema expected array")
    return payload


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
