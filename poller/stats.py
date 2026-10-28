import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from prometheus_client import Gauge, start_http_server

DATA = Path(os.environ.get("DATA_DIR", "/data"))
WINDOW = int(os.environ.get("STATS_WINDOW", "20") or 20)
GPU_TESTS = re.compile(os.environ.get("VALIDATION_TESTS", "^(agc|shader|recompiler|videoout|video_out|guest_video_out|vulkan)|gpu"))
BUILD_STEPS = ("configure", "registration", "build", "libs", "select", "graph", "cputests")
GPU_STEPS = ("tests", "rerun", "skips", "validation")

M_PHASE = Gauge("anyps5ci_avg_phase_seconds", "Average step duration over the last runs", ["kind", "phase"])
M_PART = Gauge("anyps5ci_avg_run_seconds", "Average build (CPU only) or GPU-test container time over the last runs",
               ["kind", "part"])
M_TEST = Gauge("anyps5ci_avg_test_seconds", "Average duration of one test in the normal pass", ["kind", "class"])
M_TESTS = Gauge("anyps5ci_avg_tests_per_run", "Average tests run in the normal pass", ["kind", "class"])
M_RUNS = Gauge("anyps5ci_stats_runs", "Runs the averages are taken over", ["kind"])

CACHE = {}


def steps(directory):
    out = {}
    try:
        for line in (directory / "steps.tsv").read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[2].isdigit():
                out[parts[0]] = int(parts[2])
    except OSError:
        pass
    return out


def test_times(path):
    try:
        key = (path, path.stat().st_mtime)
    except OSError:
        return None
    if key not in CACHE:
        totals = {"cpu": [0.0, 0], "gpu": [0.0, 0]}
        try:
            cases = list(ET.parse(path).getroot().iter("testcase"))
            cpu = path.with_name("cpu.xml")
            if cpu.exists():
                cases += list(ET.parse(cpu).getroot().iter("testcase"))
            for case in cases:
                cls = "gpu" if GPU_TESTS.search(case.get("name") or "") else "cpu"
                totals[cls][0] += float(case.get("time") or 0)
                totals[cls][1] += 1
        except (OSError, ET.ParseError):
            return None
        CACHE[key] = totals
    return CACHE[key]


def finished_runs(kind):
    pattern = "runs/pr-*/*/" if kind == "pr" else "baselines/*/"
    runs = [d for d in DATA.glob(pattern) if (d / "ctest.xml").exists() and "skips" in steps(d)]
    return sorted(runs, key=lambda d: (d / "ctest.xml").stat().st_mtime)[-WINDOW:]


def update():
    for kind in ("pr", "baseline"):
        runs = finished_runs(kind)
        M_RUNS.labels(kind).set(len(runs))
        if not runs:
            continue
        all_steps = [steps(d) for d in runs]
        names = {n for s in all_steps for n in s}
        for name in names:
            values = [s[name] for s in all_steps if name in s]
            M_PHASE.labels(kind, name).set(sum(values) / len(values))
        builds = [sum(s.get(n, 0) for n in BUILD_STEPS) for s in all_steps]
        gpus = [sum(v for n, v in s.items() if n in GPU_STEPS or n.startswith("confirm-")) for s in all_steps]
        M_PART.labels(kind, "build").set(sum(builds) / len(builds))
        M_PART.labels(kind, "gpu").set(sum(gpus) / len(gpus))
        sums = {"cpu": [0.0, 0, 0], "gpu": [0.0, 0, 0]}
        for d in runs:
            times = test_times(d / "ctest.xml")
            if not times:
                continue
            for cls, (seconds, count) in times.items():
                sums[cls][0] += seconds
                sums[cls][1] += count
                sums[cls][2] += 1
        for cls, (seconds, count, nruns) in sums.items():
            if count:
                M_TEST.labels(kind, cls).set(seconds / count)
                M_TESTS.labels(kind, cls).set(count / nruns)


if __name__ == "__main__":
    start_http_server(9201)
    while True:
        try:
            update()
        except Exception as error:
            print("stats update failed:", error, flush=True)
        time.sleep(120)
