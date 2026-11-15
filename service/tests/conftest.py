"""API test fixtures. Needs PostgreSQL 13+; set TEST_DATABASE_URL to an empty
database you do not mind wiping (same as db/tests):

    cd service && TEST_DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin_test pytest -v
"""
import os
import pathlib
import sys

import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

MIGRATIONS = sorted((pathlib.Path(__file__).resolve().parents[2] / "db" / "migrations").glob("*.sql"))
DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://cowin:cowin@localhost:5432/cowin_test")
SECRET = "test-secret-key-0123456789"


@pytest.fixture(scope="session")
def dsn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SET client_min_messages = warning")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in MIGRATIONS:
            conn.execute(path.read_text())
    return DSN


def make_settings(dsn, **overrides):
    values = dict(app_env="test", database_url=dsn, secret_key=SECRET,
                  otp_delivery="demo", web_dir="/nonexistent")
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_client(dsn):
    """Factory, so a test can start several 'replicas' or change settings."""
    clients = []

    def _make(**overrides):
        c = TestClient(create_app(make_settings(dsn, **overrides)))
        c.__enter__()
        clients.append(c)
        return c

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE registrations, beneficiaries, photo_id_registry, photo_id_claims CASCADE")
    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()
