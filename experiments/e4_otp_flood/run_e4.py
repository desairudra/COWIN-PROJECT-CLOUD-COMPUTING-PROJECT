#!/usr/bin/env python3
"""E4: OTP flood, with the commit 12 limits off and then on.

Starts the registration service twice (RATE_LIMIT_ENABLED=false, then true) and, for
DURATION seconds each time, runs three attackers next to 30 real users:

  A  one machine asks for OTPs for random numbers, 20 per second   -> SMS cost
  B  50 machines ask for OTPs for one victim's number, 10 per second -> victim's phone
  C  one machine guesses one OTP, 10 per second                     -> guesses checked
  users: 30 people from different IPs: request OTP, verify, add a person, list

    python experiments/e4_otp_flood/run_e4.py --db postgresql://cowin:cowin@localhost:5432/cowin \\
        --redis redis://localhost:6379/0

Needs the commit 1 schema in that database, and Redis. Writes to docs/evidence/12-e4-<time>/.
"""
import argparse
import csv
import json
import os
import pathlib
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Recorder:
    def __init__(self):
        self.rows, self.lock = [], threading.Lock()

    def add(self, **row):
        with self.lock:
            self.rows.append(row)


def call(base, method, path, body=None, ip="127.0.0.1", token=None, headers=None):
    h = {"Content-Type": "application/json", "X-Forwarded-For": ip, **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, data = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, data = e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        status, data = 0, {"error": {"code": type(e).__name__}}
    return status, data, round((time.monotonic() - t) * 1000)


def code(data):
    return (data.get("error") or {}).get("code", "") if isinstance(data, dict) else ""


def mobile():
    return f"{random.choice('6789')}{random.randint(0, 10**9 - 1):09d}"


def paced(rate, duration, fn):
    t0 = time.monotonic()
    n = 0
    with ThreadPoolExecutor(max_workers=max(4, int(rate))) as pool:
        while (elapsed := time.monotonic() - t0) < duration:
            pool.submit(fn, n, elapsed)
            n += 1
            time.sleep(max(0, t0 + n / rate - time.monotonic()))


def attacker_a(base, rec, duration):
    def go(n, t):
        s, d, ms = call(base, "POST", "/api/v1/otp/request", {"mobile": mobile()}, ip="198.51.100.66")
        rec.add(actor="A", t=round(t, 2), step="otp_request", status=s, code=code(d), ms=ms)
    paced(20, duration, go)


def attacker_b(base, rec, duration, victim):
    def go(n, t):
        s, d, ms = call(base, "POST", "/api/v1/otp/request", {"mobile": victim}, ip=f"203.0.113.{n % 50 + 1}")
        rec.add(actor="B", t=round(t, 2), step="otp_request", status=s, code=code(d), ms=ms)
    paced(10, duration, go)


def attacker_c(base, rec, duration, victim):
    state = {"txn": None, "lock": threading.Lock()}

    def new_txn(t):
        s, d, ms = call(base, "POST", "/api/v1/otp/request", {"mobile": victim}, ip="192.0.2.77")
        rec.add(actor="C", t=round(t, 2), step="otp_request", status=s, code=code(d), ms=ms)
        return d.get("txn_id") if s == 200 else None

    def go(n, t):
        with state["lock"]:
            if state["txn"] is None:
                state["txn"] = new_txn(t)
            txn = state["txn"]
        if txn is None:
            return
        s, d, ms = call(base, "POST", "/api/v1/otp/verify",
                        {"txn_id": txn, "otp": f"{random.randint(0, 999999):06d}"}, ip="192.0.2.77")
        rec.add(actor="C", t=round(t, 2), step="guess", status=s, code=code(d), ms=ms)
        if code(d) in ("OTP_LOCKED", "OTP_USED", "OTP_EXPIRED"):
            with state["lock"]:
                if state["txn"] == txn:
                    state["txn"] = None
    paced(10, duration, go)


def real_user(base, rec, i, start_at):
    time.sleep(start_at)
    ip, m, t0 = f"10.20.0.{i + 1}", mobile(), time.monotonic()
    steps = []

    def step(name, *args, **kw):
        s, d, ms = call(base, *args, ip=ip, **kw)
        steps.append((name, s, code(d), ms))
        return s, d

    s, d = step("otp_request", "POST", "/api/v1/otp/request", {"mobile": m})
    ok = s == 200
    if ok:
        s, d = step("otp_verify", "POST", "/api/v1/otp/verify", {"txn_id": d["txn_id"], "otp": d["demo_otp"]})
        ok = s == 200
    if ok:
        token = d["token"]
        person = {"full_name": f"Test User {i}", "birth_year": 1960 + i, "gender": "FEMALE",
                  "photo_id_type": "PAN", "photo_id_number": f"E4{uuid.uuid4().hex[:8].upper()}"}
        s, d = step("add_person", "POST", "/api/v1/beneficiaries", person, token=token,
                    headers={"Idempotency-Key": uuid.uuid4().hex})
        ok = s == 201
    if ok:
        s, d = step("list", "GET", "/api/v1/beneficiaries", token=token)
        ok = s == 200 and d.get("count") == 1
    for name, s, c, ms in steps:
        rec.add(actor="user", t=round(start_at, 2), step=name, status=s, code=c, ms=ms)
    rec.add(actor="user", t=round(start_at, 2), step="flow", status=200 if ok else 0,
            code="" if ok else steps[-1][2] or str(steps[-1][1]), ms=round((time.monotonic() - t0) * 1000))


def start_service(args, enabled):
    env = {**os.environ, "DATABASE_URL": args.db, "REDIS_URL": args.redis, "APP_ENV": "dev",
           "SECRET_KEY": "e4-experiment-secret-key-0123", "RATE_LIMIT_ENABLED": str(enabled).lower(),
           "TRUST_PROXY": "1", "OTP_DELIVERY": "demo", "LOG_LEVEL": "WARNING", "WEB_DIR": "/nonexistent"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port),
                             "--log-level", "warning"], cwd=ROOT / "service", env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(60):
        if call(base, "GET", "/readyz")[0] == 200:
            return proc, base
        time.sleep(0.5)
    proc.kill()
    sys.exit("service did not start:\n" + proc.stderr.read().decode()[-2000:])


def run_mode(args, enabled):
    import redis
    redis.Redis.from_url(args.redis).flushdb()
    proc, base = start_service(args, enabled)
    rec = Recorder()
    victim_b, victim_c = mobile(), mobile()
    threads = [threading.Thread(target=attacker_a, args=(base, rec, args.duration)),
               threading.Thread(target=attacker_b, args=(base, rec, args.duration, victim_b)),
               threading.Thread(target=attacker_c, args=(base, rec, args.duration, victim_c))]
    threads += [threading.Thread(target=real_user, args=(base, rec, i, i * args.duration / args.users))
                for i in range(args.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    proc.terminate()
    proc.wait(timeout=10)
    return rec.rows


def summarize(rows):
    def pick(actor, step):
        return [r for r in rows if r["actor"] == actor and r["step"] == step]
    a, b = pick("A", "otp_request"), pick("B", "otp_request")
    guesses = pick("C", "guess")
    flows = pick("user", "flow")
    flow_ms = sorted(r["ms"] for r in flows if r["status"] == 200)
    return {
        "a_sent": len(a), "a_otps": sum(r["status"] == 200 for r in a), "a_blocked": sum(r["status"] == 429 for r in a),
        "b_sent": len(b), "b_otps": sum(r["status"] == 200 for r in b), "b_blocked": sum(r["status"] == 429 for r in b),
        "c_tries": len(guesses), "c_checked": sum(r["code"] == "OTP_INVALID" for r in guesses),
        "c_otps": sum(r["status"] == 200 for r in pick("C", "otp_request")),
        "users": len(flows), "users_ok": sum(r["status"] == 200 for r in flows),
        "user_fail_codes": sorted({r["code"] for r in flows if r["status"] != 200}),
        "p50": statistics.median(flow_ms) if flow_ms else 0,
        "p95": flow_ms[max(0, int(len(flow_ms) * 0.95) - 1)] if flow_ms else 0,
        "user_429": sum(r["status"] == 429 for r in rows if r["actor"] == "user" and r["step"] != "flow"),
    }


def bar_svg(off, on, path):
    attacks = [("Bot A: OTP texts sent", off["a_otps"], on["a_otps"]),
               ("Botnet B: texts to one victim", off["b_otps"], on["b_otps"]),
               ("Guesser C: guesses checked", off["c_checked"], on["c_checked"])]
    users = off["users"] or 1
    items = attacks + [("Real users signed up", off["users_ok"], on["users_ok"])]
    top = max(max(o, n) for _, o, n in attacks) or 1
    W, L, row = 900, 250, 70
    H = 70 + row * len(items) + 50
    span = W - L - 110
    scale = lambda i, v: span * v / (users if i == len(attacks) else top)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="13">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           '<text x="24" y="30" font-size="16" font-weight="700" fill="#17242b">E4: limits stop the attacks, real users still get through</text>',
           '<rect x="24" y="44" width="12" height="12" fill="#c0362c"/><text x="42" y="54" fill="#5b6b71">limits off</text>',
           '<rect x="120" y="44" width="12" height="12" fill="#0b8a6f"/><text x="138" y="54" fill="#5b6b71">limits on</text>']
    for i, (label, o, n) in enumerate(items):
        y = 80 + i * row + (20 if i == len(attacks) else 0)
        if i == len(attacks):
            out.append(f'<line x1="24" x2="{W - 24}" y1="{y - 12}" y2="{y - 12}" stroke="#d3dcd9"/>')
            out.append(f'<text x="{L - 12}" y="{y + 40}" text-anchor="end" fill="#5b6b71" font-size="11.5">full bar = all {users}</text>')
        out.append(f'<text x="{L - 12}" y="{y + 22}" text-anchor="end" fill="#17242b">{label}</text>')
        for j, (v, color) in enumerate(((o, "#c0362c"), (n, "#0b8a6f"))):
            yy, w = y + j * 22, max(1, scale(i, v))
            shown = f"{v} of {users}" if i == len(attacks) else str(v)
            out.append(f'<rect x="{L}" y="{yy + 4}" width="{w:.1f}" height="18" fill="{color}"/>')
            out.append(f'<text x="{L + w + 6:.1f}" y="{yy + 18}" fill="#17242b">{shown}</text>')
    out.append("</svg>")
    pathlib.Path(path).write_text("\n".join(out))


def write_results(out, args, off, on):
    pct = lambda n, d: f"{n / d:.0%}" if d else "n/a"
    lines = [
        "# E4: OTP flood, limits off vs on",
        "",
        f"Run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}, {args.duration} s per mode, "
        f"{args.users} real users per mode, service on one machine with Redis and PostgreSQL.",
        "",
        f"**With the limits on, bot A got {on['a_otps']} OTP texts sent instead of {off['a_otps']}, "
        f"the victim's phone got {on['b_otps']} instead of {off['b_otps']}, and the guesser had "
        f"{on['c_checked']} guesses checked instead of {off['c_checked']}. "
        f"{on['users_ok']} of {on['users']} real users signed up "
        f"({off['users_ok']} of {off['users']} with the limits off).**",
        "",
        "| Actor | Measure | Limits off | Limits on |",
        "| --- | --- | --- | --- |",
        f"| A: one machine, random numbers, 20/s | OTP texts sent (SMS cost) | {off['a_otps']} of {off['a_sent']} | {on['a_otps']} of {on['a_sent']} |",
        f"| | Requests refused (429) | {off['a_blocked']} | {on['a_blocked']} |",
        f"| B: 50 machines, one victim, 10/s | OTP texts to the victim's phone | {off['b_otps']} of {off['b_sent']} | {on['b_otps']} of {on['b_sent']} |",
        f"| C: guessing one OTP, 10/s | Guesses actually checked | {off['c_checked']} of {off['c_tries']} | {on['c_checked']} of {on['c_tries']} |",
        f"| | New OTPs it managed to get | {off['c_otps']} | {on['c_otps']} |",
        f"| Real users, 1 every {args.duration / args.users:.0f} s | Completed sign-up (OTP, verify, add, list) | {off['users_ok']} of {off['users']} ({pct(off['users_ok'], off['users'])}) | {on['users_ok']} of {on['users']} ({pct(on['users_ok'], on['users'])}) |",
        f"| | Real-user requests refused (429) | {off['user_429']} | {on['user_429']} |",
        f"| | Sign-up time, median / 95th percentile | {off['p50']:.0f} / {off['p95']:.0f} ms | {on['p50']:.0f} / {on['p95']:.0f} ms |",
        "",
        "![E4 chart](e4-chart.svg)",
        "",
        "Limits on: 100 calls per 5 minutes per client IP (token bucket in Redis), one OTP per mobile "
        "every 30 s and 5 per hour, 3 wrong guesses per OTP, each OTP usable once.",
        "",
        "Why the guess cap matters: each guess has a one-in-a-million chance on a 6-digit OTP, so about "
        "a million guesses are needed on average. Unlimited, at 10 per second, that is about 28 hours; "
        "for a 4-digit OTP, about 17 minutes. With the limits on, the guesser gets 3 guesses per OTP and "
        "at most 5 OTPs an hour, so 15 guesses an hour: about 7.6 years for 6 digits, 28 days for 4.",
        "",
        "Raw data: `requests-off.csv`, `requests-on.csv` (one row per request).",
    ]
    if off["user_fail_codes"] or on["user_fail_codes"]:
        lines += ["", f"Real-user failure codes: off {off['user_fail_codes']}, on {on['user_fail_codes']}."]
    (out / "results.md").write_text("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True)
    p.add_argument("--redis", required=True)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--users", type=int, default=30)
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--out")
    args = p.parse_args()
    out = pathlib.Path(args.out) if args.out else ROOT / "docs" / "evidence" / \
        f"12-e4-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)

    results = {}
    for mode, enabled in (("off", False), ("on", True)):
        print(f"E4 limits {mode}: {args.duration:.0f} s ...", flush=True)
        rows = run_mode(args, enabled)
        with open(out / f"requests-{mode}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["actor", "t", "step", "status", "code", "ms"])
            w.writeheader()
            w.writerows(rows)
        results[mode] = summarize(rows)
    bar_svg(results["off"], results["on"], out / "e4-chart.svg")
    write_results(out, args, results["off"], results["on"])
    print((out / "results.md").read_text())
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
