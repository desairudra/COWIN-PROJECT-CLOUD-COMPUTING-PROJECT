"""Tests for the registration schema (commit 1).

Covers the three rules from report Sec 9.2:
  1. at most 4 beneficiaries per mobile number
  2. the same photo ID is never registered twice
  3. a retried request never creates a duplicate record
plus hash partitioning on mobile number (Sec 9.4), sequentially and under
concurrent load, since the 28 April 2021 surge is the scenario we model.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from helpers import add, count_of, new_key, register, rows_of

MOBILE = "9876543210"
OTHER = "9123456780"


def sqlstate_of(fn):
    try:
        fn()
    except psycopg.Error as exc:
        return exc.sqlstate
    return None


# --------------------------------------------------------------------------
# Partitioning (Sec 9.4)
# --------------------------------------------------------------------------

def test_all_three_tables_are_hash_partitioned_with_8_partitions(db):
    rows = db.execute("""
        SELECT c.relname, pt.partstrat, a.attname,
               (SELECT count(*) FROM pg_inherits i WHERE i.inhparent = c.oid)
          FROM pg_partitioned_table pt
          JOIN pg_class c     ON c.oid = pt.partrelid
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = pt.partattrs[0]
         ORDER BY c.relname
    """).fetchall()
    assert rows == [
        ("beneficiaries", "h", "mobile_number", 8),
        ("photo_id_registry", "h", "photo_id_number", 8),
        ("registrations", "h", "mobile_number", 8),
    ]


def test_lookup_by_mobile_touches_one_partition(db):
    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN (COSTS OFF) SELECT * FROM beneficiaries WHERE mobile_number = %s", (MOBILE,)))
    scanned = [p for p in range(8) if f"beneficiaries_p{p} " in plan + " "]
    assert len(scanned) == 1, plan


def test_beneficiaries_land_in_the_same_partition_index_as_their_registration(db):
    for i in range(50):
        m = f"9{i:09d}"
        register(db, m)
        add(db, m)
    mismatched = db.execute("""
        SELECT count(*) FROM registrations r JOIN beneficiaries b USING (mobile_number)
         WHERE regexp_replace(r.tableoid::regclass::text, '.*_p', '')
            <> regexp_replace(b.tableoid::regclass::text, '.*_p', '')
    """).fetchone()[0]
    assert mismatched == 0


def test_hash_partitioning_spreads_rows_evenly(db):
    n = 4000
    with db.cursor() as cur:
        cur.executemany("SELECT upsert_registration(%s)",
                        [(f"{7000000000 + i * 7919}",) for i in range(n)])
    counts = [r[0] for r in db.execute(
        "SELECT row_count FROM partition_balance WHERE table_name = 'registrations'")]
    assert sum(counts) == n and len(counts) == 8
    mean = n / 8
    assert max(abs(c - mean) / mean for c in counts) < 0.15, counts


# --------------------------------------------------------------------------
# Registration (OTP-verified mobile)
# --------------------------------------------------------------------------

def test_upsert_registration_is_retry_safe(db):
    assert register(db, MOBILE) == (MOBILE, True, 0)
    assert register(db, MOBILE) == (MOBILE, False, 0)
    assert db.execute("SELECT count(*) FROM registrations").fetchone()[0] == 1


@pytest.mark.parametrize("bad", ["12345", "5876543210", "98765432100", "98765abcde", ""])
def test_invalid_mobile_numbers_are_rejected(db, bad):
    assert sqlstate_of(lambda: register(db, bad)) == "23514"


def test_beneficiary_needs_a_verified_mobile(db):
    assert sqlstate_of(lambda: add(db, MOBILE)) == "CW002"


def test_counter_cannot_be_set_by_hand(db):
    register(db, MOBILE)
    assert sqlstate_of(lambda: db.execute(
        "UPDATE registrations SET beneficiary_count = 3 WHERE mobile_number = %s", (MOBILE,))) == "CW005"
    db.execute("INSERT INTO registrations (mobile_number, beneficiary_count) VALUES (%s, 4)", (OTHER,))
    assert count_of(db, OTHER) == 0


# --------------------------------------------------------------------------
# Rule 1: max 4 beneficiaries per mobile
# --------------------------------------------------------------------------

def test_fifth_beneficiary_is_rejected(db):
    register(db, MOBILE)
    for _ in range(4):
        assert add(db, MOBILE)[1] is True
    assert sqlstate_of(lambda: add(db, MOBILE)) == "CW001"
    assert count_of(db, MOBILE) == rows_of(db, MOBILE) == 4


def test_limit_holds_for_direct_inserts_too(db):
    """Bypassing add_beneficiary() must not bypass the rule."""
    register(db, MOBILE)
    for i in range(4):
        add(db, MOBILE)
    state = sqlstate_of(lambda: db.execute("""
        INSERT INTO beneficiaries (mobile_number, full_name, birth_year, gender,
                                   photo_id_type, photo_id_number, idempotency_key, request_hash)
        VALUES (%s, 'X', 1990, 'MALE', 'PAN', 'ABCDE1234F', 'direct-insert-1', sha256('x'))
    """, (MOBILE,)))
    assert state == "CW001"


def test_deleting_a_beneficiary_frees_a_slot_and_the_photo_id(db):
    register(db, MOBILE)
    register(db, OTHER)
    ids = [add(db, MOBILE, id_number=f"11112222333{i}")[0] for i in range(4)]
    db.execute("DELETE FROM beneficiaries WHERE beneficiary_id = %s", (ids[0],))
    assert count_of(db, MOBILE) == 3
    assert add(db, OTHER, id_number="111122223330")[1] is True   # photo ID is free again
    assert add(db, MOBILE)[1] is True                           # slot is free again
    assert count_of(db, MOBILE) == rows_of(db, MOBILE) == 4


def test_deleting_a_registration_cascades(db):
    register(db, MOBILE)
    add(db, MOBILE, id_number="555566667777")
    db.execute("DELETE FROM registrations WHERE mobile_number = %s", (MOBILE,))
    assert rows_of(db, MOBILE) == 0
    assert db.execute("SELECT count(*) FROM photo_id_registry").fetchone()[0] == 0


def test_concurrent_adds_to_one_mobile_stop_at_exactly_4(dsn, db):
    """16 parallel requests for one mobile, like a surge of retries and family members."""
    register(db, MOBILE)
    barrier = threading.Barrier(16)

    def worker(i):
        with psycopg.connect(dsn, autocommit=True) as c:
            barrier.wait()
            try:
                add(c, MOBILE, id_number=f"98765432{i:04d}")
                return "ok"
            except psycopg.Error as exc:
                return exc.sqlstate

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(worker, range(16)))
    assert results.count("ok") == 4, results
    assert set(results) - {"ok"} == {"CW001"}
    assert count_of(db, MOBILE) == rows_of(db, MOBILE) == 4


# --------------------------------------------------------------------------
# Rule 2: unique photo ID across all mobiles and partitions
# --------------------------------------------------------------------------

def test_same_photo_id_under_another_mobile_is_rejected(db):
    register(db, MOBILE)
    register(db, OTHER)
    add(db, MOBILE, id_type="AADHAAR", id_number="123456789012")
    assert sqlstate_of(lambda: add(db, OTHER, id_type="AADHAAR", id_number="123456789012")) == "CW003"
    assert rows_of(db, OTHER) == 0 and count_of(db, OTHER) == 0   # whole insert rolled back


def test_same_photo_id_under_the_same_mobile_is_rejected(db):
    register(db, MOBILE)
    add(db, MOBILE, id_number="123456789012")
    assert sqlstate_of(lambda: add(db, MOBILE, id_number="123456789012")) == "CW003"
    assert count_of(db, MOBILE) == 1


@pytest.mark.parametrize("variant", ["1234 5678 9012", "1234-5678-9012", " 123456789012 "])
def test_photo_id_formatting_does_not_bypass_uniqueness(db, variant):
    register(db, MOBILE)
    register(db, OTHER)
    add(db, MOBILE, id_number="123456789012")
    assert sqlstate_of(lambda: add(db, OTHER, id_number=variant)) == "CW003"


def test_lowercase_ids_are_normalized(db):
    register(db, MOBILE)
    register(db, OTHER)
    add(db, MOBILE, id_type="PAN", id_number="abcde1234f")
    assert sqlstate_of(lambda: add(db, OTHER, id_type="PAN", id_number="ABCDE1234F")) == "CW003"
    stored = db.execute("SELECT photo_id_number FROM beneficiaries").fetchone()[0]
    assert stored == "ABCDE1234F"


def test_same_number_on_a_different_id_type_is_allowed(db):
    register(db, MOBILE)
    add(db, MOBILE, id_type="PAN", id_number="ABCDE1234F")
    assert add(db, MOBILE, id_type="PASSPORT", id_number="ABCDE1234F")[1] is True


def test_concurrent_registration_of_one_photo_id_succeeds_once(dsn, db):
    mobiles = [f"8{i:09d}" for i in range(12)]
    for m in mobiles:
        register(db, m)
    barrier = threading.Barrier(len(mobiles))

    def worker(m):
        with psycopg.connect(dsn, autocommit=True) as c:
            barrier.wait()
            try:
                add(c, m, id_type="VOTER_ID", id_number="ABC1234567")
                return "ok"
            except psycopg.Error as exc:
                return exc.sqlstate

    with ThreadPoolExecutor(len(mobiles)) as pool:
        results = list(pool.map(worker, mobiles))
    assert results.count("ok") == 1, results
    assert set(results) - {"ok"} == {"CW003"}
    total = db.execute("SELECT sum(beneficiary_count) FROM registrations").fetchone()[0]
    assert total == 1   # losers' counter increments were rolled back


def test_photo_id_cannot_be_changed_after_insert(db):
    register(db, MOBILE)
    bid = add(db, MOBILE, id_number="123456789012")[0]
    assert sqlstate_of(lambda: db.execute(
        "UPDATE beneficiaries SET photo_id_number = '999999999999' WHERE beneficiary_id = %s",
        (bid,))) == "CW005"
    # Non-key fields (e.g. a name correction) can still be edited.
    db.execute("UPDATE beneficiaries SET full_name = 'Fixed  Name ' WHERE beneficiary_id = %s", (bid,))
    assert db.execute("SELECT full_name FROM beneficiaries").fetchone()[0] == "Fixed Name"


# --------------------------------------------------------------------------
# Rule 3: retries never create duplicates (idempotency key)
# --------------------------------------------------------------------------

def test_retry_with_same_key_returns_the_original_row(db):
    register(db, MOBILE)
    key = new_key()
    first = add(db, MOBILE, key=key, id_number="123456789012")
    second = add(db, MOBILE, key=key, id_number="123456789012")
    assert first[1] is True and second[1] is False
    assert first[0] == second[0]
    assert rows_of(db, MOBILE) == count_of(db, MOBILE) == 1


def test_retry_with_reformatted_but_equal_body_is_still_a_replay(db):
    register(db, MOBILE)
    key = new_key()
    first = add(db, MOBILE, key=key, name="Ravi Kumar", id_number="123456789012")
    second = add(db, MOBILE, key=key, name="  Ravi   Kumar ", id_number="1234 5678 9012")
    assert second == (first[0], False)


def test_same_key_with_different_body_is_rejected(db):
    register(db, MOBILE)
    key = new_key()
    add(db, MOBILE, key=key, name="Ravi Kumar", id_number="123456789012")
    assert sqlstate_of(lambda: add(db, MOBILE, key=key, name="Someone Else",
                                   id_number="123456789012")) == "CW004"
    assert rows_of(db, MOBILE) == 1


def test_retry_at_the_limit_is_a_replay_not_a_limit_error(db):
    """A retry of the 4th add must succeed (replay), not fail with CW001."""
    register(db, MOBILE)
    for _ in range(3):
        add(db, MOBILE)
    key = new_key()
    first = add(db, MOBILE, key=key, id_number="444455556666")
    assert add(db, MOBILE, key=key, id_number="444455556666") == (first[0], False)
    assert count_of(db, MOBILE) == 4


def test_same_key_under_different_mobiles_is_independent(db):
    register(db, MOBILE)
    register(db, OTHER)
    key = new_key()
    assert add(db, MOBILE, key=key)[1] is True
    assert add(db, OTHER, key=key)[1] is True


def test_concurrent_retries_create_one_row(dsn, db):
    """A client that times out and fires the same request 10 times at once."""
    register(db, MOBILE)
    key = new_key()
    barrier = threading.Barrier(10)

    def worker(_):
        with psycopg.connect(dsn, autocommit=True) as c:
            barrier.wait()
            return add(c, MOBILE, key=key, id_number="222233334444")

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(worker, range(10)))
    assert len({r[0] for r in results}) == 1
    assert sum(r[1] for r in results) == 1
    assert rows_of(db, MOBILE) == count_of(db, MOBILE) == 1


# --------------------------------------------------------------------------
# Input validation and listing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    ({"year": 1850}, "23514"),
    ({"year": 2999}, "23514"),
    ({"name": "   "}, "23514"),
    ({"id_number": "12"}, "23514"),
    ({"id_number": "12$4567"}, "23514"),
    ({"key": "short"}, "23514"),
    ({"gender": "UNKNOWN"}, "22P02"),
    ({"id_type": "LIBRARY_CARD"}, "22P02"),
])
def test_bad_input_is_rejected(db, kwargs, expected):
    register(db, MOBILE)
    assert sqlstate_of(lambda: add(db, MOBILE, **kwargs)) == expected
    assert count_of(db, MOBILE) == 0


def test_all_nine_photo_id_types_are_accepted(db):
    types = [r[0] for r in db.execute("SELECT unnest(enum_range(NULL::photo_id_type_t))::text")]
    assert len(types) == 9
    mobiles = ["9000000001", "9000000002", "9000000003"]
    for m in mobiles:
        register(db, m)
    for i, t in enumerate(types):
        add(db, mobiles[i // 4 if i < 8 else 2], id_type=t, id_number=f"ID{i}X12345")


def test_list_beneficiaries_masks_photo_id_and_keeps_order(db):
    register(db, MOBILE)
    add(db, MOBILE, name="First", id_number="123456789012")
    add(db, MOBILE, name="Second", id_type="PAN", id_number="ABCDE1234F")
    rows = db.execute(
        "SELECT full_name, photo_id_type::text, photo_id_masked FROM list_beneficiaries(%s)",
        (MOBILE,)).fetchall()
    assert rows == [("First", "AADHAAR", "XXXXXXXX9012"), ("Second", "PAN", "XXXXXX234F")]
    assert db.execute("SELECT count(*) FROM list_beneficiaries(%s)", (OTHER,)).fetchone()[0] == 0
