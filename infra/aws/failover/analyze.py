#!/usr/bin/env python3
"""Turn a failover run into results.md and timeline.svg.

    analyze.py RUN_DIR

RUN_DIR holds probe.csv, health.csv and run.env (written by 08_failover_test.sh).
"""
import csv
import pathlib
import sys
from collections import Counter


def load_env(path):
    env = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def load_probe(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            r["t_s"] = float(r["t_s"])
            r["status"] = int(r["status"])
            r["ms"] = int(r["ms"])
            r["ok"] = r["status"] == 200
            rows.append(r)
    return sorted(rows, key=lambda r: r["t_s"])


def load_health(path):
    if not pathlib.Path(path).exists():
        return []
    with open(path) as f:
        return [dict(r, t_s=float(r["t_s"])) for r in csv.DictReader(f)]


def transitions(health, instance):
    out, last = [], None
    for h in health:
        if h["instance"] != instance:
            continue
        if h["state"] != last:
            out.append((h["t_s"], h["state"]))
            last = h["state"]
    return out


def summarize(rows, health, env):
    kill_t = float(env["KILL_AT_S"])
    zone_a, zone_b = env["AZ_A"], env["AZ_B"]
    before = [r for r in rows if r["t_s"] < kill_t]
    after = [r for r in rows if r["t_s"] >= kill_t]
    failed_after = [r for r in after if not r["ok"]]
    a_after = [r for r in after if r["ok"] and r["zone"] == zone_a]
    restore_t = float(env.get("RESTORE_AT_S") or 0) or None
    window = [r for r in after if restore_t is None or r["t_s"] < restore_t]

    s = {
        "mode": env["MODE"],
        "url": env["URL"],
        "kill_t": kill_t,
        "total": len(rows),
        "ok_total": sum(r["ok"] for r in rows),
        "before_n": len(before),
        "before_a": sum(r["ok"] and r["zone"] == zone_a for r in before),
        "before_b": sum(r["ok"] and r["zone"] == zone_b for r in before),
        "before_failed": sum(not r["ok"] for r in before),
        "after_n": len(window),
        "after_failed": sum(not r["ok"] for r in window),
        "after_b": sum(r["ok"] and r["zone"] == zone_b for r in window),
        "first_fail": min((r["t_s"] for r in failed_after), default=None),
        "last_fail": max((r["t_s"] for r in failed_after if restore_t is None or r["t_s"] < restore_t), default=None),
        "last_a": max((r["t_s"] for r in a_after if restore_t is None or r["t_s"] < restore_t), default=None),
        "fail_codes": Counter(str(r["status"] or r["error"] or "error") for r in failed_after),
        "max_ms_after": max((r["ms"] for r in window if r["ok"]), default=0),
        "a_states": transitions(health, env["INSTANCE_A"]),
        "restore_t": restore_t,
    }
    ok_times = [r["t_s"] for r in rows if r["ok"]]
    gaps = [b - a for a, b in zip(ok_times, ok_times[1:]) if b >= kill_t]
    s["max_gap"] = max(gaps, default=0.0)
    s["availability"] = s["ok_total"] / s["total"] if s["total"] else 0
    s["window_availability"] = 1 - s["after_failed"] / s["after_n"] if s["after_n"] else 0
    s["all_on_b_after"] = (s["last_a"] - kill_t) if s["last_a"] is not None else 0.0
    s["outage_span"] = (s["last_fail"] - s["first_fail"]) if s["first_fail"] is not None else 0.0
    unhealthy = [t for t, st in s["a_states"] if t >= kill_t and st in ("unhealthy", "draining", "unused")]
    s["lb_noticed_after"] = (unhealthy[0] - kill_t) if unhealthy else None
    return s


def write_markdown(s, env, path):
    def secs(v):
        return "n/a" if v is None else f"{v:.1f} s"

    pct = lambda n, d: f"{n / d:.1%}" if d else "n/a"
    codes = ", ".join(f"{k} x{v}" for k, v in s["fail_codes"].most_common()) or "none"
    stayed_up = s["after_b"] > 0 and s["max_gap"] < 5
    verdict = ("PASS: the site stayed up" if stayed_up
               else "FAIL: the site was unreachable for longer than 5 s")
    lines = [
        f"# Failover test ({s['mode']} stop of Zone A)",
        "",
        f"Run `{env['RUN_ID']}` against `{s['url']}`: {s['total']} requests at {env['RATE']} per second "
        f"over {env['DURATION']} s. Zone A's server (`{env['INSTANCE_A']}`, {env['AZ_A']}) was stopped "
        f"at t = {s['kill_t']:.0f} s.",
        "",
        f"**{verdict}.** After the stop, {s['after_n'] - s['after_failed']} of {s['after_n']} requests "
        f"succeeded ({pct(s['after_n'] - s['after_failed'], s['after_n'])}). The longest stretch with no "
        f"successful answer was {s['max_gap']:.1f} s, so a user retrying was never locked out for longer than that.",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Requests before the stop: {env['AZ_A']} / {env['AZ_B']} / failed | {s['before_a']} / {s['before_b']} / {s['before_failed']} |",
        f"| Requests after the stop | {s['after_n']} |",
        f"| Failed after the stop | {s['after_failed']} ({pct(s['after_failed'], s['after_n'])}) |",
        f"| Longest stretch with no successful answer | {s['max_gap']:.1f} s |",
        f"| Failure codes | {codes} |",
        f"| First failure after the stop | {secs(None if s['first_fail'] is None else s['first_fail'] - s['kill_t'])} |",
        f"| Last failure after the stop | {secs(None if s['last_fail'] is None else s['last_fail'] - s['kill_t'])} |",
        f"| Last answer from {env['AZ_A']} after the stop | {secs(s['all_on_b_after'] if s['last_a'] is not None else None)} |",
        f"| Load balancer marked Zone A unhealthy after | {secs(s['lb_noticed_after'])} |",
        f"| Slowest successful answer after the stop | {s['max_ms_after']} ms |",
        f"| Availability over the whole run | {pct(s['ok_total'], s['total'])} |",
        "",
        "![timeline](timeline.svg)",
        "",
        "Zone A target states seen by the load balancer (t in seconds from the start):",
        "",
    ]
    lines += [f"- t = {t:.1f} s: {st}" for t, st in s["a_states"]] or ["- (no health data)"]
    if s["restore_t"]:
        lines += ["", f"Zone A was started again at t = {s['restore_t']:.0f} s."]
    lines += ["", "Raw data: `probe.csv` (every request), `health.csv` (target health every 2 s), `run.env`."]
    pathlib.Path(path).write_text("\n".join(lines) + "\n")


def write_svg(rows, s, env, path):
    W, H, L, R, T = 960, 300, 112, 24, 56
    lanes = {env["AZ_A"]: T + 30, env["AZ_B"]: T + 80, "failed": T + 130}
    span = max(r["t_s"] for r in rows) + 1
    x = lambda t: L + (W - L - R) * t / span
    colors = {env["AZ_A"]: "#1f5fa8", env["AZ_B"]: "#0b8a6f", "failed": "#c0362c"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="24" font-size="15" font-weight="700" fill="#17242b">Zone A {s["mode"]} stop: '
           f'{s["after_failed"]} failed of {s["after_n"]} requests after the stop</text>',
           f'<text x="{L}" y="42" fill="#5b6b71">Each tick is one request ({env["RATE"]} per second). Red = failed or timed out.</text>']
    for name, y in lanes.items():
        out.append(f'<text x="{L - 8}" y="{y + 4}" text-anchor="end" fill="#17242b">{name}</text>')
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y}" y2="{y}" stroke="#e3e8e6"/>')
    for r in rows:
        lane = r["zone"] if r["ok"] and r["zone"] in lanes else "failed"
        out.append(f'<line x1="{x(r["t_s"]):.1f}" x2="{x(r["t_s"]):.1f}" y1="{lanes[lane] - 12}" y2="{lanes[lane] + 12}" '
                   f'stroke="{colors[lane]}" stroke-width="1.4"/>')
    stop_t = float(env.get("STOP_AT_S") or s["kill_t"])
    marks = ([(s["kill_t"], "Zone A removed from load balancer", "#8a6d00"), (stop_t, "Zone A stopped", "#c0362c")]
             if s["mode"] == "graceful" else [(s["kill_t"], "Zone A stopped", "#c0362c")])
    for i, (t, label, color) in enumerate(marks + (
            [(s["restore_t"], "Zone A started again", "#5b6b71")] if s["restore_t"] else [])):
        out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{T + 8}" y2="{T + 150}" stroke="{color}" stroke-dasharray="4 3"/>')
        out.append(f'<text x="{x(t) + 4:.1f}" y="{T + 14 + 14 * i}" fill="{color}" font-weight="600">{label}</text>')
    if s["lb_noticed_after"] is not None and s["mode"] == "hard":
        t = s["kill_t"] + s["lb_noticed_after"]
        out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{T + 20}" y2="{T + 150}" stroke="#8a6d00" stroke-dasharray="2 3"/>')
        out.append(f'<text x="{x(t) + 4:.1f}" y="{T + 162}" fill="#8a6d00">load balancer marks A unhealthy (+{s["lb_noticed_after"]:.1f} s)</text>')
    axis_y = T + 190
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{axis_y}" y2="{axis_y}" stroke="#7b8c91"/>')
    step = 30 if span > 120 else 10
    for t in range(0, int(span) + 1, step):
        out.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{axis_y}" y2="{axis_y + 5}" stroke="#7b8c91"/>')
        out.append(f'<text x="{x(t):.1f}" y="{axis_y + 19}" text-anchor="middle" fill="#5b6b71">{t} s</text>')
    out.append(f'<text x="{(L + W - R) / 2}" y="{axis_y + 38}" text-anchor="middle" fill="#5b6b71">seconds from start of test</text>')
    out.append("</svg>")
    pathlib.Path(path).write_text("\n".join(out))


def main():
    run = pathlib.Path(sys.argv[1])
    env = load_env(run / "run.env")
    rows = load_probe(run / "probe.csv")
    if not rows:
        sys.exit("probe.csv is empty")
    s = summarize(rows, load_health(run / "health.csv"), env)
    write_markdown(s, env, run / "results.md")
    write_svg(rows, s, env, run / "timeline.svg")
    print((run / "results.md").read_text())


if __name__ == "__main__":
    main()
