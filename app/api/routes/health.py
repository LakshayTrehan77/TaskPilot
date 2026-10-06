from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.cache.redis import TaskCache, get_cache

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health():
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe")
def ready(response: Response, db: Session = Depends(get_db), cache: TaskCache = Depends(get_cache)):
    """Fails with 503 when PostgreSQL is unreachable. Redis is reported but does not
    affect readiness because the API works without it."""
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except SQLAlchemyError:
        database = "unavailable"
        response.status_code = 503

    redis_state = "disabled" if not cache.enabled else ("ok" if cache.ping() else "unavailable")
    return {
        "status": "ready" if database == "ok" else "not_ready",
        "database": database,
        "redis": redis_state,
    }


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
