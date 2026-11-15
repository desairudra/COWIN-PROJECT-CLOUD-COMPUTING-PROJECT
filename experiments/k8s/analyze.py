#!/usr/bin/env python3
"""results.md and timeline.svg for an E2 or E3 run.

    analyze.py RUN_DIR

RUN_DIR holds run.env, probe.csv (every request), pods.csv (pods every second),
events.csv and nodes.csv, written by run_e2.sh / run_e3.sh.
"""
import csv
import math
import pathlib
import sys
from collections import Counter, defaultdict

SLOW_MS = 1000
COLORS = ["#1f5fa8", "#0b8a6f", "#7a3fa0", "#d68a00", "#5b6b71", "#8a6d00"]


def load_env(path):
    env = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip("'\"")
    return env


def read_csv(path):
    if not pathlib.Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load(run):
    env = load_env(run / "run.env")
    probe = []
    for r in read_csv(run / "probe.csv"):
        status = int(r["status"] or 0)
        probe.append({"t": float(r["t_s"]), "ok": status == 200, "status": status, "ms": int(r["ms"] or 0),
                      "node": r.get("zone", ""), "pod": r.get("instance", ""), "release": r.get("release", ""),
                      "error": r.get("error", "")})
    probe.sort(key=lambda r: r["t"])
    pods = read_csv(run / "pods.csv")
    for p in pods:
        p["t"] = float(p["t_s"])
    events = [(float(e["t_s"]), e["event"]) for e in read_csv(run / "events.csv")]
    zones = {n["node"]: n["zone"] for n in read_csv(run / "nodes.csv")}
    return env, probe, pods, events, zones


def node_label(node, zones):
    z = zones.get(node, "")
    return f"{node} ({z})" if z and z != "-" else (node or "?")


def ready_series(pods, key, zones):
    """{second: Counter(group -> ready pods)} from pods.csv."""
    out = defaultdict(Counter)
    for p in pods:
        if p["ready"] == "True" and p["terminating"] == "0":
            g = p["release"] if key == "release" else node_label(p["node"], zones)
            out[int(p["t"])][g] += 1
    return out


def summarize(env, probe, pods, events, zones):
    s = {"total": len(probe), "ok": sum(r["ok"] for r in probe)}
    s["failed"] = s["total"] - s["ok"]
    s["slow"] = sum(r["ok"] and r["ms"] >= SLOW_MS for r in probe)
    s["availability"] = s["ok"] / s["total"] if s["total"] else 0.0
    ok_t = [r["t"] for r in probe if r["ok"]]
    gaps = [(b - a, a) for a, b in zip(ok_t, ok_t[1:])]
    s["max_gap"], s["gap_at"] = max(gaps, default=(0.0, 0.0))
    s["errors"] = Counter(r["error"] or f"HTTP {r['status']}" for r in probe if not r["ok"])
    fails = [r["t"] for r in probe if not r["ok"]]
    s["first_fail"], s["last_fail"] = (fails[0], fails[-1]) if fails else (None, None)

    ready = ready_series(pods, "node", zones)
    totals = {t: sum(c.values()) for t, c in ready.items()}
    s["recoveries"] = []
    for t, label in events:
        before = [n for sec, n in totals.items() if t - 5 <= sec < t]
        want = max(before, default=None)
        back = None
        if want:
            dipped = False
            for sec in sorted(x for x in totals if x >= t):
                if totals[sec] < want:
                    dipped = True
                elif dipped:
                    back = sec - t
                    break
        win = [r for r in probe if t <= r["t"] < t + 30]
        s["recoveries"].append({"t": t, "event": label, "pods_before": want, "back_after": back,
                                "failed_30s": sum(not r["ok"] for r in win), "n_30s": len(win)})

    if env.get("GROUP") == "release":
        v2 = [r["t"] for r in probe if r["ok"] and r["release"] == "v2"]
        v1_after = [r["t"] for r in probe if r["ok"] and r["release"] == "v1" and v2 and r["t"] >= v2[0]]
        start = next((t for t, e in events if "started" in e), None)
        done = next((t for t, e in events if "finished" in e), None)
        s["first_v2"] = (v2[0] - start) if v2 and start is not None else None
        s["last_v1"] = (v1_after[-1] - start) if v1_after and start is not None else None
        s["rollout_s"] = (done - start) if done is not None and start is not None else None
        s["mixed_s"] = (v1_after[-1] - v2[0]) if v1_after else 0.0
    else:
        by_node = Counter(node_label(r["node"], zones) for r in probe if r["ok"])
        s["by_group"] = by_node
    return s


def verdict(s):
    if s["failed"] == 0 and s["max_gap"] < 1:
        return "No downtime"
    if s["max_gap"] < 5:
        return "No outage, but some requests failed"
    return f"Outage of {s['max_gap']:.1f} s"


def fmt(v, unit=" s"):
    return "-" if v is None else f"{v:.1f}{unit}"


def write_results(run, env, s, events):
    lines = [f"# {env.get('TITLE', 'Experiment')}", "",
             f"Probe: {env.get('RATE')} requests a second for {env.get('DURATION')} s to `{env.get('URL')}`, "
             f"{env.get('PODS')} lookup pods to start with.", "",
             f"**{verdict(s)}.** {s['ok']} of {s['total']} requests answered "
             f"({s['availability']:.2%}); the longest stretch with no successful answer was "
             f"{s['max_gap']:.2f} s (at t = {s['gap_at']:.0f} s).", "",
             "| Measure | Value |", "| --- | --- |",
             f"| Requests | {s['total']} |",
             f"| Failed | {s['failed']} |",
             f"| Slower than {SLOW_MS} ms but answered | {s['slow']} |",
             f"| Longest time without a successful answer | {s['max_gap']:.2f} s |",
             f"| First / last failure | {fmt(s['first_fail'])} / {fmt(s['last_fail'])} |"]
    if "rollout_s" in s:
        lines += [f"| Rollout took (start to all new pods ready) | {fmt(s['rollout_s'])} |",
                  f"| First answer from v2 after the start | {fmt(s['first_v2'])} |",
                  f"| v1 and v2 both answering for | {fmt(s['mixed_s'])} |"]
    if s["errors"]:
        lines += [f"| Failure types | {', '.join(f'{k}: {v}' for k, v in s['errors'].most_common())} |"]
    lines.append("")
    if s["recoveries"]:
        lines += ["| Event | At | Ready pods before | Back to that many after | Failed in the next 30 s |",
                  "| --- | --- | --- | --- | --- |"]
        for r in s["recoveries"]:
            lines.append(f"| {r['event']} | {r['t']:.1f} s | {r['pods_before'] or '-'} | {fmt(r['back_after'])} | "
                         f"{r['failed_30s']} of {r['n_30s']} |")
        lines.append("")
    if s.get("by_group"):
        lines += ["Answers by node: " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_group"].items())), ""]
    lines += ["![timeline](timeline.svg)", "",
              "Raw data: `probe.csv` (every request), `pods.csv` (lookup pods every second), `events.csv`, `nodes.csv`.", ""]
    (run / "results.md").write_text("\n".join(lines))


def nice(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def write_svg(run, env, s, probe, pods, events, zones):
    group = env.get("GROUP", "node")
    end = max([r["t"] for r in probe] + [float(env.get("DURATION", 1))])
    secs = int(end) + 1
    W, L, R, T, PH, GAP = 960, 70, 160, 80, 120, 46
    H = T + 3 * PH + 2 * GAP + 50
    x = lambda t: L + (W - L - R) * t / secs
    bw = max((W - L - R) / secs - 0.6, 0.6)

    per = defaultdict(lambda: [0, 0, 0])
    answers = defaultdict(Counter)
    for r in probe:
        b = per[int(r["t"])]
        if not r["ok"]:
            b[2] += 1
        elif r["ms"] >= SLOW_MS:
            b[1] += 1
        else:
            b[0] += 1
        if r["ok"]:
            answers[int(r["t"])][r["release"] if group == "release" else node_label(r["node"], zones)] += 1
    ready = ready_series(pods, group, zones)
    groups = sorted({g for c in ready.values() for g in c} | {g for c in answers.values() for g in c})
    color = {g: COLORS[i % len(COLORS)] for i, g in enumerate(groups)}

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="26" font-size="15" font-weight="700" fill="#17242b">{env.get("TITLE", "")}</text>',
           f'<text x="{L}" y="44" fill="#5b6b71">{verdict(s)}: {s["ok"]} of {s["total"]} answered, '
           f'longest gap {s["max_gap"]:.2f} s.</text>']

    def stacked(i, title, data, keys, colors, ymax, legend):
        top = T + i * (PH + GAP)
        bottom = top + PH
        y = lambda v: bottom - PH * min(v, ymax) / ymax
        out.append(f'<text x="{L}" y="{top - 8}" font-weight="600" fill="#17242b">{title}</text>')
        for k in range(3):
            v = ymax * k / 2
            out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e3e8e6"/>')
            out.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#5b6b71">{v:g}</text>')
        for sec in range(secs):
            base = 0
            for k in keys:
                v = data[sec][k] if isinstance(data[sec], Counter) else data[sec][k]
                if v:
                    out.append(f'<rect x="{x(sec):.1f}" y="{y(base + v):.1f}" width="{bw:.1f}" '
                               f'height="{y(base) - y(base + v):.1f}" fill="{colors[k]}"/>')
                    base += v
        for j, (label, c) in enumerate(legend):
            out.append(f'<rect x="{W - R + 14}" y="{top + j * 18}" width="10" height="10" fill="{c}"/>')
            out.append(f'<text x="{W - R + 28}" y="{top + j * 18 + 9}" fill="#17242b">{label}</text>')
        for t, label in events:
            out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{bottom}" stroke="#c0362c" stroke-dasharray="3 3"/>')

    rate = float(env.get("RATE", 20))
    plain = {sec: Counter({0: v[0], 1: v[1], 2: v[2]}) for sec, v in per.items()}
    plain = defaultdict(Counter, plain)
    stacked(0, "Requests each second", plain, [0, 1, 2], {0: "#0b8a6f", 1: "#d68a00", 2: "#c0362c"},
            nice(rate * 1.2), [("answered", "#0b8a6f"), (f"answered, over {SLOW_MS} ms", "#d68a00"), ("failed", "#c0362c")])
    most = max([sum(c.values()) for c in ready.values()] + [1])
    stacked(1, "Ready lookup pods, by " + ("release" if group == "release" else "node"), ready, groups, color,
            most + 2 - most % 2, [(g, color[g]) for g in groups])
    stacked(2, "Who answered, by " + ("release" if group == "release" else "node"), answers, groups, color,
            nice(rate * 1.2), [(g, color[g]) for g in groups])

    top = T
    for j, (t, label) in enumerate(events):
        out.append(f'<text x="{x(t) + 3:.1f}" y="{top + 12 + 13 * (j % 3)}" fill="#c0362c">{label.replace("&", "and")}</text>')
    axis = T + 3 * PH + 2 * GAP
    step = nice(secs / 10)
    t = 0
    while t <= secs:
        out.append(f'<text x="{x(t):.1f}" y="{axis + 16}" text-anchor="middle" fill="#5b6b71">{t:g} s</text>')
        t += step
    out.append("</svg>")
    (run / "timeline.svg").write_text("\n".join(out))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    run = pathlib.Path(sys.argv[1])
    env, probe, pods, events, zones = load(run)
    if not probe:
        sys.exit("probe.csv is empty")
    s = summarize(env, probe, pods, events, zones)
    write_results(run, env, s, events)
    write_svg(run, env, s, probe, pods, events, zones)
    print(f"{verdict(s)}: {s['ok']} of {s['total']} answered, longest gap {s['max_gap']:.2f} s")
    print(f"Saved to {run}")


if __name__ == "__main__":
    main()
