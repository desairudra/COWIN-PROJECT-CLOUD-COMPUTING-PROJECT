"""Shared fixtures for the schema tests.

The tests need a real PostgreSQL 13+ server. Point TEST_DATABASE_URL at an
empty database you do not mind wiping (the public schema is dropped and
recreated once per test session):

    TEST_DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin_test pytest db/tests -v
"""
import os
import pathlib

import psycopg
import pytest

MIGRATIONS = sorted((pathlib.Path(__file__).parent.parent / "migrations").glob("*.sql"))
DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://cowin:cowin@localhost:5432/cowin_test")


@pytest.fixture(scope="session")
def dsn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SET client_min_messages = warning")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in MIGRATIONS:
            conn.execute(path.read_text())
    return DSN


@pytest.fixture
def db(dsn):
    """Autocommit connection on a clean set of tables."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE registrations, beneficiaries, photo_id_registry CASCADE")
        yield conn

