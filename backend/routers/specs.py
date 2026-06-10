"""/api/specs* endpoints (contract §5.1 / §5.2).

Upload (file/URL) -> parse via engine -> store operations -> SpecUploadResult.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from .. import engine_bridge
from ..db import get_db
from ..models import (
    OperationOut,
    SpecFromUrlRequest,
    SpecSummary,
    SpecUploadResult,
)
from ..repositories import specs as specs_repo
from ..repositories._util import utc_now

router = APIRouter(prefix="/api/specs", tags=["specs"])

# Reasonable upper bound for an OpenAPI document (10 MB).
_MAX_SPEC_BYTES = 10 * 1024 * 1024
_ALLOWED_EXTS = (".json", ".yaml", ".yml")
_URL_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse_result_to_dict(pr) -> dict:
    """Normalize a ParseResult-like object into plain fields."""
    if hasattr(pr, "model_dump"):
        d = pr.model_dump()
    elif isinstance(pr, dict):
        d = pr
    else:
        d = {
            "spec_version": getattr(pr, "spec_version", None),
            "base_url": getattr(pr, "base_url", None),
            "operations": list(getattr(pr, "operations", []) or []),
            "warnings": list(getattr(pr, "warnings", []) or []),
        }
    d.setdefault("operations", [])
    d.setdefault("warnings", [])
    return d


def _build_upload_result(
    conn, *, spec_id: int, warnings: list[str]
) -> SpecUploadResult:
    summary = specs_repo.spec_summary(conn, spec_id)
    ops = specs_repo.list_operations_for_spec(conn, spec_id)
    return SpecUploadResult(
        spec=summary,
        operation_count=len(ops),
        operations=ops,
        warnings=warnings,
    )


def _store_parsed(
    conn,
    *,
    name: str,
    source_type: str,
    source_ref: str,
    raw_content: str,
) -> SpecUploadResult:
    """Parse raw_content via engine, persist spec + operations, build result."""
    try:
        pr = engine_bridge.parse_openapi(raw_content, source_ref)
    except engine_bridge.EngineUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"OpenAPI parser unavailable: {exc}",
        ) from exc
    except Exception as exc:  # parser rejected the document
        raise HTTPException(
            status_code=400, detail=f"Failed to parse OpenAPI spec: {exc}"
        ) from exc

    parsed = _parse_result_to_dict(pr)
    spec_id = specs_repo.create_spec(
        conn,
        name=name,
        source_type=source_type,
        source_ref=source_ref,
        spec_version=parsed.get("spec_version"),
        raw_content=raw_content,
        parsed_at=utc_now(),
    )
    # base_url from parser is per-operation in §9; attach if op lacks one.
    base_url = parsed.get("base_url")
    operations = parsed.get("operations") or []
    for op in operations:
        if base_url and not op.get("base_url"):
            op["base_url"] = base_url
    specs_repo.insert_operations(conn, spec_id, operations)
    return _build_upload_result(conn, spec_id=spec_id, warnings=parsed.get("warnings", []))


def _guard_ssrf(url: str) -> None:
    """Block obvious SSRF targets (loopback / private / link-local / non-http)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must be http(s).")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=400, detail=f"Could not resolve host: {host}"
        ) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise HTTPException(
                status_code=400,
                detail="Refusing to fetch internal/private network address.",
            )


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@router.post("/upload", response_model=SpecUploadResult)
async def upload_spec(
    file: UploadFile = File(...), conn=Depends(get_db)
) -> SpecUploadResult:
    filename = file.filename or "spec"
    lower = filename.lower()
    if not lower.endswith(_ALLOWED_EXTS):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Use .json, .yaml, or .yml.",
        )
    raw = await file.read()
    if len(raw) > _MAX_SPEC_BYTES:
        raise HTTPException(status_code=400, detail="Spec file too large (>10MB).")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text.")

    return _store_parsed(
        conn,
        name=filename,
        source_type="file",
        source_ref=filename,
        raw_content=text,
    )


@router.post("/from-url", response_model=SpecUploadResult)
async def spec_from_url(
    body: SpecFromUrlRequest, conn=Depends(get_db)
) -> SpecUploadResult:
    _guard_ssrf(body.url)
    try:
        async with httpx.AsyncClient(
            timeout=_URL_TIMEOUT, follow_redirects=True, max_redirects=3
        ) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=400, detail=f"Failed to fetch URL: {exc}"
        ) from exc

    raw = resp.content
    if len(raw) > _MAX_SPEC_BYTES:
        raise HTTPException(status_code=400, detail="Spec from URL too large (>10MB).")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="URL content is not UTF-8 text.")

    name = body.name or body.url
    return _store_parsed(
        conn,
        name=name,
        source_type="url",
        source_ref=body.url,
        raw_content=text,
    )


@router.get("", response_model=list[SpecSummary])
def list_specs(conn=Depends(get_db)) -> list[SpecSummary]:
    return specs_repo.list_specs(conn)


@router.get("/{spec_id}/operations", response_model=list[OperationOut])
def list_operations(spec_id: int, conn=Depends(get_db)) -> list[OperationOut]:
    if specs_repo.get_spec_row(conn, spec_id) is None:
        raise HTTPException(status_code=404, detail=f"Spec {spec_id} not found.")
    return specs_repo.list_operations_for_spec(conn, spec_id)
