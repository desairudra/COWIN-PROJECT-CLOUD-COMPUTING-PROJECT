#!/usr/bin/env python3
"""Turn a k6 run into results.md, per_second.csv and load-chart.svg.

    analyze.py --raw raw.csv --out DIR [--summary summary.json] [--unit requests/s]
               [--step 60] [--p95-limit 500] [--title TEXT] [--check]

raw.csv is k6's --out csv file. Load steps are the scenarios named rate_<N>.
A step passes when at least 95% of the planned load was sent, p95 latency is
under the limit and under 1% of requests failed.
"""
import argparse
import csv
import json
import math
import pathlib
import re
import sys
from collections import Counter, defaultdict

MIN_SENT = 0.95
MAX_ERRORS = 0.01
COUNTERS = ("iterations", "dropped_iterations", "zone_a", "zone_b", "signups_ok", "signups_failed")


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def new_bucket():
    return {"ms": [], "reqs": 0, "failed": 0, "status": Counter(), **{c: 0 for c in COUNTERS}}


def read_raw(path):
    steps = defaultdict(new_bucket)
    secs = defaultdict(new_bucket)
    sec_step = defaultdict(Counter)
    endpoints = defaultdict(lambda: defaultdict(lambda: {"ms": [], "failed": 0}))
    span = defaultdict(lambda: [math.inf, -math.inf])
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            m = r["metric_name"]
            scen = r["scenario"]
            if not scen.startswith("rate_"):
                continue
            t = int(float(r["timestamp"]))
            v = float(r["metric_value"])
            st, sc = steps[scen], secs[t]
            if m == "http_req_duration":
                st["ms"].append(v)
                sc["ms"].append(v)
                endpoints[scen][r["name"]]["ms"].append(v)
            elif m == "http_reqs":
                st["reqs"] += 1
                sc["reqs"] += 1
                code = r["status"] or "0"
                st["status"][code] += 1
            elif m == "http_req_failed" and v >= 1:
                st["failed"] += 1
                sc["failed"] += 1
                endpoints[scen][r["name"]]["failed"] += 1
            elif m in COUNTERS:
                st[m] += v
                sc[m] += v
                if m == "iterations":
                    sec_step[t][scen] += 1
                    span[scen][0] = min(span[scen][0], t)
                    span[scen][1] = max(span[scen][1], t)
    return steps, secs, sec_step, endpoints, span


def rate_of(name):
    return int(re.sub(r"\D", "", name) or 0)


def summarize(steps, span, p95_limit):
    rows = []
    for name in sorted(steps, key=rate_of):
        s = steps[name]
        target = rate_of(name)
        lo, hi = span[name]
        seconds = (hi - lo + 1) if hi >= lo else 0
        planned = s["iterations"] + s["dropped_iterations"]
        sent = s["iterations"] / planned if planned else 0.0
        errors = s["failed"] / s["reqs"] if s["reqs"] else 1.0
        p95 = pct(s["ms"], 95)
        why = []
        if sent < MIN_SENT:
            why.append(f"only {sent:.0%} of the load could be sent")
        if p95 > p95_limit:
            why.append(f"p95 {p95:.0f} ms over {p95_limit:.0f} ms")
        if errors > MAX_ERRORS:
            why.append(f"{errors:.1%} of requests failed")
        rows.append({
            "step": name, "target": target, "seconds": seconds,
            "iterations": int(s["iterations"]), "dropped": int(s["dropped_iterations"]),
            "sent": sent, "reqs": s["reqs"], "failed": s["failed"], "errors": errors,
            "p50": pct(s["ms"], 50), "p95": p95, "p99": pct(s["ms"], 99),
            "max": max(s["ms"]) if s["ms"] else 0.0,
            "zone_a": int(s["zone_a"]), "zone_b": int(s["zone_b"]),
            "signups_ok": int(s["signups_ok"]), "signups_failed": int(s["signups_failed"]),
            "status": dict(s["status"]), "ok": not why, "why": why,
        })
    return rows


def capacity(rows):
    best = None
    for r in rows:
        if not r["ok"]:
            return best, r
        best = r
    return best, None


def per_second(secs, sec_step, rows, step_s=None):
    targets = {r["step"]: r["target"] for r in rows}
    if not secs:
        return []
    t0 = min(secs)
    out = []
    for t in range(t0, max(secs) + 1):
        b = secs.get(t, new_bucket())
        if step_s:
            i = (t - t0) // step_s
            step = rows[i]["step"] if i < len(rows) else ""
        else:
            step = sec_step[t].most_common(1)[0][0] if sec_step[t] else ""
        out.append({
            "t": t - t0, "step": step, "target": targets.get(step, 0),
            "achieved": int(b["iterations"]), "requests": b["reqs"], "failed": b["failed"],
            "dropped": int(b["dropped_iterations"]),
            "p50_ms": round(pct(b["ms"], 50), 1), "p95_ms": round(pct(b["ms"], 95), 1),
            "p99_ms": round(pct(b["ms"], 99), 1),
            "zone_a": int(b["zone_a"]), "zone_b": int(b["zone_b"]),
        })
    return out


def headline(rows, unit, p95_limit):
    best, broke = capacity(rows)
    if not rows:
        return "No load steps found in the k6 output."
    if broke is None:
        top = rows[-1]
        return (f"**Every step passed.** At the highest step, {top['target']} {unit}, p95 latency was "
                f"{top['p95']:.0f} ms and {top['errors']:.2%} of requests failed. The limit is above "
                f"what this run tried: add a higher step to RATES to find it.")
    if best is None:
        return (f"**Even the first step, {broke['target']} {unit}, did not pass:** "
                f"{'; '.join(broke['why'])}.")
    return (f"**Handled {best['target']} {unit}** with p95 latency {best['p95']:.0f} ms and "
            f"{best['errors']:.2%} errors. **At {broke['target']} {unit} it broke:** "
            f"{'; '.join(broke['why'])}.")


def write_results(rows, endpoints, meta, out):
    unit, limit = meta["unit"], meta["p95_limit"]
    zones = any(r["zone_a"] or r["zone_b"] for r in rows)
    signups = any(r["signups_ok"] or r["signups_failed"] for r in rows)
    lines = [f"# {meta['title']}", ""]
    if meta.get("note"):
        lines += [meta["note"], ""]
    lines += [headline(rows, unit, limit), ""]
    head = ["Target", "Load sent", "p50 ms", "p95 ms", "p99 ms", "Max ms", "Failed"]
    if signups:
        head.append("Sign-ups done")
    if zones:
        head.append("Zone A / B")
    head.append("Step")
    lines += ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    for r in rows:
        planned = r["iterations"] + r["dropped"]
        cells = [f"{r['target']} {unit}", f"{r['iterations']} of {planned} ({r['sent']:.0%})",
                 f"{r['p50']:.0f}", f"{r['p95']:.0f}", f"{r['p99']:.0f}", f"{r['max']:.0f}",
                 f"{r['failed']} of {r['reqs']} ({r['errors']:.2%})"]
        if signups:
            cells.append(f"{r['signups_ok']} of {r['iterations']}")
        if zones:
            total = r["zone_a"] + r["zone_b"]
            cells.append(f"{r['zone_a'] / total:.0%} / {r['zone_b'] / total:.0%}" if total else "-")
        cells.append("pass" if r["ok"] else "**fail**: " + "; ".join(r["why"]))
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", f"A step passes when at least {MIN_SENT:.0%} of the planned load was sent, p95 is under "
              f"{limit:.0f} ms and under {MAX_ERRORS:.0%} of requests failed. \"Load sent\" below 100% means answers were so slow "
              f"that k6 ran out of virtual users and had to skip some of the planned load.", ""]

    codes = sorted({c for r in rows for c in r["status"]})
    if any(c != "200" and c != "201" for c in codes):
        lines += ["Response codes per step (0 = no answer: timeout or connection error):", "",
                  "| Target | " + " | ".join(codes) + " |", "| --- |" + " --- |" * len(codes)]
        for r in rows:
            lines.append(f"| {r['target']} {unit} | " + " | ".join(str(r["status"].get(c, 0)) for c in codes) + " |")
        lines.append("")

    best, broke = capacity(rows)
    shown = best or (rows[0] if rows else None)
    eps = endpoints.get(shown["step"], {}) if shown else {}
    if len(eps) > 1:
        where = "highest step that passed" if best else "first step"
        lines += [f"Per endpoint at {shown['target']} {unit} ({where}):", "",
                  "| Endpoint | Requests | p50 ms | p95 ms | p99 ms | Failed |", "| --- | --- | --- | --- | --- | --- |"]
        for name in sorted(eps):
            e = eps[name]
            lines.append(f"| `{name}` | {len(e['ms'])} | {pct(e['ms'], 50):.0f} | {pct(e['ms'], 95):.0f} | "
                         f"{pct(e['ms'], 99):.0f} | {e['failed']} |")
        lines.append("")

    lines += ["![load chart](load-chart.svg)", ""]
    if meta.get("target_url"):
        lines.append(f"Target: `{meta['target_url']}`")
    lines += ["Data: `per_second.csv` (one row per second), `summary.json` (k6 end-of-test summary).", ""]
    (out / "results.md").write_text("\n".join(lines))


def write_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def nice(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def write_svg(sec, rows, meta, path):
    W, L, R = 960, 70, 30
    PH, GAP, T = 170, 50, 80
    H = T + 3 * PH + 2 * GAP + 50
    n = max(len(sec), 1)
    x = lambda t: L + (W - L - R) * t / max(n - 1, 1)
    zones = any(s["zone_a"] or s["zone_b"] for s in sec)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="26" font-size="15" font-weight="700" fill="#17242b">{meta["title"]}</text>',
           f'<text x="{L}" y="44" fill="#5b6b71">{headline(rows, meta["unit"], meta["p95_limit"]).replace("**", "")[:150]}</text>']

    starts = []
    prev = None
    for s in sec:
        if s["step"] and s["step"] != prev:
            starts.append((s["t"], s["target"]))
            prev = s["step"]

    def panel(i, title, series, ymax, ref=None, log=False):
        top = T + i * (PH + GAP)
        bottom = top + PH
        if log:
            lo = 1.0
            y = lambda v: bottom - PH * (math.log10(min(max(v, lo), ymax)) / math.log10(ymax))
            ticks = [10 ** k for k in range(int(math.log10(ymax)) + 1)]
        else:
            y = lambda v: bottom - PH * min(v, ymax) / ymax
            ticks = [ymax * k / 4 for k in range(5)]
        out.append(f'<text x="{L}" y="{top - 8}" font-weight="600" fill="#17242b">{title}</text>')
        for v in ticks:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e3e8e6"/>')
            out.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#5b6b71">{v:g}</text>')
        for t, target in starts:
            out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{bottom}" stroke="#c9d3d0" stroke-dasharray="2 3"/>')
            if i == 0:
                out.append(f'<text x="{x(t) + 4:.1f}" y="{top + 12}" fill="#5b6b71">{target}/s</text>')
        if ref is not None:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(ref[0]):.1f}" y2="{y(ref[0]):.1f}" stroke="#8a6d00" stroke-dasharray="6 4"/>')
            out.append(f'<text x="{W - R}" y="{y(ref[0]) - 4:.1f}" text-anchor="end" fill="#8a6d00">{ref[1]}</text>')
        lx = L + 7.5 * len(title) + 30
        base = {s["t"]: 0 for s in sec}
        bw = max((W - L - R) / n - 1, 1)
        for label, key, color, style in series:
            if style == "bar":
                for s in sec:
                    if s[key]:
                        y0, y1 = y(base[s["t"]]), y(base[s["t"]] + s[key])
                        out.append(f'<rect x="{x(s["t"]) - bw / 2:.1f}" y="{y1:.1f}" width="{bw:.1f}" '
                                   f'height="{y0 - y1:.1f}" fill="{color}"/>')
                        base[s["t"]] += s[key]
            else:
                pts = " ".join(f"{x(s['t']):.1f},{y(s[key]):.1f}" for s in sec)
                extra = {"dash": ' stroke-dasharray="5 4" stroke-width="1.6"', "wide": ' stroke-width="4" opacity="0.55"'}.get(style, ' stroke-width="1.6"')
                out.append(f'<polyline points="{pts}" fill="none" stroke="{color}"{extra}/>')
            out.append(f'<rect x="{lx:.0f}" y="{top - 17}" width="10" height="10" fill="{color}"/>')
            out.append(f'<text x="{lx + 14:.0f}" y="{top - 8}" fill="#17242b">{label}</text>')
            lx += 26 + 6.5 * len(label)

    tmax = nice(max([s["target"] for s in sec] + [s["achieved"] for s in sec] + [1]) * 1.1)
    series = [("planned", "target", "#7b8c91", "dash"), ("done", "achieved", "#17242b", "line")]
    if zones:
        series += [("answered by zone A", "zone_a", "#1f5fa8", "wide"), ("answered by zone B", "zone_b", "#0b8a6f", "line")]
    panel(0, f"Load: {meta['unit']}", series, tmax)

    worst = max([s["p99_ms"] for s in sec] + [meta["p95_limit"] * 2])
    lmax = 10 ** math.ceil(math.log10(worst))
    panel(1, "Latency per second (ms, log scale)", [("p50", "p50_ms", "#0b8a6f", "line"), ("p95", "p95_ms", "#1f5fa8", "line"),
                                                    ("p99", "p99_ms", "#7a3fa0", "line")],
          lmax, ref=(meta["p95_limit"], f"p95 limit {meta['p95_limit']:.0f} ms"), log=True)

    emax = nice(max([s["failed"] + s["dropped"] for s in sec] + [1]) * 1.1)
    panel(2, "Problems per second", [("failed requests", "failed", "#c0362c", "bar"),
                                     ("not sent (dropped)", "dropped", "#d68a00", "bar")], emax)

    axis_y = T + 3 * PH + 2 * GAP
    step = nice(n / 10)
    t = 0
    while t < n:
        out.append(f'<text x="{x(t):.1f}" y="{axis_y + 16}" text-anchor="middle" fill="#5b6b71">{t:g} s</text>')
        t += step
    out.append(f'<text x="{(L + W - R) / 2}" y="{axis_y + 36}" text-anchor="middle" fill="#5b6b71">seconds from start of test</text>')
    out.append("</svg>")
    path.write_text("\n".join(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--unit", default="requests/s")
    ap.add_argument("--step", type=int, help="seconds per step (default: guessed from the samples)")
    ap.add_argument("--p95-limit", type=float, default=500)
    ap.add_argument("--title", default="Load test")
    ap.add_argument("--note", default="")
    ap.add_argument("--target-url", default="")
    ap.add_argument("--check", action="store_true", help="exit 1 unless the first step passed")
    a = ap.parse_args(argv)

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    steps, secs, sec_step, endpoints, span = read_raw(a.raw)
    rows = summarize(steps, span, a.p95_limit)
    sec = per_second(secs, sec_step, rows, a.step)
    meta = {"title": a.title, "unit": a.unit, "p95_limit": a.p95_limit, "note": a.note, "target_url": a.target_url}
    write_results(rows, endpoints, meta, out)
    write_csv(sec, out / "per_second.csv")
    write_svg(sec, rows, meta, out / "load-chart.svg")
    (out / "steps.json").write_text(json.dumps(rows, indent=1))
    if a.summary and pathlib.Path(a.summary).resolve() != (out / "summary.json").resolve():
        (out / "summary.json").write_text(pathlib.Path(a.summary).read_text())

    print(headline(rows, a.unit, a.p95_limit).replace("**", ""))
    print(f"Saved to {out}")
    if a.check and (not rows or not rows[0]["ok"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
