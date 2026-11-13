# Data layer: PostgreSQL schema and partitioning (commit 1)

The storage for our scaled-down CoWIN **registration module** (Week 2 report, Sec 9.2).
It holds OTP-verified mobile numbers and the beneficiaries registered under them,
and the database itself enforces the three rules from Sec 9.2:

| # | Rule (Sec 9.2) | How it is enforced |
|---|----------------|--------------------|
| 1 | No more than **4 beneficiaries per mobile number** | `registrations.beneficiary_count` with `CHECK (0..4)`, kept in step by an `AFTER INSERT/DELETE` trigger that locks the mobile's row. Safe under concurrent requests. |
| 2 | The **same photo ID is never registered twice** | `photo_id_registry` with `PRIMARY KEY (photo_id_type, photo_id_number)`, written by the same trigger in the same transaction. IDs are normalized first, so `1234 5678 9012` = `1234-5678-9012`. |
| 3 | A **retried request never creates a duplicate** | Client-supplied `idempotency_key`, `UNIQUE (mobile_number, idempotency_key)`, plus a SHA-256 `request_hash` so a reused key with a different body is rejected rather than silently replayed. |

Partitioning (Sec 9.4, "PostgreSQL declarative partitioning"): `registrations` and
`beneficiaries` are **hash-partitioned on `mobile_number`** (8 partitions by default).
A lookup by mobile touches one partition only.

## Layout

```
db/
├── migrations/               applied in name order
│   ├── 001_types_and_helpers.sql   enums (gender, 9 photo ID types), normalizers, partition helper
│   ├── 002_tables.sql              registrations, beneficiaries, photo_id_registry (+ partitions)
│   ├── 003_triggers.sql            limit of 4, photo ID claim, immutability, counter protection
│   └── 004_functions.sql           upsert_registration, add_beneficiary, list_beneficiaries, partition_balance
├── scripts/
│   ├── apply_migrations.sh   apply (or --reset and apply) to $DATABASE_URL
│   └── seed.py               synthetic data and a partition balance report
├── tests/                    42 pytest tests, incl. concurrency
├── docker-compose.yml        local postgres:16 with migrations auto-applied
└── requirements-dev.txt
```

## Quick start

```bash
# 1. Start PostgreSQL 16 (runs the migrations on first start)
docker compose -f db/docker-compose.yml up -d
docker compose -f db/docker-compose.yml exec db createdb -U cowin cowin_test

# 2. Run the tests (they drop and recreate the schema in cowin_test)
pip install -r db/requirements-dev.txt
TEST_DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin_test pytest db/tests -v

# 3. Optional: seed data and look at the partition balance
DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin python db/scripts/seed.py --mobiles 5000
```

Without Docker, use any PostgreSQL 13+ and run
`DATABASE_URL=... ./db/scripts/apply_migrations.sh --reset`.

Change the partition count (for example for E6) without editing SQL:
`PGOPTIONS='-c cowin.partitions=16' ./db/scripts/apply_migrations.sh --reset`.

## Schema

```mermaid
erDiagram
    registrations ||--o{ beneficiaries : "owns (max 4)"
    beneficiaries ||--|| photo_id_registry : "claims"
    registrations {
        text mobile_number PK "10 digits, starts 6-9"
        smallint beneficiary_count "0..4, trigger-maintained"
        timestamptz created_at
        timestamptz last_verified_at "refreshed on each OTP verify"
    }
    beneficiaries {
        text mobile_number PK "partition key"
        uuid beneficiary_id PK
        text full_name
        smallint birth_year "stored instead of age"
        gender_t gender
        photo_id_type_t photo_id_type "9 CoWIN ID types"
        text photo_id_number "normalized"
        text idempotency_key "UNIQUE per mobile"
        bytea request_hash "SHA-256 of request"
    }
    photo_id_registry {
        photo_id_type_t photo_id_type PK
        text photo_id_number PK "partition key"
        text mobile_number FK
        uuid beneficiary_id FK
    }
```

**Why a separate `photo_id_registry`?** PostgreSQL can only enforce a `UNIQUE`
constraint on a partitioned table if the constraint includes the partition key.
`beneficiaries` is partitioned by mobile, so a unique photo ID cannot be declared on
it: two mobiles in two partitions could hold the same ID. The registry is partitioned
by the photo ID itself, so its primary key is a true global guarantee.

**Why year of birth, not age?** Sec 9.2 lists "age". A stored age goes stale, so we
store the year of birth, as CoWIN did, and compute age when needed.

**Why is the 4-limit trigger `AFTER INSERT`?** With `INSERT ... ON CONFLICT DO NOTHING`
(the idempotent replay path), `BEFORE` triggers still fire for a row that is then
dropped as a duplicate. The counter would go up on a retry. `AFTER` triggers fire only
for rows that were really inserted.

**The OTP is not stored here.** OTPs live in Redis with a TTL (commit 3). PostgreSQL
only sees a mobile number after the OTP has been verified.

## Contract for the registration service (commit 2)

Every call is one statement, one transaction, and safe to retry.

| Endpoint (commit 2) | SQL call | Returns |
|---------------------|----------|---------|
| verify OTP (on success) | `SELECT * FROM upsert_registration(:mobile)` | `mobile_number, created, beneficiary_count` |
| add beneficiary | `SELECT * FROM add_beneficiary(:mobile, :idempotency_key, :full_name, :birth_year, :gender, :photo_id_type, :photo_id_number)` | `beneficiary_id, created, created_at`. `created = false` means a replayed retry |
| list beneficiaries | `SELECT * FROM list_beneficiaries(:mobile)` | rows with the photo ID **masked** (`XXXXXXXX9012`) |

`idempotency_key`: 8–64 characters from `[A-Za-z0-9_-]`. Suggested source: an
`Idempotency-Key` request header (the client generates a UUID once and resends it on
every retry). It is unique per mobile number.

### Error codes and suggested HTTP mapping

| SQLSTATE | Meaning | Suggested HTTP |
|----------|---------|----------------|
| `CW001` | Mobile already has 4 beneficiaries | 409 Conflict |
| `CW002` | Mobile not registered (OTP not verified) | 401 / 404 |
| `CW003` | Photo ID already registered (the number is not echoed in the message) | 409 Conflict |
| `CW004` | Idempotency key reused with a different body | 422 Unprocessable |
| `CW005` | Tried to change an immutable column or the counter | 400 / 500 (bug) |
| `23514` | Invalid input (mobile format, birth year, name, ID format, key format) | 400 / 422 |
| `22P02` | Unknown gender or photo ID type | 400 / 422 |

With psycopg: `except psycopg.Error as e: e.sqlstate`.

## Notes for later commits

- **Commit 3 (rate limiter):** OTP storage and per-mobile OTP limits sit in Redis, in
  front of this layer. No schema change needed.
- **Commit 4 (sharding):** the shard key should be `mobile_number`, the same as the
  partition key, so a mobile and its beneficiaries stay on one shard and rule 1 stays a
  single-row lock. Rule 2 does **not** shard by mobile: the same photo ID could arrive
  on two different shards. Options: route `photo_id_registry` by a hash of the photo
  ID to its own shard (a cross-shard write, needing two-phase commit or a
  claim-then-insert step with cleanup), or keep a single global registry. Measure the
  cost in E6.
- **E6 (1 DB vs partitioned vs sharded):** `partition_balance` gives rows per
  partition; `seed.py` generates the load. The partition count is set by
  `cowin.partitions`.
- **Sec 7.1 (privacy):** only the minimum fields are stored, `list_beneficiaries` masks
  the photo ID, and `CW003` errors do not echo the number. Encryption at rest is a
  deployment setting (RDS), not part of this schema.

Report references: Sec 9.2 (component and rules), Sec 9.4 (PostgreSQL, partitioning).
