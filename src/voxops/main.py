import ipaddress
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from voxops.api.routes import router
from voxops.core.errors import AppError, ErrorDetail, ErrorResponse
from voxops.core.settings import Settings
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


# Why: Return structured errors without echoing raw inputs, paths or exception details.
# How: Serialize only the public category and safe human-readable message.
def error_response(category: str, message: str, status: int) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(category=category, message=message))
    return JSONResponse(body.model_dump(), status_code=status)


# Why: Prevent remote and browser-origin access to the trusted local filesystem executor.
# How: Check socket peer, Host and same-origin headers before dispatching any route.
async def local_only(request: Request, call_next) -> Response:
    peer = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    host = request.url.hostname
    origin = request.headers.get("origin")
    if not loopback or host not in {"127.0.0.1", "localhost", "::1"}:
        return error_response(
            "local_executor_only", "This API accepts loopback requests only.", 403
        )
    if origin and (
        urlsplit(origin).netloc != request.url.netloc
        or urlsplit(origin).scheme != request.url.scheme
    ):
        return error_response(
            "origin_forbidden", "Cross-origin local executor access is forbidden.", 403
        )
    try:
        return await call_next(request)
    except Exception:
        return error_response(
            "internal_error", "Local operation failed; no source details returned.", 500
        )


# Why: Provide isolated application dependencies for local startup and tests.
# How: Construct settings, store, workflow and worktree manager without network calls.
def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(title="VoxOps Stage 1", version="0.1.0")
    app.state.settings = config
    app.state.store = ScanStore(config.data_dir)
    app.state.workflow = Stage1Workflow(config, app.state.store)
    app.state.worktrees = WorktreeManager(config, app.state.store)
    app.middleware("http")(local_only)
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.include_router(router)
    return app


# Why: Preserve known error categories across HTTP serialization.
# How: Return the exception's pre-sanitized public fields only.
async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
    return error_response(error.category, error.message, error.status)


# Why: Prevent Pydantic validation errors from echoing submitted secrets or paths.
# How: Replace detailed input-bearing validation errors with one safe typed envelope.
async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    return error_response(
        "invalid_request", "Request fields failed validation; consult /docs.", 422
    )


# Why: Supply a safe default service entrypoint for the trusted local executor.
# How: Bind Uvicorn only to loopback and disable access logs that may contain query inputs.
def run() -> None:
    uvicorn.run(
        "voxops.main:create_app", factory=True, host="127.0.0.1", port=8000, access_log=False
    )


if __name__ == "__main__":
    run()
