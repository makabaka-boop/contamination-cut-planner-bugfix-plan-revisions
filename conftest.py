"""pytest 全局配置。

测试默认使用 SQLite 内存库（通过 TEST_DATABASE_URL 可指向 PostgreSQL），
每个用例前重建全部表，保证用例间互不影响。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:"
)

import pytest  # noqa: E402

from app.db import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
