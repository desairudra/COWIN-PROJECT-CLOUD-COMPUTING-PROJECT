"""Commit 16: the service on 3 databases, split by mobile number.

Creates cowin_test_s0..s2 next to TEST_DATABASE_URL (the user needs CREATEDB).
"""
import pathlib
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.db import SLOTS, Database, ShardedDatabase, make_database, normalize_photo_id, slot_of  # noqa: E402
from app.main import create_app  # noqa: E402

from conftest import DSN, MIGRATIONS, make_settings  # noqa: E402
from test_api import add, code, login  # noqa: E402

N = 3


def shard_dsn(i):
    base, _, _ = DSN.rpartition("/")
    return f"{base}/cowin_test_s{i}"


@pytest.fixture(scope="module")
def shard_dsns():
    base, _, _ = DSN.rpartition("/")
    with psycopg.connect(f"{base}/postgres", autocommit=True) as admin:
        for i in range(N):
            if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (f"cowin_test_s{i}",)).fetchone():
                admin.execute(f"CREATE DATABASE cowin_test_s{i}")
    dsns = [shard_dsn(i) for i in range(N)]
    for d in dsns:
        with psycopg.connect(d, autocommit=True) as conn:
            conn.execute("SET client_min_messages = warning")
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            for path in MIGRATIONS:
                conn.execute(path.read_text())
    return dsns


@pytest.fixture
def sharded(shard_dsns):
    for d in shard_dsns:
        with psycopg.connect(d, autocommit=True) as conn:
            conn.execute("TRUNCATE registrations, beneficiaries, photo_id_registry, photo_id_claims CASCADE")
    settings = make_settings(DSN, database_urls=shard_dsns)
    with TestClient(create_app(settings)) as c:
        yield c, shard_dsns


def rows(dsn, sql, *args):
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, args).fetchall()


def mobiles_on(router, shard, n, start=9000000000):
    out, m = [], start
    while len(out) < n:
        if router.shard_index(str(m)) == shard:
            out.append(str(m))
        m += 1
    return out


def router():
    return ShardedDatabase([shard_dsn(i) for i in range(N)], 1, 1)


def test_routing_is_stable_and_spreads_evenly():
    r = router()
    assert slot_of("9876543210") == slot_of("9876543210")
    spread = Counter(r.shard_index(str(9000000000 + i)) for i in range(6000))
    assert set(spread) == {0, 1, 2}
    assert all(1500 < v < 2500 for v in spread.values()), spread


def test_slot_map_is_checked():
    with pytest.raises(ValueError):
        ShardedDatabase(["a"], 1, 1)
    with pytest.raises(ValueError):
        ShardedDatabase(["a", "b"], 1, 1, slot_map=[0] * (SLOTS - 1))
    with pytest.raises(ValueError):
        ShardedDatabase(["a", "b"], 1, 1, slot_map=[2] * SLOTS)
    assert isinstance(make_database([], DSN, 1, 1), Database)
    assert isinstance(make_database([DSN], DSN, 1, 1), Database)
    assert isinstance(make_database(["a", "b"], DSN, 1, 1), ShardedDatabase)


def test_photo_id_normalization_matches_the_database():
    assert normalize_photo_id(" abc-pr 1234/q.") == "ABCPR1234Q"
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT normalize_photo_id(%s)", (" abc-pr 1234/q.",)).fetchone()[0] == "ABCPR1234Q"


def test_a_mobile_lives_on_exactly_one_shard(sharded):
    client, dsns = sharded
    r = router()
    for shard in range(N):
        mobile = mobiles_on(r, shard, 1)[0]
        s = login(client, mobile)
        assert add(client, s, photo_id_number=f"SHRD{shard}0001").status_code == 201
        where = [rows(d, "SELECT count(*) FROM beneficiaries WHERE mobile_number = %s", mobile)[0][0] for d in dsns]
        assert where == [1 if i == shard else 0 for i in range(N)]
    assert client.get("/readyz").status_code == 200


def test_same_photo_id_is_refused_across_shards(sharded):
    client, dsns = sharded
    r = router()
    a, b = mobiles_on(r, 0, 1)[0], mobiles_on(r, 1, 1)[0]
    pan = "DUPPN1234Q"
    sa, sb = login(client, a), login(client, b)
    assert add(client, sa, photo_id_number=pan).status_code == 201
    second = add(client, sb, photo_id_number=pan.lower())
    assert second.status_code == 409 and code(second) == "PHOTO_ID_IN_USE"
    assert sum(rows(d, "SELECT count(*) FROM beneficiaries")[0][0] for d in dsns) == 1
    owner = dsns[r.shard_index(pan)]
    assert rows(owner, "SELECT mobile_number FROM photo_id_claims") == [(a,)]


def test_retry_keeps_one_beneficiary_and_one_claim(sharded):
    client, dsns = sharded
    s = login(client, "9123456780")
    first = add(client, s, key="retry-key-0001", photo_id_number="RTRYP1234Q")
    again = add(client, s, key="retry-key-0001", photo_id_number="RTRYP1234Q")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert sum(rows(d, "SELECT count(*) FROM photo_id_claims")[0][0] for d in dsns) == 1
    assert sum(rows(d, "SELECT count(*) FROM beneficiaries")[0][0] for d in dsns) == 1


def test_fifth_beneficiary_fails_and_gives_its_photo_id_back(sharded):
    client, dsns = sharded
    s = login(client, "9988776655")
    for i in range(4):
        assert add(client, s, photo_id_number=f"LIMIT{i}0001").status_code == 201
    fifth = add(client, s, photo_id_number="LIMIT50001")
    assert fifth.status_code == 409 and code(fifth) == "BENEFICIARY_LIMIT"
    assert sum(rows(d, "SELECT count(*) FROM photo_id_claims")[0][0] for d in dsns) == 4
    other = login(client, "9988776600")
    assert add(client, other, photo_id_number="LIMIT50001").status_code == 201


def test_parallel_claims_for_one_photo_id_let_exactly_one_through(sharded):
    client, dsns = sharded
    sessions = [login(client, f"97000000{i:02d}") for i in range(8)]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda s: add(client, s, photo_id_number="RACEP1234Q").status_code, sessions))
    assert sorted(results) == [201] + [409] * 7
    assert sum(rows(d, "SELECT count(*) FROM beneficiaries")[0][0] for d in dsns) == 1


def test_counts_per_shard(sharded):
    client, dsns = sharded
    for i in range(30):
        login(client, f"96000000{i:02d}")
    db = router()
    db.open()
    try:
        counts = db.counts()
    finally:
        db.close()
    assert [c["shard"] for c in counts] == [0, 1, 2]
    assert sum(c["registrations"] for c in counts) == 30
