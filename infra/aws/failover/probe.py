#!/usr/bin/env python3
"""Hit a URL on a fixed schedule and log every answer to CSV.

    probe.py URL OUT.csv [--rate 5] [--duration 180] [--timeout 2]

Requests are sent on schedule even if earlier ones hang, so a dead server
shows up as failures at the right moments instead of stalling the log.
"""
import argparse
import csv
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


def one_request(url, timeout):
    started = time.monotonic()
    status, zone, instance, error = 0, "", "", ""
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            status = resp.status
            zone = resp.headers.get("X-Availability-Zone", "")
            instance = resp.headers.get("X-Instance-Id", "")
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        error = type(e).__name__
    return status, zone, instance, round((time.monotonic() - started) * 1000), error


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("out")
    p.add_argument("--rate", type=float, default=5)
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--timeout", type=float, default=2)
    args = p.parse_args()

    lock = threading.Lock()
    out = open(args.out, "w", newline="")
    writer = csv.writer(out)
    writer.writerow(["n", "t_s", "time_utc", "status", "zone", "instance", "ms", "error"])
    t0 = time.monotonic()

    def run(n, scheduled):
        sent_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        status, zone, instance, ms, error = one_request(args.url, args.timeout)
        with lock:
            writer.writerow([n, f"{scheduled:.3f}", sent_at, status, zone, instance, ms, error])

    total = int(args.duration * args.rate)
    with ThreadPoolExecutor(max_workers=int(args.rate * args.timeout) + 4) as pool:
        for n in range(total):
            scheduled = n / args.rate
            delay = t0 + scheduled - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pool.submit(run, n + 1, scheduled)
    out.close()


if __name__ == "__main__":
    main()
