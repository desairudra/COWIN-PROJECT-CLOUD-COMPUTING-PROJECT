#!/usr/bin/env python3
"""Fill the registration tables with synthetic data and print partition balance.

Every row goes through upsert_registration() / add_beneficiary(), so all the
schema rules (max 4, unique photo ID, idempotency) are applied to seeded data
too. Useful for:
  * checking that hash partitioning on mobile number spreads rows evenly
  * giving the API (commit 2) and the E6 experiment (commit 4) a warm database

Usage:
    pip install -r db/requirements-dev.txt
    DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin \
        python db/scripts/seed.py --mobiles 10000

No real personal data is generated: mobile numbers are random 10-digit
numbers starting 6-9, names are "Person <n>", and photo ID numbers are random
strings in the right shape for each ID type.
"""
import argparse
import os
import random
import string
import time
import uuid

import psycopg

ID_TYPES = ["AADHAAR", "DRIVING_LICENSE", "PAN", "PASSPORT", "PENSION_PASSBOOK",
            "NPR_SMART_CARD", "VOTER_ID", "UDID", "RATION_CARD"]
GENDERS = ["MALE", "FEMALE", "OTHER"]


def fake_photo_id(rng: random.Random, id_type: str) -> str:
    if id_type == "AADHAAR":
        return "".join(rng.choices(string.digits, k=12))
    if id_type == "PAN":
        return ("".join(rng.choices(string.ascii_uppercase, k=5))
                + "".join(rng.choices(string.digits, k=4)) + rng.choice(string.ascii_uppercase))
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=rng.randint(8, 15)))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mobiles", type=int, default=1000, help="number of mobile numbers (default 1000)")
    p.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL",
                                                   "postgresql://cowin:cowin@localhost:5432/cowin"))
    args = p.parse_args()

    rng = random.Random(args.seed)
    mobiles = {f"{rng.randint(6, 9)}{rng.randint(0, 10**9 - 1):09d}" for _ in range(args.mobiles)}
    beneficiaries = []
    for m in mobiles:
        for _ in range(rng.choices([1, 2, 3, 4], weights=[40, 30, 20, 10])[0]):
            t = rng.choice(ID_TYPES)
            beneficiaries.append((m, uuid.UUID(int=rng.getrandbits(128)).hex,
                                  f"Person {len(beneficiaries)}", rng.randint(1940, 2006),
                                  rng.choice(GENDERS), t, fake_photo_id(rng, t)))

    start = time.perf_counter()
    with psycopg.connect(args.dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.executemany("SELECT upsert_registration(%s)", [(m,) for m in mobiles])
        skipped = 0
        for row in beneficiaries:
            try:
                cur.execute("SELECT add_beneficiary(%s, %s, %s, %s, %s, %s, %s)", row)
            except psycopg.errors.lookup("CW003"):   # random photo ID collided; skip it
                skipped += 1
        elapsed = time.perf_counter() - start

        print(f"Seeded {len(mobiles)} mobiles and {len(beneficiaries) - skipped} beneficiaries "
              f"in {elapsed:.1f}s ({(len(beneficiaries) - skipped) / elapsed:.0f} adds/s, "
              f"single connection; not a benchmark)")
        if skipped:
            print(f"Skipped {skipped} rows with a colliding random photo ID")

        print(f"\n{'table':<20}{'partition':<24}{'rows':>8}{'vs mean':>10}")
        rows = cur.execute("SELECT table_name, partition, row_count FROM partition_balance "
                           "ORDER BY table_name, partition").fetchall()
        for table in sorted({r[0] for r in rows}):
            part = [r for r in rows if r[0] == table]
            mean = sum(r[2] for r in part) / len(part)
            for _, name, n in sorted(part, key=lambda r: int(r[1].rsplit("_p", 1)[1])):
                print(f"{table:<20}{name:<24}{n:>8}{(n - mean) / mean:>+10.1%}")


if __name__ == "__main__":
    main()
