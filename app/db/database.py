from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True)

# expire_on_commit=False lets us build responses from objects after the commit.
SessionLocal = sessionmaker(engine, expire_on_commit=False)
