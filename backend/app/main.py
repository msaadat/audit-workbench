"""Audit Workbench API — FastAPI app factory.

Serves the JSON API under /api and, when a production build exists at
frontend/dist, the compiled Vue SPA for every other route (history-mode
fallback to index.html). In development the SPA runs on the Vite dev server
instead and proxies /api here.
"""

from __future__ import annotations

from pathlib import Path
import re

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
from .routes.assistant_chat_routes import router as assistant_chat_router
from .routes.dashboard_routes import router as dashboard_router
from .routes.decisions_routes import router as decisions_router
from .routes.provenance_routes import router as provenance_router
from .routes.data_test_routes import router as data_test_router
from .routes.debug_routes import router as debug_router
from .routes.document_routes import router as document_router
from .routes.doc_test_routes import router as doc_test_router
from .routes.intake_routes import router as intake_router
from .routes.planning_routes import router as planning_router
from .routes.report_routes import router as report_router
from .routes.validation_routes import router as validation_router
from .routes.workspace_routes import router as workspace_router
from .assistant_settings import SettingsError
from .document_analysis import AnalysisConflict
from .sandbox import SandboxError
from .workspaces import (
    WorkspaceConflict,
    WorkspaceError,
    reset_request_revision,
    set_request_revision,
)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_WORKSPACE_PATH = re.compile(r"^/api/workspaces/([^/]+)(?:/|$)")


def _expected_revision(request: Request) -> int | None:
    raw = request.headers.get("if-match") or request.headers.get("x-workspace-revision")
    if raw is None:
        return None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    if value.lower().startswith("rev-"):
        value = value[4:]
    try:
        return int(value)
    except ValueError as error:
        raise WorkspaceError("The workspace revision header must be an integer ETag.") from error


def create_app() -> FastAPI:
    app = FastAPI(title="Audit Workbench")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag", "X-Workspace-Revision"],
    )

    @app.middleware("http")
    async def workspace_revision_contract(request: Request, call_next):
        """Expose revisions on every workspace response and reject stale writes.

        Existing clients may omit ``If-Match`` during the compatibility window;
        clients that supply it get strict optimistic concurrency semantics.  A
        route still loads its own fresh workspace and every actual save performs
        compare-and-swap under the shared workspace writer lock.
        """
        match = _WORKSPACE_PATH.match(request.url.path)
        if match is None:
            return await call_next(request)
        workspace_id = match.group(1)
        try:
            expected = _expected_revision(request)
        except WorkspaceError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        current = None
        try:
            from .workspaces import load_workspace

            current = load_workspace(workspace_id).revision
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and expected is not None:
                if current != expected:
                    conflict = WorkspaceConflict(expected, current)
                    response = JSONResponse(
                        {
                            "detail": str(conflict),
                            "code": "workspace_revision_conflict",
                            "expected_revision": expected,
                            "current_revision": current,
                        },
                        status_code=409,
                    )
                    response.headers["ETag"] = f'"rev-{current}"'
                    response.headers["X-Workspace-Revision"] = str(current)
                    return response
        except WorkspaceError:
            # Let the endpoint and the normal WorkspaceError handler produce
            # the existing not-found response.
            return await call_next(request)
        token = None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and expected is not None:
            token = set_request_revision(expected)
        try:
            response = await call_next(request)
        finally:
            if token is not None:
                reset_request_revision(token)
        try:
            from .workspaces import load_workspace

            current = load_workspace(workspace_id).revision
        except WorkspaceError:
            pass
        if current is not None:
            response.headers["ETag"] = f'"rev-{current}"'
            response.headers["X-Workspace-Revision"] = str(current)
        return response

    @app.exception_handler(WorkspaceError)
    @app.exception_handler(QueryError)
    @app.exception_handler(SandboxError)
    @app.exception_handler(SettingsError)
    async def user_error(request: Request, error: Exception):
        if isinstance(error, WorkspaceConflict):
            response = JSONResponse(
                {
                    "detail": str(error),
                    "code": "workspace_revision_conflict",
                    "expected_revision": error.expected_revision,
                    "current_revision": error.current_revision,
                    **(
                        {
                            "parent_ref": error.parent_ref,
                            "expected_parent_sha1": error.expected_sha1,
                            "current_parent_sha1": error.current_sha1,
                        }
                        if hasattr(error, "parent_ref") else {}
                    ),
                },
                status_code=409,
            )
            response.headers["ETag"] = f'"rev-{error.current_revision}"'
            response.headers["X-Workspace-Revision"] = str(error.current_revision)
            return response
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(LLMError)
    async def llm_error(request: Request, error: Exception):
        # 503: the request was fine, the LLM backend just isn't available.
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(AnalysisConflict)
    async def analysis_conflict(request: Request, error: Exception):
        return JSONResponse({"detail": str(error)}, status_code=409)

    app.include_router(workspace_router)
    app.include_router(analysis_router)
    app.include_router(dashboard_router)
    app.include_router(decisions_router)
    app.include_router(provenance_router)
    app.include_router(data_test_router)
    app.include_router(debug_router)
    app.include_router(analyses_router)
    app.include_router(assistant_router)
    app.include_router(assistant_chat_router)
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
