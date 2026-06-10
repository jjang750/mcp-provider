"""FastAPI application entry point for mcp-provider (contract §1/§9).

Responsibilities (backend-engineer owned):
  - lifespan: run idempotent SQLite DDL migration on startup (db.init_db)
  - register the §5 routers (specs / workflows / executions)
  - mount StaticFiles + Jinja2Templates directories (frontend fills the
    actual files; backend only mounts so it doesn't break if empty)

Run:
    uvicorn backend.app:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import init_db
from .routers import executions as executions_router
from .routers import operations as operations_router
from .routers import specs as specs_router
from .routers import workflows as workflows_router

# Repo root = parent of backend/.
_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
_STATIC_DIR = _REPO_ROOT / "static"
_TEMPLATES_DIR = _REPO_ROOT / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure schema exists (idempotent).
    init_db()
    yield
    # Shutdown: nothing to tear down (per-request connections).


app = FastAPI(title="mcp-provider", version="0.1.0", lifespan=lifespan)

# Single-user / local tool — permissive CORS is acceptable (§0 Assumption 1).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Static + templates: ensure dirs exist so mounting never breaks (frontend
#     fills the contents). Templates object is exposed for the frontend router. ---
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# --- API routers (§5) ---
app.include_router(specs_router.router)
app.include_router(operations_router.router)
app.include_router(workflows_router.router)
app.include_router(executions_router.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index(request: Request):
    """Serve the UI shell if frontend provided index.html; else a JSON ping.

    The frontend owns templates/index.html; until it exists we return a small
    JSON so the root path is never a 500.
    """
    if (_TEMPLATES_DIR / "index.html").exists():
        return templates.TemplateResponse("index.html", {"request": request})
    return JSONResponse(
        {"app": "mcp-provider", "docs": "/docs", "api": "/api"}
    )


@app.get("/editor/{workflow_id}", tags=["meta"])
def editor(workflow_id: int, request: Request):
    """Serve the workflow editor page (BUG-2 fix).

    Renders templates/editor.html with the context variable ``workflow_id``
    (consumed by editor.html's ``<meta name="workflow-id" content="{{ workflow_id }}">``).
    Falls back to a JSON ping if the frontend template is not present yet.
    """
    if (_TEMPLATES_DIR / "editor.html").exists():
        return templates.TemplateResponse(
            "editor.html", {"request": request, "workflow_id": workflow_id}
        )
    return JSONResponse({"app": "mcp-provider", "editor": workflow_id})
