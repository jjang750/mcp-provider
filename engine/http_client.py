"""Thin httpx wrapper for the execution engine (contract §6, §0.8).

Responsibilities:
  * Build the final URL from ``base_url`` + ``path`` with path params substituted.
  * Inject auth (bearer token / apiKey header|query) at execution time — secrets
    are *never* persisted (Assumption §0.8); they arrive via ``run_workflow(auth=...)``.
  * Apply a timeout and return a normalised result.

``httpx`` is imported lazily so that ``import engine`` works in a bare
environment where the dependency is not yet installed (backend owns
``requirements.txt``).  Any HTTP/transport error is converted into an
``HttpCallError`` so the executor can record a failed node without crashing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_TIMEOUT_SECONDS = 30.0


class HttpCallError(Exception):
    """Raised for transport-level failures (DNS, connect, timeout)."""


@dataclass
class HttpResult:
    status_code: int
    json: Any                      # parsed body, or {"raw","status_code"} wrapper
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400


_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


def build_url(base_url: Optional[str], path: str, path_params: dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders in ``path`` and join with ``base_url``."""
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in path_params and path_params[key] is not None:
            from urllib.parse import quote

            return quote(str(path_params[key]), safe="")
        # Leave unresolved placeholder visible so failures are diagnosable.
        return match.group(0)

    rendered_path = _PATH_PARAM_RE.sub(_sub, path or "")

    if not base_url:
        return rendered_path
    return base_url.rstrip("/") + "/" + rendered_path.lstrip("/")


def _apply_auth(
    auth_meta: Optional[dict],
    auth_values: Optional[dict],
    headers: dict[str, str],
    query: dict[str, Any],
) -> None:
    """Inject credentials into headers/query based on the operation auth meta."""
    if not auth_meta:
        return
    auth_values = auth_values or {}
    atype = (auth_meta.get("type") or "none").lower()

    if atype in ("bearer", "http", "oauth2"):
        token = auth_values.get("token") or auth_values.get("bearer")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif atype == "basic":
        # Expect a pre-encoded credential or raw "user:pass".
        cred = auth_values.get("basic") or auth_values.get("token")
        if cred:
            headers["Authorization"] = f"Basic {cred}"
    elif atype == "apikey":
        key = (
            auth_values.get("api_key")
            or auth_values.get("apiKey")
            or auth_values.get("token")
        )
        if key:
            name = auth_meta.get("name", "api_key")
            if auth_meta.get("in") == "query":
                query[name] = key
            else:  # default header
                headers[name] = key


async def call(
    *,
    method: str,
    base_url: Optional[str],
    path: str,
    path_params: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, Any]] = None,
    body: Any = None,
    body_content_type: str = "application/json",
    auth_meta: Optional[dict] = None,
    auth_values: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> HttpResult:
    """Perform an HTTP request and return an :class:`HttpResult`.

    Raises :class:`HttpCallError` only for transport-level problems; HTTP 4xx/5xx
    are returned as a normal ``HttpResult`` (``is_error`` True) so the executor
    decides how to treat them.
    """
    try:
        import httpx  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on env
        raise HttpCallError(
            "httpx is not installed; add it to requirements.txt"
        ) from exc

    q: dict[str, Any] = dict(query or {})
    h: dict[str, str] = {str(k): str(v) for k, v in (headers or {}).items()}
    _apply_auth(auth_meta, auth_values, h, q)

    url = build_url(base_url, path, path_params or {})

    # Fail fast with a clear, actionable message instead of httpx's terse
    # "Request URL is missing an 'http://' or 'https://' protocol." This happens
    # when no base_url is configured for the node/operation.
    if not re.match(r"^https?://", url):
        raise HttpCallError(
            f"base_url 미설정: 요청 URL '{url}' 에 http(s):// 프로토콜이 없습니다. "
            f"노드 속성의 Base URL 또는 오퍼레이션 base_url 을 설정하세요."
        )

    request_kwargs: dict[str, Any] = {"params": q, "headers": h}
    if body is not None:
        if body_content_type == "application/json":
            request_kwargs["json"] = body
        elif body_content_type == "application/x-www-form-urlencoded":
            request_kwargs["data"] = body
        else:
            if isinstance(body, (bytes, str)):
                request_kwargs["content"] = body
            else:
                request_kwargs["json"] = body

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method.upper(), url, **request_kwargs)
    except Exception as exc:  # transport / timeout / connection
        raise HttpCallError(f"{method.upper()} {url} failed: {exc}") from exc

    # Parse body as JSON; otherwise wrap (Assumption §0.4).
    parsed: Any
    try:
        parsed = resp.json()
    except Exception:
        parsed = {"raw": resp.text, "status_code": resp.status_code}

    return HttpResult(
        status_code=resp.status_code,
        json=parsed,
        headers=dict(resp.headers),
    )


__all__ = ["call", "build_url", "HttpResult", "HttpCallError", "DEFAULT_TIMEOUT_SECONDS"]
