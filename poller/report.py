import collections
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# Same patterns as tools/vulkan_validation_gate.py upstream.
DIAGNOSTIC = re.compile(r"\b(VUID-[A-Za-z0-9_-]+|SYNC-HAZARD-[A-Za-z0-9_-]+)")
MESSAGE = re.compile(r"Validation (?:Error|Warning):|(?:VUID-|SYNC-HAZARD-)[\w-]+\s*\((?:ERROR|WARNING)\s*/")
MISSING_LAYER = "was not found but was requested by env var VK_INSTANCE_LAYERS"
ERROR_LINE = re.compile(r"(\berror\b:|^FAILED:|CMake Error|undefined reference|fatal error)", re.IGNORECASE)
SLOW_SECONDS = 30
MAX_COMMENT = 60000
NINJA_PROGRESS = re.compile(r"^\[\d+/\d+\] (Building|Linking|Generating|Creating|Copying|Running|Performing|Install|Scanning)")
LOG_FILES = ("steps.tsv", "infra.txt", "configure.log", "registration.log", "build.log", "libs.log", "select.log",
             "cputests.log", "tests.log", "rerun.log", "confirm-1.log", "confirm-2.log", "skips.log", "container.log")
MAX_LOG_BYTES = 900_000


def read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def parse_junit(path):
    tests = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    for case in root.iter("testcase"):
        name = case.get("name") or "?"
        output = "\n".join(case.findtext(ch) or "" for ch in ("system-out", "system-err"))
        problem = case.find("failure") if case.find("failure") is not None else case.find("error")
        message = (problem.get("message") or "").strip() if problem is not None else ""
        if problem is not None or case.get("status") == "fail":
            status = "failed"
        elif case.find("skipped") is not None or case.get("status") in ("notrun", "disabled"):
            status = "skipped"
        else:
            status = "passed"
        diagnostics = collections.Counter()
        missing = False
        for line in output.splitlines():
            if MESSAGE.search(line):
                diagnostics.update(DIAGNOSTIC.findall(line))
            missing |= MISSING_LAYER in line
        tests[name] = {"status": status, "time": float(case.get("time") or 0), "output": output, "message": message,
                       "diagnostics": diagnostics, "missing_layer": missing}
    return tests


def load_run(directory):
    directory = Path(directory)
    steps = {}
    for line in read(directory / "steps.tsv").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            steps[parts[0]] = (int(parts[1]), int(parts[2]))
    tests = parse_junit(directory / "ctest.xml")
    if tests is not None:
        tests = {**(parse_junit(directory / "cpu.xml") or {}), **tests}
    rerun = parse_junit(directory / "rerun.xml") or {}
    retried = sorted(n for n, t in rerun.items() if t["status"] == "failed")
    flaky = []
    for name, test in (tests or {}).items():
        if test["status"] == "failed" and rerun.get(name, {}).get("status") == "passed":
            test["status"] = "passed"
            test["flaky"] = True
            flaky.append((name, test["message"] or "failed"))
    validation = parse_junit(directory / "validation.xml")
    confirm = [parse_junit(p) or {} for p in sorted(directory.glob("confirm-*.xml"))]
    gpu = read(directory / "gpu.txt")
    devices = [blk for blk in re.split(r"\n(?=GPU\d+:)", gpu) if re.match(r"GPU\d+:", blk)]
    nvidia = [blk for blk in devices if re.search(r"deviceName\s*=\s*NVIDIA", blk)]
    block = nvidia[0] if nvidia else ""
    device = re.search(r"deviceName\s*=\s*(.+)", block)
    driver = re.search(r"driverInfo\s*=\s*(.+)", block)
    api = re.search(r"apiVersion\s*=\s*(.+)", block)
    return {
        "dir": directory,
        "steps": steps,
        "configure_ok": steps.get("configure", (1,))[0] == 0,
        "build_ok": steps.get("build", (1,))[0] == 0 and steps.get("libs", (1,))[0] == 0,
        "registration_ok": steps.get("registration", (0,))[0] == 0,
        "tests": tests,
        "validation": validation,
        "failed": sorted(n for n, t in (tests or {}).items() if t["status"] == "failed"),
        "flaky": sorted(n for n, _ in flaky),
        "flaky_reasons": dict(flaky),
        "retried": retried,
        "confirm": confirm,
        "selected": (directory / "select.tests").exists(),
        "gpu": {"device": device.group(1).strip() if device else "unknown",
                "driver": driver.group(1).strip() if driver else "unknown",
                "api": api.group(1).strip() if api else "unknown"},
        "nvidia_visible": bool(nvidia),
        "skips_log": read(directory / "skips.log"),
        "skips_ok": steps.get("skips", (0,))[0] == 0,
        "container_code": read(directory / "container.code").strip(),
    }


def listed_tests(directory):
    return set(re.findall(r"Test\s+#\d+: (\S+)", read(Path(directory) / "all-tests.txt")))


def junit_cases(path):
    try:
        return list(ET.parse(path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return []


def append_cases(path, cases):
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        tree = ET.ElementTree(ET.Element("testsuite", {"name": "carried"}))
    root = tree.getroot()
    for case in cases:
        root.append(case)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def carry_over(run_dir, base_dir):
    run_dir, base_dir = Path(run_dir), Path(base_dir)
    listed = listed_tests(run_dir)
    keep = lambda name: not listed or name in listed
    ran = {c.get("name") for f in ("cpu.xml", "ctest.xml") for c in junit_cases(run_dir / f)}
    previous = {}
    for f in ("cpu.xml", "ctest.xml"):
        for case in junit_cases(base_dir / f):
            previous[case.get("name")] = case
    carried = [c for n, c in previous.items() if keep(n) and n not in ran]
    append_cases(run_dir / "ctest.xml", carried)
    append_cases(run_dir / "rerun.xml", [c for c in junit_cases(base_dir / "rerun.xml")
                                         if keep(c.get("name")) and c.get("name") not in ran])
    validated = {c.get("name") for c in junit_cases(run_dir / "validation.xml")}
    append_cases(run_dir / "validation.xml", [c for c in junit_cases(base_dir / "validation.xml")
                                              if keep(c.get("name")) and c.get("name") not in validated])
    (run_dir / "carried.txt").write_text(f"{len(carried)} results carried over from {base_dir.name}, {len(ran)} run\n")
    return len(carried), len(ran)


TOOLING = re.compile(r"^(ccache: error: .*|.*No space left on device.*|.*virtual memory exhausted.*|"
                     r".*g\+\+: fatal error: Killed signal terminated program.*)$", re.MULTILINE)


def tooling_error(directory):
    for name in ("configure", "build", "libs"):
        match = TOOLING.search(read(Path(directory) / f"{name}.log"))
        if match:
            return match.group(1)[:300]
    return None


def new_validation_tests(run_dir, base_dir):
    pr = parse_junit(Path(run_dir) / "validation.xml")
    base = parse_junit(Path(base_dir) / "validation.xml")
    new, _, _, _ = validation_compare(pr, base)
    return sorted(new)


def split_confirmed(new_vuids, confirm):
    if not confirm:
        return {t: {v: (c, None) for v, c in ids.items()} for t, ids in new_vuids.items()}, {}
    confirmed, intermittent = {}, {}
    for test, ids in new_vuids.items():
        for vuid, count in ids.items():
            seen = sum(run.get(test, {}).get("diagnostics", {}).get(vuid, 0) for run in confirm)
            target = confirmed if seen else intermittent
            target.setdefault(test, {})[vuid] = (count, seen)
    return confirmed, intermittent


def tail(text, lines=25):
    rows = [r for r in text.strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def error_excerpt(text, limit=60):
    rows = text.splitlines()
    picked, seen = [], set()
    for i, row in enumerate(rows):
        if ERROR_LINE.search(row):
            for j in range(max(0, i - 2), min(len(rows), i + 6)):
                if j not in seen:
                    seen.add(j)
                    picked.append(rows[j])
            if len(picked) >= limit:
                break
    return "\n".join(picked[:limit]) or tail(text, 40)


def log_files(directory):
    files = {}
    for index, name in enumerate(LOG_FILES, 1):
        text = read(Path(directory) / name)
        if name == "build.log":
            rows = text.splitlines()
            kept = [row for row in rows if not NINJA_PROGRESS.match(row)]
            if len(kept) < len(rows):
                text = f"({len(rows) - len(kept)} ninja progress lines removed)\n" + "\n".join(kept) + "\n"
        if not text.strip():
            continue
        data = text.encode()
        if len(data) > MAX_LOG_BYTES:
            head, tail = data[:MAX_LOG_BYTES // 3], data[-(MAX_LOG_BYTES * 2 // 3):]
            text = (head.decode(errors="ignore") + f"\n\n… {len(data) - len(head) - len(tail)} bytes cut; "
                    "the full file is on the test machine …\n\n" + tail.decode(errors="ignore"))
        files[f"{index:02d}-{name}"] = {"content": text}
    return files


def details(summary, body, lang=""):
    body = body.replace("```", "'''")
    return f"<details><summary>{summary}</summary>\n\n```{lang}\n{body}\n```\n\n</details>\n"


def fmt_duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def validation_compare(pr, base):
    new, existing, missing, uncompared = {}, {}, [], []
    for name, test in (pr or {}).items():
        if name not in (base or {}):
            uncompared.append(name)
            continue
        base_ids = set(base[name]["diagnostics"])
        ids = set(test["diagnostics"])
        if ids - base_ids:
            new[name] = {i: test["diagnostics"][i] for i in sorted(ids - base_ids)}
        if ids & base_ids:
            existing[name] = sorted(ids & base_ids)
        if test["missing_layer"]:
            missing.append(name)
    return new, existing, missing, uncompared


def render(ctx, result, base):
    short = lambda sha: sha[:7]
    number = ctx["number"]
    header_rows = [f"PR head `{short(ctx['head'])}` merged onto main `{short(ctx['main'])}` · "
                   f"tested {ctx['finished']:%Y-%m-%d %H:%M} UTC · {fmt_duration(ctx['duration'])}", ""]
    repro = (f"    git fetch origin pull/{number}/head && git checkout {ctx['main'][:12]} && git merge --no-ff FETCH_HEAD\n"
             "    git submodule update --init --recursive\n"
             "    cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DANYPS5_ENABLE_SPIRV_TOOLS=ON\n"
             "    cmake --build build && cmake --build build --target libs\n"
             "    ctest --test-dir build -E '" + ctx["validation_tests"] + "' --output-on-failure\n"
             "    ANYPS5_REQUIRE_VULKAN=1 ANYPS5_NO_SHADER_CACHE=1 VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation \\\n"
             "      ctest --test-dir build -R '" + ctx["validation_tests"] + "' --output-on-failure\n")
    footer = ("\n<sub>Automated build and test on a contributor's NVIDIA machine; it does not replace upstream CI. "
              "A new comment is posted for each new push. "
              + (f"[Full logs]({ctx['logs_url']}).</sub>\n" if ctx.get("logs_url") else "Full logs are kept on the test machine.</sub>\n"))
    summary = {"counts": {}, "new_vuids": 0}

    if ctx["status"] == "conflict":
        body = (f"{ctx['marker']}\n## ⚠️ NVIDIA GPU test: needs rebase\n\n{header_rows[0]}\n\n"
                f"Doesn't merge cleanly into main `{short(ctx['main'])}`. Not built.\n{footer}")
        return "merge-conflict", body, summary
    if ctx["status"] != "ok" or result is None or not result["steps"]:
        body = f"{ctx['marker']}\n## NVIDIA GPU test: infrastructure error\n\n{header_rows[0]}\n"
        return "infra-error", body, summary
    built = result["configure_ok"] and result["build_ok"]
    if built and not result["nvidia_visible"]:
        body = f"{ctx['marker']}\n## NVIDIA GPU test: infrastructure error (no NVIDIA Vulkan device)\n"
        return "infra-error", body, summary

    gpu = result["gpu"]
    table = ["| | |", "|---|---|"]
    if built:
        table.append(f"| GPU | {gpu['device']}, driver {gpu['driver']}, Vulkan {gpu['api']} |")
    table.append(f"| Host | {ctx['host']}, container ubuntu:24.04 |")
    if ctx.get("logs_url"):
        table.append(f"| Logs | [build and test logs for this run]({ctx['logs_url']}) |")
    sections = []
    base_failed = set(base["failed"]) if base else set()
    base_tests = (base or {}).get("tests") or {}

    if not result["configure_ok"] or not result["build_ok"]:
        stage = "Configure" if not result["configure_ok"] else "Build"
        log_name = "configure" if not result["configure_ok"] else ("build" if result["steps"].get("build", (1,))[0] else "libs")
        base_note = "" if not base or base["build_ok"] else f" (main `{short(ctx['main'])}` also fails to build)"
        table.append(f"| {stage} | ❌ failed{base_note} |")
        table.append("| Tests | not run |")
        sections.append(details(f"{log_name}: first errors", error_excerpt(read(result["dir"] / f"{log_name}.log"))))
        outcome, icon, word = "build-failed", "❌", "build failed"
    else:
        tests = result["tests"] or {}
        counts = collections.Counter(t["status"] for t in tests.values())
        summary["counts"] = dict(counts)
        unstable = ctx.get("main_unstable") or {}
        base_failed |= {n for n, (k, total) in unstable.items() if k == total}
        new_fail = [n for n in result["failed"] if n not in base_failed and n not in unstable]
        unstable_fail = [n for n in result["failed"] if n not in base_failed and n in unstable]
        old_fail = [n for n in result["failed"] if n in base_failed]
        new_vuids, existing, missing, uncompared = validation_compare(result["validation"], (base or {}).get("validation"))
        new_vuids, intermittent = split_confirmed(new_vuids, result["confirm"])
        summary["new_vuids"] = sum(len(v) for v in new_vuids.values())
        slow = sorted(((t["time"], n) for n, t in tests.items() if t["time"] > SLOW_SECONDS), reverse=True)

        table.append("| Build | ✅ passed (Release, `-DANYPS5_ENABLE_SPIRV_TOOLS=ON`) |")
        if not result["registration_ok"]:
            table.append("| Test registration | ❌ `tools/check_test_registration.py` failed |")
        test_icon = "❌" if new_fail else "✅"
        failed_cell = f"{counts['failed']} failed"
        if result["failed"]:
            failed_cell += (f" ({'**' if new_fail else ''}{len(new_fail)} new{'**' if new_fail else ''}, "
                            f"{len(old_fail)} also fail on main"
                            + (f", {len(unstable_fail)} unstable on main" if unstable_fail else "") + ")")
        test_cell = (f"{test_icon} {counts['passed']:,} passed · {failed_cell} · "
                     f"{counts['skipped']} skipped (`ANYPS5_REQUIRE_VULKAN=1`)")
        if result["tests"] is None:
            test_cell = "❌ no JUnit report (ctest did not finish)"
        table.append(f"| Tests | {test_cell} |")
        table.append("| Test setup | CPU-only tests without a GPU; GPU tests (and any CPU-only test that failed or "
                     "skipped without one) once on the GPU with `VK_LAYER_KHRONOS_validation` and "
                     "`ANYPS5_NO_SHADER_CACHE=1`; main ran the same way |")
        if result["selected"] and base_tests:
            table.append(f"| Test selection | {len(tests):,} of {len(base_tests):,} tests run: the ones this change can "
                         "reach, chosen by `tools/select_tests.py` from main's build graph |")
        validated = len(result["validation"] or {})
        if result["validation"] is None:
            table.append("| Vulkan validation | ⚠️ not run |")
        elif missing:
            table.append(f"| Vulkan validation | ❌ validation layer not loaded in {len(missing)} tests |")
        elif new_vuids:
            table.append(f"| Vulkan validation | ❌ **{summary['new_vuids']} new validation ID(s)** vs main "
                         f"({validated} tests run on the GPU) |")
        else:
            table.append(f"| Vulkan validation | ✅ no new validation IDs vs main ({validated} tests run on the GPU) |")
        table.append("| Slow tests (>30 s, GPU tests timed with validation layers) | " + (", ".join(f"⚠️ `{n}` {t:.1f} s" for t, n in slow[:5]) or "none") + " |")

        if new_fail:
            rows = ["### Failing tests", "", "| Test | Result | On main `" + short(ctx["main"]) + "` |", "|---|---|---|"]
            for name in new_fail:
                on_main = base_tests.get(name, {}).get("status", "not on main")
                rows.append(f"| `{name}` | ❌ failed | {'✅ passed' if on_main == 'passed' else on_main} |")
            sections.append("\n".join(rows) + "\n")
            for name in new_fail[:8]:
                reason = tests[name]["message"] or "failed"
                retried = " (also failed when re-run alone)" if name in (result.get("retried") or ()) else ""
                sections.append(details(f"{name}: {reason}{retried}, log excerpt",
                                        f"ctest: {reason}\n\n{tail(tests[name]['output'])}"))
        if new_vuids:
            rows = ["### New validation errors", "", "| ID | main | PR | PR, test re-run alone | Test |", "|---|---|---|---|---|"]
            for test, ids in new_vuids.items():
                for vuid, (count, seen) in ids.items():
                    alone = "not re-run" if seen is None else f"{seen} (in {len(result['confirm'])} runs)"
                    rows.append(f"| `{vuid}` | 0 | {count} | {alone} | `{test}` |")
            sections.append("\n".join(rows) + "\n")
            for test, ids in list(new_vuids.items())[:5]:
                lines = [l for l in result["validation"][test]["output"].splitlines()
                         if MESSAGE.search(l) and any(i in l for i in ids)]
                sections.append(details(f"{test}: new validation messages", "\n".join(l[:600] for l in lines[:10])))
        if intermittent:
            sections.append(details(
                f"Intermittent validation messages ({sum(len(v) for v in intermittent.values())}, seen once in the "
                f"parallel run, not reproduced when the test was re-run alone {len(result['confirm'])} times; "
                "not counted against this PR)",
                "\n".join(f"{t}: {v} ({c}x)" for t, ids in intermittent.items() for v, (c, _) in ids.items())))
        if unstable_fail:
            sections.append(details(
                f"Unstable on main ({len(unstable_fail)}, these also failed in recent main runs on this machine; "
                "not counted against this PR)",
                "\n".join(f"{n}: failed in {unstable[n][0]} of the last {unstable[n][1]} main runs" for n in unstable_fail)))
        if old_fail:
            sections.append(details(f"Failing on main too ({len(old_fail)}, not counted against this PR)",
                                    "\n".join(old_fail)))
        if result["flaky"]:
            sections.append(details(f"Passed on retry ({len(result['flaky'])}, failed in the parallel run, passed when "
                                    "re-run with a 360 s timeout; not counted against this PR)",
                                    "\n".join(f"{n}: {result['flaky_reasons'].get(n, 'failed')} in the parallel run"
                                              for n in result["flaky"])))
        if uncompared:
            sections.append(details(f"Validated but not compared ({len(uncompared)}, not in the main baseline's "
                                    "validation pass)", "\n".join(sorted(uncompared))))
        if existing:
            by_id = {}
            for t, ids in existing.items():
                for i in ids:
                    by_id.setdefault(i, []).append(t)
            sections.append(details(
                f"Validation IDs already present on main ({len(by_id)} IDs in {len(existing)} tests)",
                "\n".join(f"{i} ({len(ts)} test{'s' if len(ts) != 1 else ''}):{', '.join(sorted(ts)[:8])}"
                          + (f", and {len(ts) - 8} more" if len(ts) > 8 else "")
                          for i, ts in sorted(by_id.items(), key=lambda kv: (-len(kv[1]), kv[0])))))
        skipped = sorted(n for n, t in tests.items() if t["status"] == "skipped")
        if skipped:
            sections.append(details(f"Skipped ({len(skipped)})", (result["skips_log"].strip() or "\n".join(skipped))[:4000]))

        if new_fail or new_vuids or missing or result["tests"] is None or not result["registration_ok"]:
            outcome, icon, word = ("validation-failed" if not new_fail and result["tests"] is not None
                                   and result["registration_ok"] and (new_vuids or missing) else "tests-failed"), "❌", "failed"
        else:
            outcome, icon, word = "passed", "✅", "passed"

    body = "\n".join([ctx["marker"], f"## {icon} NVIDIA GPU test: {word}", "", header_rows[0], "", *table, "",
                      *sections, details("Reproduce", repro.replace("    ", "")), footer])
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + "\n\n…truncated; see the full logs.\n" + footer
    return outcome, body, summary
