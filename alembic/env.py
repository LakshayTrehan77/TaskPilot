from sqlalchemy import create_engine, text

from alembic import context
from app.config import get_settings
from app.db.models import Base

target_metadata = Base.metadata
database_url = get_settings().database_url

# Arbitrary constant; all replicas must use the same one.
MIGRATION_LOCK_ID = 727274


def run_migrations_offline() -> None:
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            # Several API replicas start at once on Kubernetes. The advisory lock makes the
            # others wait, and then find the schema already at head.
            connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
            connection.commit()

        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
