from collections.abc import Iterator

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, sessionmaker

from app.cache.redis import TaskCache, get_cache
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_access_token
from app.db.database import SessionLocal
from app.db.models import User
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from app.services.plan import PlanService
from app.services.task import TaskService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def get_session_factory() -> sessionmaker:
    return SessionLocal


def get_db(factory: sessionmaker = Depends(get_session_factory)) -> Iterator[Session]:
    with factory() as db:
        yield db


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    user_id = decode_access_token(token) if token else None
    user = UserRepository(db).get(user_id) if user_id else None
    if user is None:
        raise UnauthorizedError("Invalid or missing token")
    return user


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    return AuthService(db)


def get_task_service(
    db: Session = Depends(get_db), cache: TaskCache = Depends(get_cache)
) -> TaskService:
    return TaskService(db, cache)


def get_plan_service(
    db: Session = Depends(get_db), cache: TaskCache = Depends(get_cache)
) -> PlanService:
    return PlanService(db, cache)
