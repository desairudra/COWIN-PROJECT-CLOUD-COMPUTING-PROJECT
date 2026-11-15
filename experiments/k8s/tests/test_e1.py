import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "plot_e1", pathlib.Path(__file__).resolve().parents[2] / "e1_hpa" / "plot_e1.py")
plot_e1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plot_e1)

ENV = {"LOW": "20", "PEAK": "300", "HOLD": "180s", "COOL": "180s"}


def test_stages_and_planned_load():
    st = plot_e1.stages(ENV)
    assert (st["ramp_at"], st["peak_at"], st["drop_at"], st["low_again"], st["end"]) == (30, 90, 270, 290, 470)
    assert plot_e1.planned(0, ENV, st) == 20
    assert plot_e1.planned(60, ENV, st) == 160
    assert plot_e1.planned(200, ENV, st) == 300
    assert plot_e1.planned(280, ENV, st) == 160
    assert plot_e1.planned(400, ENV, st) == 20


def test_summary_finds_scale_up_and_down():
    st = plot_e1.stages(ENV)
    hpa = [{"t": float(t), "current": c, "desired": d, "cpu": cpu} for t, c, d, cpu in
           [(0, 2, 2, 20), (35, 2, 2, 40), (45, 2, 4, 90), (60, 4, 6, 110), (100, 6, 6, 60),
            (280, 6, 6, 30), (300, 5, 2, 10), (340, 2, 2, 10)]]
    ready = {t: (2 if t < 50 else 4 if t < 70 else 6 if t < 300 else 2) for t in range(0, 470)}
    per = {t: {"ms": [10.0] * 5, "reqs": 5, "failed": 0} for t in range(0, 470)}
    s = plot_e1.summarize(ENV, st, per, hpa, ready)
    assert s["floor"] == 2
    assert s["first_up"] == 15
    assert s["max_desired"] == 6 and s["max_ready"] == 6
    assert s["max_ready_after"] == 40
    assert s["back_down"] == 70
    assert s["max_cpu"] == 110
    assert s["failed"] == 0 and s["reqs"] == 470 * 5
    assert [w[0] for w in s["windows"]][0] == "Before the spike"


def test_seconds_parser():
    assert plot_e1.secs("180s") == 180 and plot_e1.secs(30) == 30
