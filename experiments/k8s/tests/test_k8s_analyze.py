import csv
import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location("k8s_analyze", pathlib.Path(__file__).resolve().parents[1] / "analyze.py")
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)


def make_run(tmp, group="node", fail_window=(30.0, 31.0), slow_window=None, rollout=False):
    run = tmp / "run"
    run.mkdir()
    (run / "run.env").write_text(f"EXPERIMENT=E\nMODE=m\nTITLE=Test run\nGROUP={group}\nRATE=10\n"
                                 f"DURATION=60\nPODS=4\nURL=http://localhost:8080/x\n")
    with open(run / "probe.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "t_s", "time_utc", "status", "zone", "instance", "ms", "error", "release"])
        for n in range(600):
            t = n / 10
            failed = fail_window and fail_window[0] <= t < fail_window[1]
            slow = slow_window and slow_window[0] <= t < slow_window[1]
            release = "v2" if rollout and t >= 25 + (n % 4) * 3 else "v1"
            node = "cowin-worker" if n % 2 else "cowin-worker2"
            w.writerow([n + 1, f"{t:.3f}", "", 0 if failed else 200, "" if failed else node,
                        f"lookup-{n % 4}", 2000 if failed else (1200 if slow else 20),
                        "URLError" if failed else "", "" if failed else release])
    with open(run / "pods.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "pod", "node", "phase", "terminating", "ready", "release"])
        for t in range(60):
            n_ready = 3 if 30 <= t < 34 else 4
            for i in range(4):
                w.writerow([f"{t}.10", f"lookup-{i}", "cowin-worker" if i % 2 else "cowin-worker2", "Running", 0,
                            "True" if i < n_ready else "False", "v2" if rollout and t >= 25 + i * 3 else "v1"])
    (run / "events.csv").write_text("t_s,event\n30.00,killed lookup-3\n" if not rollout
                                    else "t_s,event\n20.00,rollout of v2 started\n40.00,rollout finished\n")
    (run / "nodes.csv").write_text("node,zone\ncowin-control-plane,-\ncowin-worker,ap-south-1a\ncowin-worker2,ap-south-1b\n")
    return run


def test_pod_kill_run(tmp_path, capsys):
    run = make_run(tmp_path)
    env, probe, pods, events, zones = analyze.load(run)
    s = analyze.summarize(env, probe, pods, events, zones)
    assert s["total"] == 600 and s["failed"] == 10
    assert 1.0 < s["max_gap"] < 1.2
    assert s["recoveries"][0]["pods_before"] == 4
    assert s["recoveries"][0]["back_after"] == 4
    assert s["recoveries"][0]["failed_30s"] == 10
    assert analyze.verdict(s) == "No outage, but some requests failed"
    assert "cowin-worker (ap-south-1a)" in s["by_group"]
    sys.argv = ["analyze.py", str(run)]
    analyze.main()
    text = (run / "results.md").read_text()
    assert "killed lookup-3" in text and "URLError: 10" in text
    svg = (run / "timeline.svg").read_text()
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_clean_rollout(tmp_path):
    run = make_run(tmp_path, group="release", fail_window=None, rollout=True)
    env, probe, pods, events, zones = analyze.load(run)
    s = analyze.summarize(env, probe, pods, events, zones)
    assert s["failed"] == 0 and analyze.verdict(s) == "No downtime"
    assert s["rollout_s"] == 20.0
    assert 5 <= s["first_v2"] < 5.5
    assert s["mixed_s"] > 0


def test_slow_answers_are_counted(tmp_path):
    run = make_run(tmp_path, fail_window=None, slow_window=(10, 12))
    env, probe, pods, events, zones = analyze.load(run)
    s = analyze.summarize(env, probe, pods, events, zones)
    assert s["slow"] == 20 and s["failed"] == 0


def test_long_outage_verdict():
    s = {"failed": 50, "max_gap": 12.3}
    assert analyze.verdict(s) == "Outage of 12.3 s"


def test_nothing_answered_is_an_outage(tmp_path):
    run = make_run(tmp_path, fail_window=(0, 60))
    env, probe, pods, events, zones = analyze.load(run)
    s = analyze.summarize(env, probe, pods, events, zones)
    assert s["ok"] == 0 and s["max_gap"] > 59
    assert analyze.verdict(s).startswith("Outage")
