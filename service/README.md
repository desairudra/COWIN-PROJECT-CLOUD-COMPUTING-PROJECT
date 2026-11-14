# Registration service (commit 11)

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
uvicorn app.main:app --reload --port 8000              # http://localhost:8000/docs
```

Tests (need a throwaway database, same as `db/tests`):

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin_test pytest -v
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
| `CORS_ORIGINS` | empty | comma-separated origins, only if the frontend is hosted elsewhere |
| `WEB_DIR` | `../web` | if it holds an `index.html`, it is served at `/` |

## Design

**Stateless (Sec 9.4).** Nothing about a user is kept in process memory, so the
load balancer can send any request to any replica, and the autoscaler can add or
remove replicas at any time (commit 14). Following CoWIN's own API shape
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

## Known gaps, closed by commit 12 (rate limiter + Redis)

- No limit yet on OTP requests per mobile or per client, so an OTP flood is possible. E4
  measures this before and after the limiter.
- A `txn_id` can be tried repeatedly until it expires, and it stays valid after a
  successful verify. Redis adds one-time use and a cap on wrong attempts.
  Until then, keep `OTP_LENGTH=6`.

The SMS step is simulated. The Sec 9.5 design puts it on SQS/SNS so sending never
blocks a registration.
