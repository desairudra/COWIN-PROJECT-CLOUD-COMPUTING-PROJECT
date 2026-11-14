# Registration service (commit 2)

FastAPI service for our scaled-down CoWIN registration module (Week 2 report
Sec 9.2, 9.4). It sits on the PostgreSQL schema from commit 1.

| # | Operation | Endpoint | Notes |
|---|-----------|----------|-------|
| 1 | Request OTP | `POST /api/v1/otp/request` `{"mobile"}` | returns `txn_id` (and `demo_otp` in demo mode) |
| 2 | Verify OTP | `POST /api/v1/otp/verify` `{"txn_id","otp"}` | registers the mobile, returns a Bearer `token` |
| 3 | Add beneficiary | `POST /api/v1/beneficiaries` + `Idempotency-Key` header | `201` new, `200` + `Idempotent-Replayed: true` on retry |
| 4 | List beneficiaries | `GET /api/v1/beneficiaries` | photo ID masked except the last 4 characters |

Also `GET /healthz` (liveness), `GET /readyz` (checks the DB), and interactive docs at `/docs`.
Every response carries `X-Served-By: <hostname>` so the load-balancer experiments
(E2, E3) can see which replica answered.

## Run locally

```bash
docker compose -f db/docker-compose.yml up -d          # PostgreSQL + migrations (commit 1)
cd service
pip install -r requirements.txt
export DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin
export SECRET_KEY=change-me-to-a-long-random-string
export REDIS_URL=redis://localhost:6379/0
uvicorn app.main:app --reload --port 8000              # http://localhost:8000/docs
```

Tests (need a throwaway database and Redis; `db/docker-compose.yml` starts both):

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin_test \
TEST_REDIS_URL=redis://localhost:6379/15 pytest -v      # 42 tests
```

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://cowin:cowin@localhost:5432/cowin` | PostgreSQL |
| `SECRET_KEY` | random (dev only) | **Required outside `APP_ENV=dev`, and the same on every replica** |
| `APP_ENV` | `dev` | anything other than `dev` makes `SECRET_KEY` mandatory |
| `OTP_DELIVERY` | `demo` | `demo` returns the OTP in the response (no SMS gateway); `log` only logs it |
| `OTP_LENGTH` | `6` | digits in the OTP (the report's Sec 4.1 says CoWIN used 4) |
| `OTP_TTL_SECONDS` | `180` | OTP lifetime |
| `SESSION_TTL_SECONDS` | `900` | session token lifetime |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | connections per replica |
| `REDIS_URL` | empty | Redis for limits and OTP state; **required outside `APP_ENV=dev`** |
| `RATE_LIMIT_ENABLED` | `true` | `false` turns all limits off (used by E4's baseline) |
| `RATE_LIMIT_CAPACITY` / `RATE_LIMIT_WINDOW_S` | `100` / `300` | calls per client per window |
| `OTP_COOLDOWN_S` / `OTP_MAX_PER_HOUR` | `30` / `5` | OTPs per mobile |
| `OTP_MAX_ATTEMPTS` | `3` | wrong guesses per OTP |
| `TRUST_PROXY` | `0` | `1` behind the load balancer |
| `CORS_ORIGINS` | empty | comma-separated origins, only if the frontend is hosted elsewhere |
| `WEB_DIR` | `../web` | if it holds an `index.html`, it is served at `/` |

## Design

**Stateless (Sec 9.4).** Nothing about a user is kept in process memory, so the
load balancer can send any request to any replica, and the autoscaler can add or
remove replicas at any time (commit 5). Following CoWIN's own API shape
(generate OTP returns a `txnId`, confirm OTP returns a token):

- the `txn_id` is a signed token holding the mobile number, an expiry, and an
  HMAC of the OTP keyed with `SECRET_KEY`. The OTP cannot be recovered from it,
  even by brute force, without the key;
- the session is a signed token holding the mobile number and an expiry.

**Retry-safe.**
- Verify OTP calls `upsert_registration`, so repeating it does not duplicate the mobile.
- Add beneficiary requires an `Idempotency-Key`. A retry with the same key and body
  returns the original record (`200`, `Idempotent-Replayed: true`). The same key with a
  different body returns `422`. The guarantee comes from the database (commit 1), so
  it holds across replicas and concurrent retries. The tests fire 8 parallel retries
  at two replicas and get exactly one row.
- A dropped database connection is retried once inside the service (every call is
  idempotent). If the database stays down, the response is `503` with `Retry-After: 1`.

**Errors** always look like `{"error": {"code", "message", "details?"}}`:

| HTTP | code | when |
|------|------|------|
| 400 | `IDEMPOTENCY_KEY_REQUIRED` / `_INVALID` | header missing or badly formed |
| 401 | `OTP_INVALID`, `OTP_EXPIRED`, `UNAUTHENTICATED` | wrong/expired OTP, missing/expired session |
| 409 | `BENEFICIARY_LIMIT` | 5th beneficiary on one mobile |
| 409 | `PHOTO_ID_IN_USE` | photo ID already registered (number not echoed) |
| 422 | `INVALID_INPUT` (+ `details`), `IDEMPOTENCY_KEY_REUSED` | bad fields, key reused |
| 503 | `DB_UNAVAILABLE` | database unreachable |

## Rate limits and OTP protection (commit 12)

Redis holds the limits and OTP state, so every replica enforces the same counts.

| Layer | Rule | Stops | Response |
| --- | --- | --- | --- |
| Per client IP | Token bucket: 100 calls per 5 minutes (Sec 4.2) | One machine flooding the API | `429 RATE_LIMITED` + `Retry-After` |
| Per mobile | One OTP every 30 s, at most 5 an hour | A botnet "SMS-bombing" one phone | `429 OTP_TOO_SOON` + `Retry-After` |
| Per OTP | 3 wrong guesses, then locked | Guessing the code | `429 OTP_LOCKED` |
| Per OTP | Usable once | Replaying a stolen code | `401 OTP_USED` |

Every `/api/` response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`. Health checks
and the web page are never limited.

The bucket is a Lua script that runs inside Redis, so concurrent requests from many replicas
cannot overspend it: 50 simultaneous requests against 20 tokens give exactly 20 successes
(tested). Behind the load balancer set `TRUST_PROXY=1`. The client is then the last
`X-Forwarded-For` entry, the one the load balancer adds, so a client cannot dodge its bucket
by sending a fake header.

If Redis is down, rate limits **fail open** (requests are allowed and a warning is logged, so a
cache outage does not take the site down) and OTP requests and checks **fail closed**
(`503 OTP_UNAVAILABLE`, because skipping the guess limit would be a security hole).
`/readyz` returns `503 NOT_READY`, so Kubernetes stops sending traffic to that pod.

Experiment **E4** measures all of this: [`experiments/e4_otp_flood/`](../experiments/e4_otp_flood/README.md).

The SMS step is simulated. The Sec 9.5 design puts it on SQS/SNS so sending never
blocks a registration.
