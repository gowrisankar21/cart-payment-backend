from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


class Database:
    """Owns the engine and the session factories.

    * ``session_factory``: READ COMMITTED (explicit), used by every write use case.
      Services own transactions: ``with db.session_factory() as s, s.begin(): ...``
    * ``read_only_session_factory``: REPEATABLE READ + READ ONLY, used only by
      reporting so every aggregate comes from one snapshot and nothing can be written.
    """

    def __init__(self, settings: Settings):
        self.engine: Engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            isolation_level="READ COMMITTED",
            connect_args={
                "options": (
                    f"-c lock_timeout={settings.db_lock_timeout_ms} "
                    f"-c statement_timeout={settings.db_statement_timeout_ms}"
                )
            },
        )
        self.session_factory: sessionmaker[Session] = sessionmaker(
            self.engine, expire_on_commit=False
        )
        ro_engine = self.engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        self.read_only_session_factory: sessionmaker[Session] = sessionmaker(
            ro_engine, expire_on_commit=False
        )

    def dispose(self) -> None:
        self.engine.dispose()
