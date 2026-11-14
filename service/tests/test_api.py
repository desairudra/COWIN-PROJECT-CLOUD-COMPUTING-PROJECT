"""API tests for the registration service (commit 11): the four endpoints,
statelessness across replicas, and retry safety."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

MOBILE = "9876543210"
PERSON = {"full_name": "Asha Rao", "birth_year": 1985, "gender": "FEMALE",
          "photo_id_type": "PAN", "photo_id_number": "ABCPR1234Q"}


def login(client, mobile=MOBILE, verify_with=None):
    r = client.post("/api/v1/otp/request", json={"mobile": mobile})
    assert r.status_code == 200, r.text
    body = r.json()
    r = (verify_with or client).post("/api/v1/otp/verify",
                                     json={"txn_id": body["txn_id"], "otp": body["demo_otp"]})
    assert r.status_code == 200, r.text
    return r.json()


def auth(session):
    return {"Authorization": f"Bearer {session['token']}"}


def add(client, session, key=None, **fields):
    person = {**PERSON, **fields}
    return client.post("/api/v1/beneficiaries", json=person,
                       headers={**auth(session), "Idempotency-Key": key or uuid.uuid4().hex})


def code(r):
    return r.json()["error"]["code"]


# -- health -------------------------------------------------------------------

def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    assert "X-Served-By" in client.get("/healthz").headers


# -- 1 + 2: OTP ---------------------------------------------------------------

def test_otp_login_registers_the_mobile_once(client):
    first = login(client)
    assert first["new_registration"] is True and first["beneficiary_count"] == 0
    second = login(client)
    assert second["new_registration"] is False


def test_demo_otp_is_hidden_when_delivery_is_log(make_client):
    c = make_client(otp_delivery="log")
    body = c.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    assert "demo_otp" not in body and "txn_id" in body


def test_wrong_otp_is_rejected(client):
    txn = client.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    wrong = "0" * len(txn["demo_otp"]) if txn["demo_otp"] != "0" * 6 else "1" * 6
    r = client.post("/api/v1/otp/verify", json={"txn_id": txn["txn_id"], "otp": wrong})
    assert r.status_code == 401 and code(r) == "OTP_INVALID"


def test_expired_otp_is_rejected(make_client):
    c = make_client(otp_ttl_seconds=-1)
    txn = c.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    r = c.post("/api/v1/otp/verify", json={"txn_id": txn["txn_id"], "otp": txn["demo_otp"]})
    assert r.status_code == 401 and code(r) == "OTP_EXPIRED"


def test_tampered_txn_id_is_rejected(client):
    txn = client.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    body, sig = txn["txn_id"].split(".")
    r = client.post("/api/v1/otp/verify", json={"txn_id": body[:-2] + "AA." + sig, "otp": txn["demo_otp"]})
    assert r.status_code == 401


def test_txn_id_does_not_contain_the_otp(client):
    txn = client.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    import base64
    payload = base64.urlsafe_b64decode(txn["txn_id"].split(".")[0] + "==").decode()
    assert txn["demo_otp"] not in payload


def test_bad_mobile_is_rejected(client):
    for bad in ["12345", "5876543210", "abcdefghij"]:
        r = client.post("/api/v1/otp/request", json={"mobile": bad})
        assert r.status_code == 422 and code(r) == "INVALID_INPUT"


# -- stateless: any replica can serve any step ---------------------------------

def test_otp_from_one_replica_verifies_on_another(make_client):
    a, b = make_client(), make_client()
    session = login(a, verify_with=b)
    assert add(b, session).status_code == 201       # session from b used on b
    assert a.get("/api/v1/beneficiaries", headers=auth(session)).json()["count"] == 1


def test_replica_with_a_different_secret_rejects_tokens(make_client):
    a, other = make_client(), make_client(secret_key="a-completely-different-secret")
    session = login(a)
    assert other.get("/api/v1/beneficiaries", headers=auth(session)).status_code == 401


# -- auth on beneficiary endpoints ----------------------------------------------

def test_beneficiary_endpoints_need_a_session(client):
    assert code(client.get("/api/v1/beneficiaries")) == "UNAUTHENTICATED"
    r = client.get("/api/v1/beneficiaries", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


def test_otp_txn_id_cannot_be_used_as_a_session(client):
    txn = client.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
    r = client.get("/api/v1/beneficiaries", headers={"Authorization": f"Bearer {txn['txn_id']}"})
    assert r.status_code == 401


def test_expired_session_is_rejected(make_client):
    c = make_client(session_ttl_seconds=-1)
    session = login(c)
    assert c.get("/api/v1/beneficiaries", headers=auth(session)).status_code == 401


# -- 3 + 4: add and list ----------------------------------------------------------

def test_add_then_list_with_masked_photo_id(client):
    s = login(client)
    r = add(client, s)
    assert r.status_code == 201 and r.json()["created"] is True
    listing = client.get("/api/v1/beneficiaries", headers=auth(s)).json()
    assert listing["count"] == 1 and listing["max_allowed"] == 4
    assert listing["beneficiaries"][0]["photo_id_masked"] == "XXXXXX234Q"
    assert "photo_id_number" not in listing["beneficiaries"][0]


def test_idempotency_key_is_required_and_validated(client):
    s = login(client)
    r = client.post("/api/v1/beneficiaries", json=PERSON, headers=auth(s))
    assert r.status_code == 400 and code(r) == "IDEMPOTENCY_KEY_REQUIRED"
    r = add(client, s, key="bad key!")
    assert r.status_code == 400 and code(r) == "IDEMPOTENCY_KEY_INVALID"


def test_retry_returns_200_replay_and_no_duplicate(client):
    s = login(client)
    key = uuid.uuid4().hex
    first, second = add(client, s, key=key), add(client, s, key=key)
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["beneficiary_id"] == second.json()["beneficiary_id"]
    assert client.get("/api/v1/beneficiaries", headers=auth(s)).json()["count"] == 1


def test_key_reused_for_a_different_person_is_422(client):
    s = login(client)
    key = uuid.uuid4().hex
    add(client, s, key=key)
    r = add(client, s, key=key, full_name="Someone Else")
    assert r.status_code == 422 and code(r) == "IDEMPOTENCY_KEY_REUSED"


def test_fifth_beneficiary_is_409(client):
    s = login(client)
    for i in range(4):
        assert add(client, s, photo_id_number=f"ABCPR123{i}Q").status_code == 201
    r = add(client, s, photo_id_number="ABCPR9999Q")
    assert r.status_code == 409 and code(r) == "BENEFICIARY_LIMIT"


def test_same_photo_id_on_another_mobile_is_409(client):
    add(client, login(client, "9876543210"), photo_id_number="ABCPR1234Q")
    r = add(client, login(client, "9123456780"), photo_id_number="abcpr 1234 q")
    assert r.status_code == 409 and code(r) == "PHOTO_ID_IN_USE"
    assert "ABCPR1234Q" not in r.text


def test_bad_beneficiary_fields_are_422_with_details(client):
    s = login(client)
    r = add(client, s, full_name="   ", birth_year=time.localtime().tm_year + 1,
            gender="X", photo_id_type="LIBRARY_CARD")
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    assert {"full_name", "birth_year", "gender", "photo_id_type"} <= fields


def test_photo_id_rejected_by_database_format_check_is_422(client):
    s = login(client)
    r = add(client, s, photo_id_number="AB$%12")      # passes length, fails DB format
    assert r.status_code == 422 and code(r) == "INVALID_INPUT"


def test_concurrent_retries_through_the_api_create_one_row(make_client):
    a, b = make_client(), make_client()
    s = login(a)
    key = uuid.uuid4().hex
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: add(a if i % 2 else b, s, key=key), range(8)))
    assert sorted(r.status_code for r in results).count(201) == 1
    assert {r.json()["beneficiary_id"] for r in results} == {results[0].json()["beneficiary_id"]}
    assert a.get("/api/v1/beneficiaries", headers=auth(s)).json()["count"] == 1


def test_database_down_returns_503_with_retry_after(make_client):
    c = make_client(database_url="postgresql://nobody@127.0.0.1:1/none",
                    db_pool_min=0, db_pool_max=1)
    import app.db as dbmod
    original = dbmod.Database._run

    def fast(self, sql, params, *, many=False, attempts=2):
        return original(self, sql, params, many=many, attempts=1)

    dbmod.Database._run = fast
    try:
        txn = c.post("/api/v1/otp/request", json={"mobile": MOBILE}).json()
        r = c.post("/api/v1/otp/verify", json={"txn_id": txn["txn_id"], "otp": txn["demo_otp"]})
    finally:
        dbmod.Database._run = original
    assert r.status_code == 503 and r.headers["Retry-After"] == "1"
