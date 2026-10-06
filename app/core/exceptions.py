import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    default_detail = "Internal server error"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class UnauthorizedError(AppError):
    status_code = 401
    default_detail = "Not authenticated"


class ForbiddenError(AppError):
    status_code = 403
    default_detail = "You do not have access to this resource"


class NotFoundError(AppError):
    status_code = 404
    default_detail = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    default_detail = "Request conflicts with the current state of the resource"


class InvalidTransitionError(ConflictError):
    default_detail = "Invalid state transition"


def _error_response(status_code: int, detail: str, headers: dict | None = None) -> JSONResponse:
    request_id = request_id_var.get()
    headers = {**(headers or {}), "X-Request-ID": request_id}
    return JSONResponse(
        {"detail": detail, "request_id": request_id}, status_code=status_code, headers=headers
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return _error_response(exc.status_code, exc.detail, headers)

    @app.exception_handler(OperationalError)
    async def handle_db_unavailable(request: Request, exc: OperationalError):
        logger.error("database unavailable: %s", exc.__class__.__name__, exc_info=exc)
        return _error_response(503, "Database temporarily unavailable")

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _error_response(500, "Internal server error")
