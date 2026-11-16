"""Synchronous database wiring; schema changes remain Alembic's responsibility."""
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker


def create_session_factory(database_url: str):
    engine = sa.create_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    return sessionmaker(engine, expire_on_commit=False)
