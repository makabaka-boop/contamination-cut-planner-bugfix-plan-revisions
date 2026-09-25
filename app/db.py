"""数据库引擎与会话。"""

import time

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


def _create_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        # 供本地测试使用；生产通过 DATABASE_URL 指向 PostgreSQL
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(url, pool_pre_ping=True)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(attempts=30, delay=1.0):
    """建表；数据库尚未就绪时重试，避免服务启动竞态。"""
    from . import models  # noqa: F401  确保模型已注册

    last_error = None
    for _ in range(attempts):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # 数据库未就绪等
            last_error = exc
            time.sleep(delay)
    raise last_error
