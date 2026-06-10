"""mcp-provider engine package (contract §6, §9).

Pure, FastAPI-independent modules imported by the backend and the MCP server.

Public API (stable import path for backend):
    * ``run_workflow``  — async workflow executor (``engine.executor``)
    * ``parse_openapi`` — OpenAPI 2.0/3.x parser (``engine.parser``)
    * ``ParseResult``   — parser return model (``engine._models``)
"""
from __future__ import annotations

from ._models import ParseResult
from .executor import run_workflow
from .parser import parse_openapi

__all__ = ["run_workflow", "parse_openapi", "ParseResult"]
