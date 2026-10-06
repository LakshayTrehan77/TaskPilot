from fastapi import FastAPI

from app.api.routes import auth, health, plans, tasks, users
from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestIDMiddleware, setup_logging
from app.observability.metrics import MetricsMiddleware

DESCRIPTION = """
Task management API with an AI planning step.

1. **Register** and **login** (use the Authorize button).
2. **Create a task**, then ask for a plan with `POST /tasks/{id}/plans` (returns 202).
3. Poll the plan, **approve** it, **execute** it, then complete its steps.
"""


def create_app() -> FastAPI:
    setup_logging(get_settings().log_level)

    app = FastAPI(title="TaskPilot", description=DESCRIPTION, version="1.0.0")
    # Added last = outermost, so the request id is set before metrics and handlers run.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    for module in (auth, users, tasks, plans, health):
        app.include_router(module.router)
    return app


app = create_app()
