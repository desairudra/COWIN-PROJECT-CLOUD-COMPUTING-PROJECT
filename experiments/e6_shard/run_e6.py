#!/usr/bin/env python3
"""E6: one table vs a partitioned table vs registrations sharded over 2 and 4 databases.

    docker compose -f db/shards/docker-compose.yml up -d --wait
    python experiments/e6_shard/run_e6.py                 # about 4 minutes

Each setup starts from an empty schema, gets PRELOAD registered mobiles, then
PROCS x THREADS clients sign people up as fast as they can for DURATION seconds:
register the mobile, add one beneficiary, list. The calls go through the
service's own Database / ShardedDatabase classes, so the routing measured here
is the routing the service uses.
"""
import argparse
import multiprocessing as mp
import pathlib
import statistics
import sys
import time
from datetime import datetime, timezone

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "service"))
from app.db import Database, ShardedDatabase  # noqa: E402

MIGRATIONS = sorted((ROOT / "db" / "migrations").glob("*.sql"))
DEFAULT_DSNS = [f"postgresql://cowin:cowin@localhost:{p}/cowin" for p in (5441, 5442, 5443, 5444)]

SETUPS = {
    "one-table": {"dbs": 1, "partitions": 1, "label": "1 database, 1 table"},
    "partitioned": {"dbs": 1, "partitions": 8, "label": "1 database, 8 partitions"},
    "sharded-2": {"dbs": 2, "partitions": 8, "label": "2 databases (shards), 8 partitions each"},
    "sharded-4": {"dbs": 4, "partitions": 8, "label": "4 databases (shards), 8 partitions each"},
}


class Person:
    class _V:
        def __init__(self, v):
            self.value = v

    def __init__(self, n):
        self.full_name = f"Load Test {n}"
        self.birth_year = 1940 + n % 60
        self.gender = self._V(("MALE", "FEMALE", "OTHER")[n % 3])
        self.photo_id_type = self._V("PAN")
        self.photo_id_number = f"E6P{n:012d}"


def reset(dsn, partitions):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET client_min_messages = warning")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(f"SET cowin.partitions = '{partitions}'")
        for path in MIGRATIONS:
            conn.execute(path.read_text())


def make_db(dsns, pool):
    return ShardedDatabase(dsns, 1, pool) if len(dsns) > 1 else Database(dsns[0], 1, pool)


def shard_of(db, mobile):
    return db.shard_index(mobile) if isinstance(db, ShardedDatabase) else 0


def preload(dsns, n):
    router = make_db(dsns, 1)
    per = [[] for _ in dsns]
    for i in range(n):
        m = str(9000000000 + i)
        per[shard_of(router, m)].append(m)
    for dsn, mobiles in zip(dsns, per):
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany("SELECT upsert_registration(%s)", [(m,) for m in mobiles])
                cur.executemany(
                    "SELECT add_beneficiary(%s, %s, %s, %s, %s, 'PAN', %s)",
                    [(m, f"preload-{m}", f"Preload {m}", 1950 + int(m) % 50, "FEMALE", f"PRE{m}") for m in mobiles])
            conn.commit()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("VACUUM ANALYZE")


def worker(args):
    dsns, proc, threads, duration, start_at = args
    import threading
    db = make_db(dsns, threads)
    db.open()
    for d in (db.shards if isinstance(db, ShardedDatabase) else [db]):
        d.pool.wait(timeout=30)
    lat = {"register": [], "add": [], "list": []}
    done, errors = [0], [0]
    lock = threading.Lock()

    def run(t):
        n = 0
        while time.time() < start_at:
            time.sleep(0.01)
        end = start_at + duration
        local = {"register": [], "add": [], "list": []}
        ok = err = 0
        while time.time() < end:
            seq = (proc * 64 + t) * 1_000_000 + n
            mobile = str(6000000000 + seq)
            n += 1
            try:
                a = time.perf_counter()
                db.upsert_registration(mobile)
                b = time.perf_counter()
                db.add_beneficiary(mobile, f"e6-{seq:012d}", Person(seq))
                c = time.perf_counter()
                db.list_beneficiaries(mobile)
                d = time.perf_counter()
            except Exception:
                err += 1
                continue
            local["register"].append((b - a) * 1000)
            local["add"].append((c - b) * 1000)
            local["list"].append((d - c) * 1000)
            ok += 1
        with lock:
            for k in lat:
                lat[k].extend(local[k])
            done[0] += ok
            errors[0] += err

    ts = [threading.Thread(target=run, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    db.close()
    return done[0], errors[0], lat


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def scatter_query(dsns):
    """A question about everyone (people born before 1960) has to ask every shard."""
    sql = "SELECT count(*) FROM beneficiaries WHERE birth_year < 1960"
    times, total = [], 0
    for _ in range(3):
        t = time.perf_counter()
        total = 0
        for d in dsns:
            with psycopg.connect(d) as conn:
                total += conn.execute(sql).fetchone()[0]
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times), total


def spread(dsns, partitions):
    per_db = []
    for d in dsns:
        with psycopg.connect(d) as conn:
            per_db.append(conn.execute("SELECT count(*) FROM registrations").fetchone()[0])
    with psycopg.connect(dsns[0]) as conn:
        per_part = [r[0] for r in conn.execute(
            "SELECT count(*) FROM registrations GROUP BY tableoid ORDER BY tableoid").fetchall()]
    return per_db, per_part


def run_setup(name, all_dsns, a):
    cfg = SETUPS[name]
    dsns = all_dsns[:cfg["dbs"]]
    print(f"==> {name}: {cfg['label']}")
    for d in dsns:
        reset(d, cfg["partitions"])
    if a.preload:
        t = time.time()
        preload(dsns, a.preload)
        print(f"    preloaded {a.preload} mobiles in {time.time() - t:.0f} s")
    start_at = time.time() + 3
    with mp.get_context("spawn").Pool(a.procs) as pool:
        results = pool.map(worker, [(dsns, p, a.threads, a.duration, start_at) for p in range(a.procs)])
    done = sum(r[0] for r in results)
    errors = sum(r[1] for r in results)
    lat = {k: [v for r in results for v in r[2][k]] for k in ("register", "add", "list")}
    q_ms, q_total = scatter_query(dsns)
    per_db, per_part = spread(dsns, cfg["partitions"])
    row = {"name": name, **cfg, "signups": done, "errors": errors, "rate": done / a.duration,
           "calls": done * 3 / a.duration, "scatter_ms": q_ms, "scatter_rows": q_total,
           "per_db": per_db, "per_part": per_part}
    for k, v in lat.items():
        row[f"{k}_p50"], row[f"{k}_p95"], row[f"{k}_p99"] = pct(v, 50), pct(v, 95), pct(v, 99)
    allv = [x for v in lat.values() for x in v]
    row["p50"], row["p95"], row["p99"] = pct(allv, 50), pct(allv, 95), pct(allv, 99)
    print(f"    {row['rate']:.0f} sign-ups/s, p95 {row['p95']:.1f} ms per call, {errors} errors")
    return row


def evenness(xs):
    return max(xs) / min(xs) if xs and min(xs) else float("inf")


def write_results(out, rows, a):
    base = rows[0]
    lines = ["# E6: one table vs partitioned vs sharded", "",
             f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Each setup: empty schema, {a.preload} mobiles "
             f"preloaded, then {a.procs} x {a.threads} clients signing people up for {a.duration} s "
             "(register the mobile, add a beneficiary, list = 3 database calls). Every PostgreSQL "
             "server is limited to 1 CPU (`db/shards/docker-compose.yml`).", ""]
    best = max(rows, key=lambda r: r["rate"])
    if best is base:
        head = (f"**No setup beat the single table here ({base['rate']:.0f} sign-ups/s).** That is what happens "
                "when the databases share the same CPUs: check that each one is limited to its own CPU.")
    else:
        head = (f"**{best['label']} handled {best['rate']:.0f} sign-ups/s, "
                f"{best['rate'] / base['rate']:.1f}x the single table ({base['rate']:.0f}/s).** "
                f"p95 per call: {base['p95']:.1f} ms on the single table, {best['p95']:.1f} ms here.")
    lines += [head, "",
              "| Setup | Sign-ups/s | DB calls/s | vs 1 table | p50 ms | p95 ms | p99 ms | Errors | Query over everyone |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['label']} | {r['rate']:.0f} | {r['calls']:.0f} | {r['rate'] / base['rate']:.2f}x | "
                     f"{r['p50']:.1f} | {r['p95']:.1f} | {r['p99']:.1f} | {r['errors']} | {r['scatter_ms']:.0f} ms |")
    lines += ["", "p95 per call type (ms):", "",
              "| Setup | Register | Add beneficiary | List |", "| --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['label']} | {r['register_p95']:.1f} | {r['add_p95']:.1f} | {r['list_p95']:.1f} |")
    lines += ["", "How evenly the mobiles were spread (registrations per database, and per partition "
              "in the first database):", "", "| Setup | Per database | Largest / smallest | Per partition (database 1) |",
              "| --- | --- | --- | --- |"]
    for r in rows:
        dbs = " / ".join(str(x) for x in r["per_db"])
        parts = " / ".join(str(x) for x in r["per_part"])
        lines.append(f"| {r['label']} | {dbs} | {evenness(r['per_db']):.2f} | {parts} |")
    lines += ["",
              "Notes:",
              "- Adding a beneficiary on the sharded setups is two database calls inside: claim the photo ID on "
              "the shard that owns that ID, then insert on the mobile's shard (`ShardedDatabase` in "
              "`service/app/db.py`). That is the price of keeping photo IDs unique across databases.",
              "- \"Query over everyone\" (people born before 1960) has to ask every shard and add the "
              "answers; that is the price of sharding for reports and analytics.",
              "- Partitioning alone keeps one server: it makes each table and index smaller (faster vacuum, "
              "cheaper index maintenance), but it can't use more than one server's CPU.",
              "- Sharding only adds capacity when each database has its own CPU. Here each container is capped "
              "at 1 CPU to stand in for separate machines; the client runs on the same laptop, so at some point "
              "the laptop itself becomes the limit.", "",
              "![E6 chart](e6-chart.svg)", ""]
    (out / "results.md").write_text("\n".join(lines))


def write_svg(out, rows):
    W, H, L, T = 900, 420, 230, 70
    bw = 26
    maxr = max(r["rate"] for r in rows) * 1.15
    maxp = max(r["p95"] for r in rows) * 1.15
    x = lambda v, m: L + (W - L - 60) * v / m
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>',
         '<text x="20" y="28" font-size="15" font-weight="700" fill="#17242b">E6: sign-ups per second and p95 latency per call</text>',
         '<rect x="20" y="40" width="10" height="10" fill="#1f5fa8"/><text x="34" y="49" fill="#17242b">sign-ups/s</text>',
         '<rect x="120" y="40" width="10" height="10" fill="#d68a00"/><text x="134" y="49" fill="#17242b">p95 ms per call</text>']
    for i, r in enumerate(rows):
        y = T + i * 85
        o.append(f'<text x="{L - 10}" y="{y + 22}" text-anchor="end" fill="#17242b">{r["label"]}</text>')
        o.append(f'<rect x="{L}" y="{y}" width="{x(r["rate"], maxr) - L:.1f}" height="{bw}" fill="#1f5fa8"/>')
        o.append(f'<text x="{x(r["rate"], maxr) + 6:.1f}" y="{y + 18}" fill="#17242b">{r["rate"]:.0f}/s</text>')
        o.append(f'<rect x="{L}" y="{y + bw + 4}" width="{x(r["p95"], maxp) - L:.1f}" height="{bw - 8}" fill="#d68a00"/>')
        o.append(f'<text x="{x(r["p95"], maxp) + 6:.1f}" y="{y + bw + 17}" fill="#17242b">{r["p95"]:.1f} ms</text>')
    o.append("</svg>")
    (out / "e6-chart.svg").write_text("\n".join(o))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--setups", default="one-table,partitioned,sharded-2,sharded-4")
    ap.add_argument("--dsns", default=",".join(DEFAULT_DSNS), help="4 database URLs, comma-separated")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--preload", type=int, default=20000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    dsns = [d.strip() for d in a.dsns.split(",") if d.strip()]
    names = [n.strip() for n in a.setups.split(",")]
    for n in names:
        if n not in SETUPS:
            sys.exit(f"unknown setup {n}; choose from {', '.join(SETUPS)}")
        if SETUPS[n]["dbs"] > len(dsns):
            sys.exit(f"{n} needs {SETUPS[n]['dbs']} databases, got {len(dsns)}")
    for d in dsns[:max(SETUPS[n]["dbs"] for n in names)]:
        try:
            psycopg.connect(d, connect_timeout=3).close()
        except psycopg.OperationalError:
            sys.exit(f"cannot reach {d}\nStart them: docker compose -f db/shards/docker-compose.yml up -d --wait")

    out = pathlib.Path(a.out or ROOT / "docs" / "evidence" / f"16-e6-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    rows = [run_setup(n, dsns, a) for n in names]
    write_results(out, rows, a)
    write_svg(out, rows)
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
