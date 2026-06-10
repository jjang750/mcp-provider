"""Internal engine models.

The architect contract (§9) states the canonical Pydantic definitions live in
``backend/models.py``.  The engine is allowed to use *"동일 키의 dict 또는 자체
동형 모델"* as long as the **wire keys match §4/§5.0 exactly**.

To keep ``import engine`` working even in a bare environment where ``pydantic``
is not yet installed (backend owns ``requirements.txt``), we prefer real
Pydantic v2 models when available and otherwise fall back to thin
dataclass-style shims that expose the same attributes + ``model_dump`` /
``model_dump(by_alias=True)`` behaviour the rest of the engine relies on.

Either way the JSON shapes produced/consumed by the engine are identical to the
contract.
"""
from __future__ import annotations

from typing import Any, Optional

try:  # Prefer real pydantic when present (backend installs it).
    from pydantic import BaseModel, Field  # type: ignore

    _HAS_PYDANTIC = True
except Exception:  # pragma: no cover - exercised only in bare env
    _HAS_PYDANTIC = False


if _HAS_PYDANTIC:

    class ParseResult(BaseModel):
        """§6 parser return shape."""

        spec_version: Optional[str] = None
        base_url: Optional[str] = None
        operations: list[dict] = Field(default_factory=list)
        warnings: list[str] = Field(default_factory=list)

else:  # pragma: no cover - bare-env fallback shim

    class _Shim:
        """Minimal stand-in offering ``model_dump`` and attribute access."""

        __slots__: tuple[str, ...] = ()

        def model_dump(self, by_alias: bool = False) -> dict:  # noqa: ARG002
            return {k: getattr(self, k) for k in self.__slots__}

        def __repr__(self) -> str:  # pragma: no cover - cosmetic
            inner = ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__slots__)
            return f"{type(self).__name__}({inner})"

    class ParseResult(_Shim):
        __slots__ = ("spec_version", "base_url", "operations", "warnings")

        def __init__(
            self,
            spec_version: Optional[str] = None,
            base_url: Optional[str] = None,
            operations: Optional[list[dict]] = None,
            warnings: Optional[list[str]] = None,
        ) -> None:
            self.spec_version = spec_version
            self.base_url = base_url
            self.operations = operations if operations is not None else []
            self.warnings = warnings if warnings is not None else []


__all__ = ["ParseResult", "_HAS_PYDANTIC"]
