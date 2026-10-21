"""Build a minimal value that satisfies a JSON schema (used by the mock provider)."""

from __future__ import annotations

from typing import Any

_MAX_DEPTH = 12


def generate(schema: dict[str, Any]) -> Any:
    """Return a schema-valid value for `schema` (handles $ref, enum, const, anyOf, allOf)."""
    return _gen(schema, schema, 0)


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(node, dict)
    return node


def _merge_all_of(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    for part in schema["allOf"]:
        resolved = _resolve(part["$ref"], root) if "$ref" in part else part
        for key, value in resolved.items():
            if key == "properties" and key in merged:
                merged[key] = {**merged[key], **value}
            elif key == "required" and key in merged:
                merged[key] = [*merged[key], *value]
            else:
                merged[key] = value
    return merged


def _gen(schema: dict[str, Any], root: dict[str, Any], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("schema too deeply nested (recursive?) for the mock provider")
    if "$ref" in schema:
        return _gen(_resolve(schema["$ref"], root), root, depth + 1)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    if "allOf" in schema:
        return _gen(_merge_all_of(schema, root), root, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _gen(options[0], root, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0])
    if kind == "object" or (kind is None and "properties" in schema):
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        return {name: _gen(props[name], root, depth + 1) for name in props if name in required}
    if kind == "array":
        item = schema.get("items", {})
        return [_gen(item, root, depth + 1) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        return _string(schema)
    if kind == "integer":
        return int(_number(schema))
    if kind == "number":
        return float(_number(schema))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return {}  # an empty schema accepts anything


def _number(schema: dict[str, Any]) -> float:
    if "minimum" in schema:
        return float(schema["minimum"])
    if "exclusiveMinimum" in schema:
        return float(schema["exclusiveMinimum"]) + 1
    if "maximum" in schema:
        return min(0.0, float(schema["maximum"]))
    return 0.0


def _string(schema: dict[str, Any]) -> str:
    fmt = schema.get("format")
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "uuid":
        return "00000000-0000-4000-8000-000000000000"
    return "x" * int(schema["minLength"]) if schema.get("minLength") else "mock"
