"""Commit 12: rate limits and OTP state in Redis.

    TEST_REDIS_URL=redis://localhost:6379/15 TEST_DATABASE_URL=... pytest tests/test_limits.py -v

Uses Redis database 15 by default and empties it before each test.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis as redis_lib

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def limited(make_client):
    r = redis_lib.Redis.from_url(REDIS_URL)
    r.flushdb()

    def _make(**overrides):
        values = dict(redis_url=REDIS_URL, otp_cooldown_s=0, otp_max_per_hour=1000)
        values.update(overrides)
        return make_client(**values)

    yield _make
    r.flushdb()


def otp(c, mobile="9876543210", ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return c.post("/api/v1/otp/request", json={"mobile": mobile}, headers=headers)


def verify(c, txn, code, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return c.post("/api/v1/otp/verify", json={"txn_id": txn["txn_id"], "otp": code}, headers=headers)


def wrong(code):
    return ("0" * len(code)) if code != "0" * len(code) else ("1" * len(code))


def code_of(r):
    return r.json()["error"]["code"]


# -- per-client token bucket ---------------------------------------------------------

def test_bucket_allows_capacity_then_429_with_retry_after(limited):
    c = limited(rate_limit_capacity=5, rate_limit_window_s=300)
    for i in range(5):
        r = otp(c, f"98765432{i:02d}")
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Limit"] == "5"
        assert r.headers["X-RateLimit-Remaining"] == str(4 - i)
    r = otp(c, "9876543299")
    assert r.status_code == 429 and code_of(r) == "RATE_LIMITED"
    assert int(r.headers["Retry-After"]) >= 1


def test_bucket_refills_over_time(limited):
    c = limited(rate_limit_capacity=2, rate_limit_window_s=2)
    assert otp(c, "9876543201").status_code == 200
    assert otp(c, "9876543202").status_code == 200
    assert otp(c, "9876543203").status_code == 429
    time.sleep(1.1)
    assert otp(c, "9876543204").status_code == 200


def test_clients_have_separate_buckets_behind_the_load_balancer(limited):
    c = limited(rate_limit_capacity=2, trust_proxy=1)
    for _ in range(2):
        assert otp(c, ip="203.0.113.1").status_code == 200
    assert otp(c, ip="203.0.113.1").status_code == 429
    assert otp(c, ip="203.0.113.2").status_code == 200


def test_spoofed_forwarded_for_does_not_escape_the_bucket(limited):
    c = limited(rate_limit_capacity=2, trust_proxy=1)
    for fake in ("1.1.1.1", "2.2.2.2"):
        assert otp(c, ip=f"{fake}, 203.0.113.9").status_code == 200
    assert otp(c, ip="3.3.3.3, 203.0.113.9").status_code == 429


def test_forwarded_for_ignored_when_no_proxy_is_trusted(limited):
    c = limited(rate_limit_capacity=2, trust_proxy=0)
    assert otp(c, ip="10.0.0.1").status_code == 200
    assert otp(c, ip="10.0.0.2").status_code == 200
    assert otp(c, ip="10.0.0.3").status_code == 429


def test_health_is_never_limited(limited):
    c = limited(rate_limit_capacity=1)
    otp(c)
    assert otp(c).status_code == 429
    for _ in range(5):
        assert c.get("/healthz").status_code == 200


def test_bucket_is_shared_by_replicas(limited):
    a, b = limited(rate_limit_capacity=3), limited(rate_limit_capacity=3)
    assert otp(a, "9876543201").status_code == 200
    assert otp(b, "9876543202").status_code == 200
    assert otp(a, "9876543203").status_code == 200
    assert otp(b, "9876543204").status_code == 429


def test_bucket_is_exact_under_concurrency(limited):
    c = limited(rate_limit_capacity=20)
    barrier = threading.Barrier(50)

    def hit(i):
        barrier.wait()
        return otp(c, f"98765{i:05d}").status_code

    with ThreadPoolExecutor(50) as pool:
        codes = list(pool.map(hit, range(50)))
    assert codes.count(200) == 20 and codes.count(429) == 30


# -- per-mobile OTP limits -------------------------------------------------------------

def test_second_otp_within_cooldown_is_refused(limited):
    c = limited(otp_cooldown_s=30)
    assert otp(c, "9876543210").status_code == 200
    r = otp(c, "9876543210")
    assert r.status_code == 429 and code_of(r) == "OTP_TOO_SOON"
    assert 1 <= int(r.headers["Retry-After"]) <= 30
    assert otp(c, "9123456780").status_code == 200


def test_hourly_cap_per_mobile(limited):
    c = limited(otp_max_per_hour=3)
    for _ in range(3):
        assert otp(c, "9876543210").status_code == 200
    assert code_of(otp(c, "9876543210")) == "OTP_TOO_SOON"


def test_botnet_cannot_flood_one_mobile(limited):
    c = limited(otp_cooldown_s=30, trust_proxy=1)
    codes = [otp(c, "9876543210", ip=f"198.51.100.{i}").status_code for i in range(20)]
    assert codes.count(200) == 1


# -- per-OTP rules -------------------------------------------------------------------

def test_otp_is_single_use(limited):
    c = limited()
    t = otp(c).json()
    assert verify(c, t, t["demo_otp"]).status_code == 200
    r = verify(c, t, t["demo_otp"])
    assert r.status_code == 401 and code_of(r) == "OTP_USED"


def test_wrong_guesses_are_capped(limited):
    c = limited(otp_max_attempts=3)
    t = otp(c).json()
    for left in (2, 1, 0):
        r = verify(c, t, wrong(t["demo_otp"]))
        assert code_of(r) == "OTP_INVALID"
    r = verify(c, t, t["demo_otp"])
    assert r.status_code == 429 and code_of(r) == "OTP_LOCKED"
    assert code_of(verify(c, t, t["demo_otp"])) == "OTP_USED"


def test_wrong_guess_message_counts_down(limited):
    c = limited(otp_max_attempts=3)
    t = otp(c).json()
    assert "2 attempt(s) left" in verify(c, t, wrong(t["demo_otp"])).json()["error"]["message"]


def test_correct_otp_within_attempts_still_works(limited):
    c = limited(otp_max_attempts=3)
    t = otp(c).json()
    verify(c, t, wrong(t["demo_otp"]))
    verify(c, t, wrong(t["demo_otp"]))
    assert verify(c, t, t["demo_otp"]).status_code == 200


def test_two_simultaneous_verifies_only_one_wins(limited):
    c = limited()
    t = otp(c).json()
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return verify(c, t, t["demo_otp"]).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(go, range(8)))
    assert codes.count(200) == 1


# -- switches and failure behaviour -----------------------------------------------------

def test_limits_off_allows_floods_and_unlimited_guesses(limited):
    c = limited(rate_limit_enabled=False, rate_limit_capacity=2, otp_cooldown_s=30, otp_max_attempts=3)
    assert all(otp(c, "9876543210").status_code == 200 for _ in range(10))
    t = otp(c).json()
    assert all(code_of(verify(c, t, wrong(t["demo_otp"]))) == "OTP_INVALID" for _ in range(10))
    assert verify(c, t, t["demo_otp"]).status_code == 200


def test_redis_down_rate_limit_fails_open_otp_fails_closed(make_client):
    c = make_client(redis_url="redis://127.0.0.1:1/0")
    assert c.get("/healthz").status_code == 200
    r = otp(c)
    assert r.status_code == 503 and code_of(r) == "OTP_UNAVAILABLE"


def test_readyz_checks_redis(make_client, limited):
    assert limited().get("/readyz").status_code == 200
    down = make_client(redis_url="redis://127.0.0.1:1/0")
    r = down.get("/readyz")
    assert r.status_code == 503 and code_of(r) == "NOT_READY"
