#!/usr/bin/env python3
"""results.md and e1-chart.svg for an E1 (autoscaling) run.

    plot_e1.py RUN_DIR K6_RAW_CSV
"""
import csv
import importlib.util
import math
import pathlib
import sys
from collections import defaultdict

_spec = importlib.util.spec_from_file_location(
    "loadtest_analyze", pathlib.Path(__file__).resolve().parents[2] / "loadtest" / "analyze.py")
_lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lt)
nice, pct = _lt.nice, _lt.pct



def load_env(path):
    env = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip("'\"")
    return env


def secs(v):
    v = str(v).strip()
    return float(v[:-1]) if v.endswith("s") else float(v)


def stages(env):
    low_t, ramp, hold, down, cool = 30.0, 60.0, secs(env.get("HOLD", "180s")), 20.0, secs(env.get("COOL", "180s"))
    peak_at = low_t + ramp
    drop_at = peak_at + hold
    return {"ramp_at": low_t, "peak_at": peak_at, "drop_at": drop_at, "low_again": drop_at + down,
            "end": drop_at + down + cool}


def planned(t, env, st):
    low, peak = float(env.get("LOW", 20)), float(env.get("PEAK", 300))
    if t < st["ramp_at"]:
        return low
    if t < st["peak_at"]:
        return low + (peak - low) * (t - st["ramp_at"]) / (st["peak_at"] - st["ramp_at"])
    if t < st["drop_at"]:
        return peak
    if t < st["low_again"]:
        return peak - (peak - low) * (t - st["drop_at"]) / (st["low_again"] - st["drop_at"])
    return low


def read_k6(path, t0):
    per = defaultdict(lambda: {"ms": [], "reqs": 0, "failed": 0})
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            m = r["metric_name"]
            if r.get("scenario") != "hpa" or m not in ("http_req_duration", "http_reqs", "http_req_failed"):
                continue
            t = int(float(r["timestamp"]) - t0)
            b = per[t]
            v = float(r["metric_value"])
            if m == "http_req_duration":
                b["ms"].append(v)
            elif m == "http_reqs":
                b["reqs"] += 1
            elif v >= 1:
                b["failed"] += 1
    return per


def read_hpa(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"t": float(r["t_s"]), "current": int(r["current"] or 0), "desired": int(r["desired"] or 0),
                             "cpu": int(r["cpu_pct"]) if r["cpu_pct"] else None})
            except ValueError:
                continue
    return rows


def read_ready(path):
    ready = defaultdict(int)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["ready"] == "True" and r["terminating"] == "0":
                ready[int(float(r["t_s"]))] += 1
    return ready


def summarize(env, st, per, hpa, ready):
    floor = min((h["current"] for h in hpa if h["current"]), default=2)
    s = {"floor": floor}
    up = next((h for h in hpa if h["t"] >= st["ramp_at"] and h["desired"] > floor), None)
    s["first_up"] = up["t"] - st["ramp_at"] if up else None
    s["max_desired"] = max((h["desired"] for h in hpa), default=0)
    s["max_ready"] = max(ready.values(), default=0)
    top = next((t for t in sorted(ready) if ready[t] >= s["max_ready"]), None)
    s["max_ready_after"] = top - st["ramp_at"] if top is not None else None
    s["max_cpu"] = max((h["cpu"] for h in hpa if h["cpu"] is not None), default=None)
    down = next((h for h in hpa if h["t"] >= st["low_again"] and h["current"] <= floor), None)
    s["back_down"] = down["t"] - st["drop_at"] if down else None

    def window(a, b):
        ms = [v for t, p in per.items() if a <= t < b for v in p["ms"]]
        reqs = sum(p["reqs"] for t, p in per.items() if a <= t < b)
        failed = sum(p["failed"] for t, p in per.items() if a <= t < b)
        return {"p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99), "reqs": reqs, "failed": failed,
                "rate": reqs / (b - a) if b > a else 0}

    s["windows"] = [
        ("Before the spike", 0, st["ramp_at"]),
        ("Ramp up", st["ramp_at"], st["peak_at"]),
        ("First 30 s at peak", st["peak_at"], st["peak_at"] + 30),
        ("Last 60 s at peak", st["drop_at"] - 60, st["drop_at"]),
        ("After the spike", st["low_again"], st["end"]),
    ]
    s["windows"] = [(name, a, b, window(a, b)) for name, a, b in s["windows"] if b > a]
    s["reqs"] = sum(p["reqs"] for p in per.values())
    s["failed"] = sum(p["failed"] for p in per.values())
    return s


def fmt(v, unit=" s"):
    return "-" if v is None else f"{v:.0f}{unit}"


def write_results(run, env, s):
    peak = env.get("PEAK")
    head = (f"**The autoscaler went from {s['floor']} to {s['max_desired']} pods** "
            f"{'and back to ' + str(s['floor']) + ' ' + fmt(s['back_down']) + ' after the load dropped' if s['back_down'] is not None else '(did not get back to the minimum before the run ended)'}. "
            f"First scale-up {fmt(s['first_up'])} after the load started rising; "
            f"{s['failed']} of {s['reqs']} requests failed.")
    w = {name: v for name, _, _, v in s["windows"]}
    lines = [f"# {env.get('TITLE', 'E1')}", "",
             f"Load from k6 through nginx (`{env.get('URL')}`): {env.get('LOW')} requests/s, ramp to {peak} in 60 s, "
             f"hold {env.get('HOLD')}, back to {env.get('LOW')} and wait {env.get('COOL')}. Autoscaler: {env.get('HPA')}.", "",
             head, "",
             "| Measure | Value |", "| --- | --- |",
             f"| First scale-up after the load started rising | {fmt(s['first_up'])} |",
             f"| Most pods asked for | {s['max_desired']} |",
             f"| Most pods ready (reached after) | {s['max_ready']} ({fmt(s['max_ready_after'])}) |",
             f"| Highest average CPU seen by the autoscaler | {fmt(s['max_cpu'], '%')} of the request |",
             f"| Back to {s['floor']} pods after the load dropped | {fmt(s['back_down'])} |",
             f"| Requests / failed | {s['reqs']} / {s['failed']} |", "",
             "| Phase | Requests/s | p50 ms | p95 ms | p99 ms | Failed |", "| --- | --- | --- | --- | --- | --- |"]
    for name, _, _, v in s["windows"]:
        lines.append(f"| {name} | {v['rate']:.0f} | {v['p50']:.0f} | {v['p95']:.0f} | {v['p99']:.0f} | {v['failed']} |")
    if "First 30 s at peak" in w and "Last 60 s at peak" in w:
        a, b = w["First 30 s at peak"]["p95"], w["Last 60 s at peak"]["p95"]
        if b < 0.8 * a:
            note = "the same load, answered faster once the extra pods were serving"
        elif b > 1.25 * a:
            note = "slower at the end: the extra pods were not enough (raise maxReplicas or the CPU limit)"
        else:
            note = "about the same: the pods kept up from the start"
        lines += ["", f"p95 was {a:.0f} ms in the first 30 s at peak and {b:.0f} ms in the last 60 s: {note}."]
    lines += ["", "![E1 chart](e1-chart.svg)", "",
              "Raw data: `hpa.csv` (autoscaler every 5 s), `pods.csv` (pods every second), `per_second.csv`, `summary.json`.", ""]
    (run / "results.md").write_text("\n".join(lines))


def write_svg(run, env, st, s, per, hpa, ready, sec_rows):
    end = int(st["end"]) + 5
    W, L, R, T, PH, GAP = 960, 70, 30, 80, 140, 52
    H = T + 4 * PH + 3 * GAP + 50
    x = lambda t: L + (W - L - R) * t / end
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="26" font-size="15" font-weight="700" fill="#17242b">{env.get("TITLE", "E1")}</text>',
           f'<text x="{L}" y="44" fill="#5b6b71">{s["floor"]} to {s["max_desired"]} pods; first scale-up '
           f'{fmt(s["first_up"])} after the ramp began; {s["failed"]} of {s["reqs"]} requests failed.</text>']

    def panel(i, title, ymax, lines, log=False, ref=None, bars=None):
        top = T + i * (PH + GAP)
        bottom = top + PH
        if log:
            y = lambda v: bottom - PH * math.log10(min(max(v, 1), ymax)) / math.log10(ymax)
            ticks = [10 ** k for k in range(int(math.log10(ymax)) + 1)]
        else:
            y = lambda v: bottom - PH * min(v, ymax) / ymax
            ticks = [ymax * k / 4 for k in range(5)]
        out.append(f'<text x="{L}" y="{top - 8}" font-weight="600" fill="#17242b">{title}</text>')
        for v in ticks:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e3e8e6"/>')
            out.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#5b6b71">{v:g}</text>')
        for t in (st["ramp_at"], st["peak_at"], st["drop_at"], st["low_again"]):
            out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{bottom}" stroke="#c9d3d0" stroke-dasharray="2 3"/>')
        if bars:
            label, pts, c = bars
            bw = max((W - L - R) / end - 0.5, 0.5)
            for t, v in pts:
                if v:
                    out.append(f'<rect x="{x(t):.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{bottom - y(v):.1f}" fill="{c}" opacity="0.35"/>')
        if ref:
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(ref[0]):.1f}" y2="{y(ref[0]):.1f}" stroke="#8a6d00" stroke-dasharray="6 4"/>')
            out.append(f'<text x="{W - R}" y="{y(ref[0]) - 4:.1f}" text-anchor="end" fill="#8a6d00">{ref[1]}</text>')
        lx = L + 7.5 * len(title) + 30
        legend = list(lines) + ([(bars[0], None, bars[2], "bar")] if bars else [])
        for label, pts, c, style in legend:
            if pts:
                d = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts if v is not None)
                if style == "step":
                    d = []
                    prev = None
                    for t, v in pts:
                        if prev is not None:
                            d.append(f"{x(t):.1f},{y(prev):.1f}")
                        d.append(f"{x(t):.1f},{y(v):.1f}")
                        prev = v
                    d = " ".join(d)
                dash = ' stroke-dasharray="5 4"' if style == "dash" else ""
                out.append(f'<polyline points="{d}" fill="none" stroke="{c}" stroke-width="1.8"{dash}/>')
            out.append(f'<rect x="{lx:.0f}" y="{top - 17}" width="10" height="10" fill="{c}"/>')
            out.append(f'<text x="{lx + 14:.0f}" y="{top - 8}" fill="#17242b">{label}</text>')
            lx += 26 + 6.5 * len(label)

    peak = float(env.get("PEAK", 300))
    plan = [(t, planned(t, env, st)) for t in range(0, int(st["end"]) + 1)]
    done = [(r["t"], r["requests"]) for r in sec_rows]
    panel(0, "Load (requests/s)", nice(peak * 1.15), [("planned", plan, "#7b8c91", "dash"), ("done", done, "#17242b", "line")])

    hmax = max([h["desired"] for h in hpa] + list(ready.values()) + [2])
    panel(1, "Pods", 4 * math.ceil((hmax + 1) / 4),
          [("asked for by the autoscaler", [(h["t"], h["desired"]) for h in hpa], "#7a3fa0", "step")],
          bars=("ready", sorted(ready.items()), "#1f5fa8"))

    cpu = [(h["t"], h["cpu"]) for h in hpa if h["cpu"] is not None]
    cmax = nice(max([c for _, c in cpu] + [100]) * 1.1)
    panel(2, "Average CPU, % of the request", cmax, [("CPU", cpu, "#0b8a6f", "line")], ref=(50, "target 50%"))

    worst = max([r["p99_ms"] for r in sec_rows] + [10])
    panel(3, "Latency (ms, log scale)", 10 ** math.ceil(math.log10(worst)),
          [("p50", [(r["t"], r["p50_ms"]) for r in sec_rows if r["requests"]], "#0b8a6f", "line"),
           ("p95", [(r["t"], r["p95_ms"]) for r in sec_rows if r["requests"]], "#1f5fa8", "line"),
           ("p99", [(r["t"], r["p99_ms"]) for r in sec_rows if r["requests"]], "#7a3fa0", "line")], log=True)

    axis = T + 4 * PH + 3 * GAP
    step = nice(end / 10)
    t = 0
    while t <= end:
        out.append(f'<text x="{x(t):.1f}" y="{axis + 16}" text-anchor="middle" fill="#5b6b71">{t:g} s</text>')
        t += step
    out.append("</svg>")
    (run / "e1-chart.svg").write_text("\n".join(out))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    run, raw = pathlib.Path(sys.argv[1]), sys.argv[2]
    env = load_env(run / "run.env")
    st = stages(env)
    per = read_k6(raw, float(env["K6_START"]))
    hpa = read_hpa(run / "hpa.csv")
    ready = read_ready(run / "pods.csv")
    sec_rows = []
    for t in range(0, max(per, default=0) + 1):
        p = per.get(t, {"ms": [], "reqs": 0, "failed": 0})
        sec_rows.append({"t": t, "requests": p["reqs"], "failed": p["failed"], "ready": ready.get(t, 0),
                         "p50_ms": round(pct(p["ms"], 50), 1), "p95_ms": round(pct(p["ms"], 95), 1),
                         "p99_ms": round(pct(p["ms"], 99), 1)})
    with open(run / "per_second.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sec_rows[0]) if sec_rows else ["t"])
        w.writeheader()
        w.writerows(sec_rows)
    s = summarize(env, st, per, hpa, ready)
    write_results(run, env, s)
    write_svg(run, env, st, s, per, hpa, ready, sec_rows)
    print(f"{s['floor']} -> {s['max_desired']} pods, first scale-up {fmt(s['first_up'])} after the ramp began, "
          f"back down {fmt(s['back_down'])} after the drop; {s['failed']} of {s['reqs']} failed")
    print(f"Saved to {run}")


if __name__ == "__main__":
    main()
