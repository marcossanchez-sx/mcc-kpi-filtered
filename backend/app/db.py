"""
Conexión y sesión.

El engine se crea de forma perezosa a propósito: importar la app no debe exigir que
el driver de Postgres esté instalado ni que la base de datos esté levantada. Así los
tests corren sobre SQLite sin tocar nada, y la API puede arrancar y responder al
healthcheck aunque Postgres tarde en estar listo.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_URL = "postgresql+psycopg://mcc:mcc@db:5432/mcc"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    if url.startswith("sqlite"):
        # check_same_thread=False porque TestClient atiende las peticiones en otro hilo.
        # Y para :memory: hace falta StaticPool: por defecto cada conexión nueva crea
        # una base vacía distinta, así que el esquema creado en un hilo no se ve en
        # otro y todo falla con "no such table".
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            return create_engine(
                url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
                future=True,
            )
        return create_engine(url, connect_args={"check_same_thread": False}, future=True)
    # pool_pre_ping descarta conexiones muertas: sobrevive a reinicios del contenedor
    # de base de datos sin que la API tenga que reiniciarse también.
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, future=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


def init_db(target_engine: Engine | None = None) -> None:
    Base.metadata.create_all(target_engine or get_engine())


def get_session() -> Iterator[Session]:
    """
    Dependencia de FastAPI. Commit al terminar bien, rollback si algo falla, para que
    una carga a medias nunca quede persistida.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
