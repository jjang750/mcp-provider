"""OpenAPI / Swagger parser (contract §3, §6).

Parses OpenAPI **2.0** and **3.0/3.1** specs (JSON or YAML) into the flat
``operations`` shape defined in contract §3.  Resolves internal ``$ref``
references (``#/definitions/...`` for 2.0, ``#/components/...`` for 3.x),
unrolling circular refs once and marking them with ``{"$circular": true}``.

Design goals (per ``openapi-to-mcp`` skill):
  * Partial success over total failure — one bad operation does not sink the
    rest; problems accumulate in ``warnings``.
  * Lenient parsing — we do not strictly validate the spec.
  * Pure module — no FastAPI dependency.

Return value: :class:`engine._models.ParseResult` whose ``operations`` items
match the DB-independent subset of ``OperationOut`` (§5.2), i.e. keys:
``operation_id, method, path, base_url, summary, params_schema,
request_schema, response_schema, auth``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ._models import ParseResult

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


# --------------------------------------------------------------------------- #
# Load & version detection
# --------------------------------------------------------------------------- #
def _load_raw(raw_content: str, source_hint: Optional[str]) -> dict:
    """Parse raw text into a dict, trying JSON first then YAML."""
    text = raw_content.lstrip("﻿").strip()
    if not text:
        raise ValueError("empty spec content")

    # JSON first (fast path, also valid for most swagger.json).
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # YAML (also a JSON superset). Optional dependency.
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on env
        # Last-ditch: try JSON again so we surface a meaningful error.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(
                "could not parse spec: not valid JSON and PyYAML is unavailable"
            ) from exc

    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("spec root must be a mapping/object")
    return loaded


def _detect_version(spec: dict) -> Optional[str]:
    if "swagger" in spec:
        return str(spec.get("swagger"))
    if "openapi" in spec:
        return str(spec.get("openapi"))
    return None


# --------------------------------------------------------------------------- #
# $ref resolution
# --------------------------------------------------------------------------- #
def _resolve_refs(node: Any, root: dict, _seen: Optional[frozenset[str]] = None) -> Any:
    """Recursively resolve internal ``$ref`` pointers.

    Circular references are unrolled once then replaced with
    ``{"$circular": true}`` to keep the result finite/serialisable.
    External refs (with a scheme/host) are left untouched.
    """
    if _seen is None:
        _seen = frozenset()

    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen:
                return {"$circular": True, "$ref": ref}
            target = _lookup_pointer(root, ref)
            if target is None:
                return {"$unresolved": ref}
            resolved = _resolve_refs(target, root, _seen | {ref})
            # Merge sibling keys (besides $ref) per JSON Reference leniency.
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(resolved, dict):
                merged = dict(resolved)
                merged.update(_resolve_refs(siblings, root, _seen))
                return merged
            return resolved
        return {k: _resolve_refs(v, root, _seen) for k, v in node.items()}

    if isinstance(node, list):
        return [_resolve_refs(v, root, _seen) for v in node]

    return node


def _lookup_pointer(root: dict, ref: str) -> Any:
    parts = ref.lstrip("#/").split("/")
    cur: Any = root
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")  # JSON Pointer unescape
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# --------------------------------------------------------------------------- #
# Schema flattening helpers (allOf merge, oneOf/anyOf first-candidate)
# --------------------------------------------------------------------------- #
def _flatten_schema(schema: Any, warnings: list[str]) -> Any:
    if not isinstance(schema, dict):
        return schema

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged: dict[str, Any] = {}
        required: list[Any] = []
        properties: dict[str, Any] = {}
        for sub in schema["allOf"]:
            sub_f = _flatten_schema(sub, warnings)
            if isinstance(sub_f, dict):
                for k, v in sub_f.items():
                    if k == "required" and isinstance(v, list):
                        required.extend(v)
                    elif k == "properties" and isinstance(v, dict):
                        properties.update(v)
                    else:
                        merged[k] = v
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = sorted(set(required))
        # carry over any non-allOf siblings
        for k, v in schema.items():
            if k != "allOf":
                merged.setdefault(k, _flatten_schema(v, warnings))
        return merged

    for combiner in ("oneOf", "anyOf"):
        if combiner in schema and isinstance(schema[combiner], list) and schema[combiner]:
            warnings.append(
                f"{combiner} encountered; using first candidate, "
                f"{len(schema[combiner]) - 1} alternative(s) dropped"
            )
            first = _flatten_schema(schema[combiner][0], warnings)
            rest = {k: v for k, v in schema.items() if k != combiner}
            if isinstance(first, dict):
                out = dict(first)
                for k, v in rest.items():
                    out.setdefault(k, _flatten_schema(v, warnings))
                return out
            return first

    return {k: _flatten_schema(v, warnings) for k, v in schema.items()}


# --------------------------------------------------------------------------- #
# base_url determination
# --------------------------------------------------------------------------- #
def _base_url_v2(spec: dict) -> Optional[str]:
    host = spec.get("host")
    base_path = spec.get("basePath", "") or ""
    schemes = spec.get("schemes") or ["https"]
    scheme = "https" if "https" in schemes else schemes[0]
    if host:
        return f"{scheme}://{host}{base_path}".rstrip("/")
    if base_path:
        return base_path.rstrip("/")
    return None


def _base_url_v3(spec: dict) -> Optional[str]:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        url = servers[0].get("url") if isinstance(servers[0], dict) else None
        if url:
            # Apply default variable substitutions if present.
            variables = servers[0].get("variables") or {}
            for name, var in variables.items():
                if isinstance(var, dict) and "default" in var:
                    url = url.replace("{" + name + "}", str(var["default"]))
            return url.rstrip("/")
    return None


# --------------------------------------------------------------------------- #
# auth / security scheme mapping
# --------------------------------------------------------------------------- #
def _collect_security_schemes(spec: dict, version: Optional[str]) -> dict[str, dict]:
    if version and version.startswith("2"):
        return spec.get("securityDefinitions") or {}
    return (spec.get("components") or {}).get("securitySchemes") or {}


def _map_auth(scheme: dict) -> dict:
    """Map one securityScheme to the contract §3 auth shape."""
    stype = (scheme.get("type") or "").lower()
    if stype == "apikey":
        return {
            "type": "apiKey",
            "in": scheme.get("in", "header"),
            "name": scheme.get("name", "api_key"),
        }
    if stype == "http":
        body_scheme = (scheme.get("scheme") or "").lower()
        if body_scheme == "bearer":
            return {"type": "bearer"}
        if body_scheme == "basic":
            return {"type": "basic"}
        return {"type": "http", "scheme": body_scheme or "bearer"}
    if stype in ("oauth2", "openidconnect"):
        return {"type": "oauth2"}
    if stype == "basic":  # swagger 2.0 basic
        return {"type": "basic"}
    return {"type": "none"}


def _resolve_operation_auth(
    op_security: Any,
    global_security: Any,
    schemes: dict[str, dict],
) -> dict:
    """Pick the effective auth for an operation. First requirement wins."""
    requirements = op_security if op_security is not None else global_security
    if not requirements:
        return {"type": "none"}
    if isinstance(requirements, list) and requirements:
        first = requirements[0]
        if isinstance(first, dict) and first:
            scheme_name = next(iter(first.keys()))
            scheme = schemes.get(scheme_name)
            if isinstance(scheme, dict):
                return _map_auth(scheme)
    return {"type": "none"}


# --------------------------------------------------------------------------- #
# parameter classification
# --------------------------------------------------------------------------- #
def _param_type(param: dict) -> str:
    # 3.x: type lives under .schema; 2.0: type is inline.
    schema = param.get("schema")
    if isinstance(schema, dict) and schema.get("type") in _JSON_SCHEMA_TYPES:
        return schema["type"]
    t = param.get("type")
    if t in _JSON_SCHEMA_TYPES:
        return t
    return "string"


def _param_enum(param: dict) -> Optional[list]:
    schema = param.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("enum"), list):
        return schema["enum"]
    if isinstance(param.get("enum"), list):
        return param["enum"]
    return None


def _param_default(param: dict) -> Any:
    schema = param.get("schema")
    if isinstance(schema, dict) and "default" in schema:
        return schema["default"]
    return param.get("default")


def _build_param_entry(param: dict) -> dict:
    entry: dict[str, Any] = {
        "name": param.get("name", ""),
        "type": _param_type(param),
        "required": bool(param.get("required", False)),
        "description": param.get("description", "") or "",
    }
    enum = _param_enum(param)
    if enum is not None:
        entry["enum"] = enum
    default = _param_default(param)
    if default is not None:
        entry["default"] = default
    return entry


def _classify_parameters(
    parameters: list,
    version: Optional[str],
    warnings: list[str],
) -> tuple[dict[str, list], Optional[dict]]:
    """Return (params_schema, request_schema-from-body-param-or-None).

    Handles swagger 2.0 ``in: body``/``in: formData`` by normalising to a
    request_schema, and path/query/header (+cookie folded into header note).
    """
    params_schema: dict[str, list] = {"path": [], "query": [], "header": []}
    body_request_schema: Optional[dict] = None

    for param in parameters:
        if not isinstance(param, dict):
            continue
        loc = param.get("in")
        if loc == "path":
            params_schema["path"].append(_build_param_entry(param))
        elif loc == "query":
            params_schema["query"].append(_build_param_entry(param))
        elif loc == "header":
            params_schema["header"].append(_build_param_entry(param))
        elif loc == "cookie":
            # No dedicated cookie bucket in §3; record under header w/ note.
            entry = _build_param_entry(param)
            entry["description"] = (entry["description"] + " (cookie)").strip()
            params_schema["header"].append(entry)
        elif loc == "body":  # swagger 2.0 body param -> requestBody
            body_request_schema = {
                "content_type": "application/json",
                "schema": param.get("schema", {}),
                "required": bool(param.get("required", False)),
            }
        elif loc == "formData":  # swagger 2.0 form param
            if body_request_schema is None:
                body_request_schema = {
                    "content_type": "application/x-www-form-urlencoded",
                    "schema": {"type": "object", "properties": {}, "required": []},
                    "required": False,
                }
            props = body_request_schema["schema"].setdefault("properties", {})
            props[param.get("name", "")] = {"type": _param_type(param)}
            if param.get("required"):
                body_request_schema["schema"].setdefault("required", []).append(
                    param.get("name", "")
                )
                body_request_schema["required"] = True
        else:
            warnings.append(f"unknown parameter location '{loc}' skipped")

    return params_schema, body_request_schema


# --------------------------------------------------------------------------- #
# requestBody (3.x) and responses
# --------------------------------------------------------------------------- #
def _extract_request_body_v3(operation: dict, warnings: list[str]) -> Optional[dict]:
    rb = operation.get("requestBody")
    if not isinstance(rb, dict):
        return None
    content = rb.get("content")
    if not isinstance(content, dict) or not content:
        return None
    # Prefer application/json, else first content-type.
    ctype = "application/json" if "application/json" in content else next(iter(content))
    media = content.get(ctype) or {}
    schema = media.get("schema", {}) if isinstance(media, dict) else {}
    return {
        "content_type": ctype,
        "schema": _flatten_schema(schema, warnings),
        "required": bool(rb.get("required", False)),
    }


def _extract_responses(
    operation: dict,
    version: Optional[str],
    warnings: list[str],
) -> Optional[dict]:
    responses = operation.get("responses")
    if not isinstance(responses, dict) or not responses:
        return None
    out: dict[str, Any] = {}
    is_v2 = bool(version and version.startswith("2"))
    for code, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        schema: Any = None
        if is_v2:
            schema = resp.get("schema")
        else:
            content = resp.get("content")
            if isinstance(content, dict) and content:
                ctype = (
                    "application/json"
                    if "application/json" in content
                    else next(iter(content))
                )
                media = content.get(ctype) or {}
                schema = media.get("schema") if isinstance(media, dict) else None
        if schema is not None:
            out[str(code)] = _flatten_schema(schema, warnings)
    return out or None


# --------------------------------------------------------------------------- #
# operationId generation / dedup
# --------------------------------------------------------------------------- #
def _slug_path(path: str) -> str:
    slug = re.sub(r"[{}]", "", path)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_")
    return slug or "root"


def _ensure_operation_id(operation: dict, method: str, path: str) -> str:
    oid = operation.get("operationId")
    if isinstance(oid, str) and oid.strip():
        return oid.strip()
    return f"{method.lower()}_{_slug_path(path)}"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def parse_openapi(raw_content: str, source_hint: Optional[str] = None) -> ParseResult:
    """Parse an OpenAPI 2.0 / 3.0 / 3.1 spec into a :class:`ParseResult`.

    Partial failures are tolerated: every operation that can be parsed is
    returned; problems are accumulated in ``warnings`` rather than raised.
    """
    warnings: list[str] = []

    try:
        spec = _load_raw(raw_content, source_hint)
    except Exception as exc:
        # Total load failure: nothing to salvage.
        return ParseResult(
            spec_version=None,
            base_url=None,
            operations=[],
            warnings=[f"failed to load spec: {exc}"],
        )

    if not isinstance(spec, dict):
        return ParseResult(
            spec_version=None,
            base_url=None,
            operations=[],
            warnings=["spec root is not an object"],
        )

    version = _detect_version(spec)
    if version is None:
        warnings.append("could not detect spec version (no 'swagger'/'openapi' field)")
    is_v2 = bool(version and version.startswith("2"))

    # Resolve $refs once over the whole document for stable lookups.
    try:
        resolved = _resolve_refs(spec, spec)
        if isinstance(resolved, dict):
            spec = resolved
    except RecursionError:
        warnings.append("ref resolution hit recursion limit; using shallow spec")

    base_url = _base_url_v2(spec) if is_v2 else _base_url_v3(spec)
    if base_url is None:
        warnings.append("could not determine base_url from spec")

    schemes = _collect_security_schemes(spec, version)
    global_security = spec.get("security")

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        warnings.append("no 'paths' object found")
        return ParseResult(
            spec_version=version, base_url=base_url, operations=[], warnings=warnings
        )

    operations: list[dict] = []
    seen_ids: dict[str, int] = {}

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            warnings.append(f"path '{path}' is not an object; skipped")
            continue
        # Path-level shared parameters (apply to all methods on this path).
        shared_params = path_item.get("parameters") or []

        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                warnings.append(f"{method.upper()} {path}: operation not an object; skipped")
                continue
            try:
                op = _parse_one_operation(
                    method=method.upper(),
                    path=path,
                    operation=operation,
                    shared_params=shared_params,
                    version=version,
                    base_url=base_url,
                    schemes=schemes,
                    global_security=global_security,
                    warnings=warnings,
                )
                # operationId uniqueness.
                oid = op["operation_id"]
                if oid in seen_ids:
                    seen_ids[oid] += 1
                    new_oid = f"{oid}_{seen_ids[oid]}"
                    warnings.append(
                        f"duplicate operationId '{oid}' -> renamed '{new_oid}'"
                    )
                    op["operation_id"] = new_oid
                else:
                    seen_ids[oid] = 0
                operations.append(op)
            except Exception as exc:  # noqa: BLE001 - isolate per-operation failure
                warnings.append(f"{method.upper()} {path}: parse failed ({exc}); skipped")

    return ParseResult(
        spec_version=version,
        base_url=base_url,
        operations=operations,
        warnings=warnings,
    )


def _parse_one_operation(
    *,
    method: str,
    path: str,
    operation: dict,
    shared_params: list,
    version: Optional[str],
    base_url: Optional[str],
    schemes: dict[str, dict],
    global_security: Any,
    warnings: list[str],
) -> dict:
    # Merge path-level + operation-level parameters (op-level overrides by name/in).
    merged_params: dict[tuple, dict] = {}
    for p in list(shared_params) + list(operation.get("parameters") or []):
        if isinstance(p, dict):
            merged_params[(p.get("name"), p.get("in"))] = p
    parameters = list(merged_params.values())

    params_schema, body_from_params = _classify_parameters(parameters, version, warnings)

    # requestBody: 3.x dedicated object, else 2.0 body/formData param.
    request_schema = _extract_request_body_v3(operation, warnings)
    if request_schema is None:
        request_schema = body_from_params

    response_schema = _extract_responses(operation, version, warnings)
    auth = _resolve_operation_auth(
        operation.get("security"), global_security, schemes
    )

    return {
        "operation_id": _ensure_operation_id(operation, method, path),
        "method": method,
        "path": path,
        "base_url": base_url,
        "summary": operation.get("summary") or operation.get("description") or None,
        "params_schema": params_schema,
        "request_schema": request_schema,
        "response_schema": response_schema,
        "auth": auth,
    }


__all__ = ["parse_openapi", "ParseResult"]
