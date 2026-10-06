import logging
import re
import time
from contextvars import ContextVar
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger("taskpilot.request")
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class RequestIDMiddleware:
    # Plain ASGI middleware: BaseHTTPMiddleware breaks contextvars for background tasks.
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        # Client-supplied ids end up in logs, so only accept harmless ones.
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info("%s %s -> %s (%.1f ms)", scope["method"], scope["path"], status, elapsed_ms)
