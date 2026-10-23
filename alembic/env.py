"""Alembic environment. The DB URL comes from app settings (DATABASE_URL), not alembic.ini."""

import os

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import get_default_settings

config = context.config


def _url() -> str:
    # Tests point Alembic at the test DB through ALEMBIC_DATABASE_URL.
    return os.environ.get("ALEMBIC_DATABASE_URL") or get_default_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
