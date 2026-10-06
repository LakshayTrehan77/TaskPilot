import os

# Must be set before app modules are imported: settings are read at import time.
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-0123456789")
os.environ.setdefault("AI_MODE", "mock")
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("DATABASE_URL", "sqlite:///./unused.db")

import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.dependencies import get_session_factory  # noqa: E402
from app.cache.redis import TaskCache, get_cache  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import Base, User, UserRole  # noqa: E402
from app.main import app  # noqa: E402


class FakeRedis:
    """Just the commands TaskCache uses, backed by a dict (TTL is ignored)."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)

    def ping(self):
        return True


class BrokenRedis:
    def _fail(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    get = setex = incr = ping = _fail


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def client(session_factory, fake_redis):
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_cache] = lambda: TaskCache(fake_redis, ttl_seconds=30)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register_and_login(client, email="alice@example.com", password="password123") -> dict:
    client.post("/auth/register", json={"email": email, "password": password})
    response = client.post("/auth/login", data={"username": email, "password": password})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def auth_headers(client):
    return register_and_login(client)


@pytest.fixture
def other_headers(client):
    return register_and_login(client, email="bob@example.com")


@pytest.fixture
def admin_headers(client, session_factory):
    with session_factory() as db:
        db.add(
            User(
                email="admin@example.com",
                password_hash=hash_password("adminpass123"),
                role=UserRole.ADMIN,
            )
        )
        db.commit()
    return register_and_login(client, email="admin@example.com", password="adminpass123")


@pytest.fixture
def task(client, auth_headers) -> dict:
    response = client.post(
        "/tasks",
        json={"title": "Prepare a data engineering project", "description": "Airflow + PostgreSQL"},
        headers=auth_headers,
    )
    return response.json()
