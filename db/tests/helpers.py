"""Small helpers shared by the schema tests."""
import uuid


def new_key() -> str:
    return uuid.uuid4().hex


def register(conn, mobile):
    return conn.execute("SELECT * FROM upsert_registration(%s)", (mobile,)).fetchone()


def add(conn, mobile, *, key=None, name="Test Person", year=1990, gender="FEMALE",
        id_type="AADHAAR", id_number=None):
    key = key or new_key()
    id_number = id_number or str(uuid.uuid4().int)[:12]
    return conn.execute(
        "SELECT beneficiary_id, created FROM add_beneficiary(%s, %s, %s, %s, %s, %s, %s)",
        (mobile, key, name, year, gender, id_type, id_number),
    ).fetchone()


def count_of(conn, mobile):
    return conn.execute(
        "SELECT beneficiary_count FROM registrations WHERE mobile_number = %s", (mobile,)
    ).fetchone()[0]


def rows_of(conn, mobile):
    return conn.execute(
        "SELECT count(*) FROM beneficiaries WHERE mobile_number = %s", (mobile,)
    ).fetchone()[0]
