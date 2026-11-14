import csv
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import analyze  # noqa: E402

FIELDS = ["metric_name", "timestamp", "metric_value", "check", "error", "error_code", "expected_response",
          "group", "method", "name", "proto", "scenario", "service", "status", "subproto", "tls_version",
          "url", "extra_tags", "metadata"]


def request(t, scen, ms, status=200, name="by_city", zone="a"):
    ok = 200 <= status < 400
    base = {"timestamp": t, "scenario": scen, "name": name, "status": status if status else "",
            "expected_response": str(ok).lower()}
    rows = [{**base, "metric_name": "http_reqs", "metric_value": 1},
            {**base, "metric_name": "http_req_duration", "metric_value": ms},
            {**base, "metric_name": "http_req_failed", "metric_value": 0 if ok else 1},
            {**base, "metric_name": "iterations", "metric_value": 1}]
    if zone:
        rows.append({**base, "metric_name": f"zone_{zone}", "metric_value": 1})
    return rows


def write_raw(path, steps, t0=1_700_000_000):
    """steps: list of (rate, seconds, ms, status, dropped_per_second)."""
    rows = [{"metric_name": "http_reqs", "timestamp": t0, "metric_value": 1, "scenario": "", "status": 200}]
    t = t0
    for rate, seconds, ms, status, dropped in steps:
        scen = f"rate_{rate}"
        for _ in range(seconds):
            for i in range(rate):
                rows += request(t, scen, ms + i % 3, status, zone="ab"[i % 2])
            if dropped:
                rows.append({"metric_name": "dropped_iterations", "timestamp": t, "metric_value": dropped,
                             "scenario": scen})
            t += 1
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return path


def run(tmp_path, steps, *extra):
    raw = write_raw(tmp_path / "raw.csv", steps)
    out = tmp_path / "out"
    analyze.main(["--raw", str(raw), "--out", str(out), "--title", "test", *extra])
    return out, json.loads((out / "steps.json").read_text())


def test_percentiles():
    assert analyze.pct([], 95) == 0
    assert analyze.pct([5], 99) == 5
    assert analyze.pct(list(range(1, 101)), 50) == pytest.approx(50.5)
    assert analyze.pct(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_all_steps_pass(tmp_path):
    out, rows = run(tmp_path, [(5, 4, 10, 200, 0), (10, 4, 20, 200, 0)])
    assert [r["target"] for r in rows] == [5, 10]
    assert all(r["ok"] for r in rows)
    assert rows[1]["reqs"] == 40 and rows[1]["failed"] == 0
    assert rows[1]["zone_a"] == rows[1]["zone_b"] == 20
    text = (out / "results.md").read_text()
    assert "Every step passed" in text
    assert "50% / 50%" in text


def test_slow_step_breaks(tmp_path):
    out, rows = run(tmp_path, [(5, 4, 10, 200, 0), (10, 4, 900, 200, 0)])
    assert rows[0]["ok"] and not rows[1]["ok"]
    assert "p95" in rows[1]["why"][0]
    best, broke = analyze.capacity(rows)
    assert best["target"] == 5 and broke["target"] == 10
    assert "Handled 5 requests/s" in (out / "results.md").read_text()


def test_errors_break_a_step(tmp_path):
    _, rows = run(tmp_path, [(10, 3, 10, 503, 0)])
    assert rows[0]["errors"] == 1.0
    assert not rows[0]["ok"]
    assert any("failed" in w for w in rows[0]["why"])


def test_dropped_load_breaks_a_step(tmp_path):
    out, rows = run(tmp_path, [(10, 4, 10, 200, 2)])
    assert rows[0]["dropped"] == 8
    assert rows[0]["sent"] == pytest.approx(40 / 48)
    assert not rows[0]["ok"]
    assert "40 of 48" in (out / "results.md").read_text()


def test_timeouts_show_as_code_zero(tmp_path):
    out, _ = run(tmp_path, [(4, 2, 10, 0, 0)])
    assert "0 = no answer" in (out / "results.md").read_text()


def test_capacity_stops_at_first_failure():
    rows = [{"ok": True, "target": 1}, {"ok": False, "target": 2}, {"ok": True, "target": 3}]
    best, broke = analyze.capacity(rows)
    assert best["target"] == 1 and broke["target"] == 2


def test_outputs_and_check_flag(tmp_path):
    out, _ = run(tmp_path, [(5, 3, 10, 200, 0)], "--check")
    for f in ("results.md", "per_second.csv", "load-chart.svg", "steps.json"):
        assert (out / f).exists()
    svg = (out / "load-chart.svg").read_text()
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    with open(out / "per_second.csv") as f:
        sec = list(csv.DictReader(f))
    assert len(sec) == 3 and sec[0]["target"] == "5"

    (tmp_path / "bad").mkdir()
    with pytest.raises(SystemExit):
        run(tmp_path / "bad", [(5, 3, 10, 500, 0)], "--check")


def test_setup_requests_are_ignored(tmp_path):
    _, rows = run(tmp_path, [(5, 2, 10, 200, 0)])
    assert len(rows) == 1 and rows[0]["reqs"] == 10


def test_step_length_sets_the_planned_line(tmp_path):
    out, _ = run(tmp_path, [(5, 3, 10, 200, 0), (10, 3, 10, 200, 0)], "--step", "3")
    with open(out / "per_second.csv") as f:
        targets = [int(r["target"]) for r in csv.DictReader(f)]
    assert targets == [5, 5, 5, 10, 10, 10]
