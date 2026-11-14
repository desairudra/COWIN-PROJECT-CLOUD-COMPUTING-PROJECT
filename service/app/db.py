"""Connection pool and the three database calls the service makes.

Each call maps to one SQL function from db/migrations/004_functions.sql, runs
as one statement in its own transaction, and is safe to repeat. That makes a
retry after a dropped connection safe, so transient connection errors are
retried here once.
"""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("cowin.db")

# Errors that mean "the connection broke", not "the request was wrong".
_TRANSIENT = (psycopg.OperationalError, psycopg.errors.AdminShutdown)


class Database:
    def __init__(self, url: str, min_size: int, max_size: int):
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
            check=ConnectionPool.check_connection,
        )

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    def _run(self, sql: str, params: tuple, *, many: bool = False, attempts: int = 2):
        for attempt in range(1, attempts + 1):
            try:
                with self.pool.connection(timeout=5) as conn:
                    cur = conn.execute(sql, params)
                    return cur.fetchall() if many else cur.fetchone()
            except _TRANSIENT as exc:
                if attempt == attempts:
                    raise
                log.warning("transient DB error (%s); retrying", exc.__class__.__name__)
                time.sleep(0.05)

    def ping(self) -> bool:
        return self._run("SELECT 1 AS ok", (), attempts=1)["ok"] == 1

    def upsert_registration(self, mobile: str) -> dict:
        return self._run("SELECT * FROM upsert_registration(%s)", (mobile,))

    def add_beneficiary(self, mobile: str, key: str, b) -> dict:
        return self._run(
            "SELECT * FROM add_beneficiary(%s, %s, %s, %s, %s, %s, %s)",
            (mobile, key, b.full_name, b.birth_year, b.gender.value,
             b.photo_id_type.value, b.photo_id_number),
        )

    def list_beneficiaries(self, mobile: str) -> list[dict]:
        return self._run("SELECT * FROM list_beneficiaries(%s)", (mobile,), many=True)
