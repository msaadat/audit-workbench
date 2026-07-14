"""Audit Workbench API — FastAPI app factory.

Serves the JSON API under /api and, when a production build exists at
frontend/dist, the compiled Vue SPA for every other route (history-mode
fallback to index.html). In development the SPA runs on the Vite dev server
instead and proxies /api here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .explore import QueryError
from .llm import LLMError
from .routes.agent_routes import router as agent_router
from .routes.analyses_routes import router as analyses_router
from .routes.analysis_routes import router as analysis_router
from .routes.assistant_routes import router as assistant_router
from .routes.dashboard_routes import router as dashboard_router
from .routes.document_routes import router as document_router
from .routes.doc_test_routes import router as doc_test_router
from .routes.intake_routes import router as intake_router
from .routes.planning_routes import router as planning_router
from .routes.report_routes import router as report_router
from .routes.validation_routes import router as validation_router
from .routes.workspace_routes import router as workspace_router
from .assistant_settings import SettingsError
from .sandbox import SandboxError
from .workspaces import WorkspaceError

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="Audit Workbench")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(WorkspaceError)
    @app.exception_handler(QueryError)
    @app.exception_handler(SandboxError)
    @app.exception_handler(SettingsError)
    async def user_error(request: Request, error: Exception):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(LLMError)
    async def llm_error(request: Request, error: Exception):
        # 503: the request was fine, the LLM backend just isn't available.
        return JSONResponse({"detail": str(error)}, status_code=503)

    app.include_router(workspace_router)
    app.include_router(analysis_router)
    app.include_router(dashboard_router)
    app.include_router(analyses_router)
    app.include_router(assistant_router)
    app.include_router(validation_router)
    app.include_router(agent_router)
    app.include_router(intake_router)
    app.include_router(planning_router)
    app.include_router(document_router)
    app.include_router(doc_test_router)
    app.include_router(report_router)

    if FRONTEND_DIST.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(FRONTEND_DIST / "assets")),
            name="assets",
        )

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = (FRONTEND_DIST / path).resolve()
            if (
                path
                and candidate.is_file()
                and candidate.is_relative_to(FRONTEND_DIST.resolve())
            ):
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
