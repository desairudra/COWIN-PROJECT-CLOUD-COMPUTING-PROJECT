"""Connection pool and the three database calls the service makes, on one
database (Database) or several (ShardedDatabase, commit 16).

Each call maps to one SQL function from db/migrations/004_functions.sql, runs
as one statement in its own transaction, and is safe to repeat. That makes a
retry after a dropped connection safe, so transient connection errors are
retried here once.
"""
import hashlib
import logging
import re
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


SLOTS = 64
_PHOTO_ID_JUNK = re.compile(r"[\s\-/.]")


def slot_of(key: str) -> int:
    """Stable slot 0..63 for a key. Same answer in every process and on every run."""
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big") % SLOTS


def normalize_photo_id(raw: str) -> str:
    """Same rule as normalize_photo_id() in 001_types_and_helpers.sql."""
    return _PHOTO_ID_JUNK.sub("", raw).upper()


class ShardedDatabase:
    """Registrations split over several databases by mobile number (commit 16).

    A key is hashed to one of 64 slots, and each slot belongs to one database.
    With 4 databases, slots 0, 4, 8... are on the first. Moving a slot to a new
    database later is a data copy plus a change to slot_map, not a re-hash of
    everything.

    Everything about one mobile (registration, beneficiaries, the 4-per-mobile
    limit) lives on one database, so those rules stay single-database
    transactions. A photo ID must be unique across all of them: it is claimed
    first on the database its own slot points to, then the beneficiary is added
    on the mobile's database, and the claim is given back if that fails.
    """

    def __init__(self, urls: list[str], min_size: int, max_size: int, slot_map: list[int] | None = None):
        if len(urls) < 2:
            raise ValueError("ShardedDatabase needs at least 2 database URLs")
        self.shards = [Database(u, min_size, max_size) for u in urls]
        self.slot_map = slot_map or [s % len(urls) for s in range(SLOTS)]
        if len(self.slot_map) != SLOTS or not all(0 <= i < len(urls) for i in self.slot_map):
            raise ValueError("slot_map must give a database for each of the 64 slots")

    def shard_index(self, key: str) -> int:
        return self.slot_map[slot_of(key)]

    def shard_for(self, key: str) -> Database:
        return self.shards[self.shard_index(key)]

    def open(self) -> None:
        for s in self.shards:
            s.open()

    def close(self) -> None:
        for s in self.shards:
            s.close()

    def ping(self) -> bool:
        return all(s.ping() for s in self.shards)

    def upsert_registration(self, mobile: str) -> dict:
        return self.shard_for(mobile).upsert_registration(mobile)

    def list_beneficiaries(self, mobile: str) -> list[dict]:
        return self.shard_for(mobile).list_beneficiaries(mobile)

    def add_beneficiary(self, mobile: str, key: str, b) -> dict:
        number = normalize_photo_id(b.photo_id_number)
        owner = self.shard_for(number)
        args = (b.photo_id_type.value, number, mobile, key)
        if not owner._run("SELECT claim_photo_id(%s, %s, %s, %s) AS ok", args)["ok"]:
            from .errors import SQLSTATE_TO_HTTP, ApiError
            raise ApiError(*SQLSTATE_TO_HTTP["CW003"])
        try:
            return self.shard_for(mobile).add_beneficiary(mobile, key, b)
        except _TRANSIENT:
            raise          # outcome unknown: keep the claim; the client's retry (same key) reuses it
        except Exception:
            owner._run("SELECT release_photo_id(%s, %s, %s, %s) AS ok", args)
            raise

    def counts(self) -> list[dict]:
        """Rows per database, for the E6 experiment and the status page."""
        out = []
        for i, s in enumerate(self.shards):
            row = s._run("SELECT (SELECT count(*) FROM registrations) AS registrations, "
                         "(SELECT count(*) FROM beneficiaries) AS beneficiaries, "
                         "(SELECT count(*) FROM photo_id_claims) AS claims", ())
            out.append({"shard": i, **row})
        return out


def make_database(urls: list[str], url: str, min_size: int, max_size: int):
    """One database, or several when DATABASE_URLS lists more than one."""
    if len(urls) > 1:
        return ShardedDatabase(urls, min_size, max_size)
    return Database(urls[0] if urls else url, min_size, max_size)
