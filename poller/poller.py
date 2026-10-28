import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from prometheus_client import Counter, Gauge, start_http_server

import report

ENV = os.environ


def env_bool(name, default=False):
    return ENV.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(ENV.get(name, "").strip() or default)
    except ValueError:
        return default


def env_list(name):
    return [item.strip() for item in ENV.get(name, "").split(",") if item.strip()]


CFG = {
    "token": ENV.get("GITHUB_TOKEN", "").strip(),
    "repo": ENV.get("GITHUB_REPO", "boykopovar/AnyPS5").strip(),
    "post": env_bool("POST_COMMENTS"),
    "gist_logs": env_bool("GIST_LOGS", True),
    "marker": ENV.get("COMMENT_MARKER", "<!-- anyps5-nvidia-ci -->").strip(),
    "filter": ENV.get("PR_FILTER", "gpu-paths").strip(),
    "gpu_paths": env_list("GPU_PATHS"),
    "include": {int(n) for n in env_list("INCLUDE_PRS") if n.isdigit()},
    "exclude": {int(n) for n in env_list("EXCLUDE_PRS") if n.isdigit()},
    "skip_drafts": env_bool("SKIP_DRAFTS", True),
    "max_age_days": env_int("MAX_PR_AGE_DAYS", 30),
    "interval": env_int("POLL_INTERVAL_SECONDS", 900),
    "baseline_interval": env_int("BASELINE_INTERVAL_MINUTES", 360) * 60,
    "baseline_full": env_int("BASELINE_FULL_HOURS", 12) * 3600,
    "cpu_test_parallel": env_int("CPU_TEST_PARALLEL", 32),
    "max_builds": max(1, env_int("MAX_BUILDS", 20)),
    "build_cpus": max(1, env_int("BUILD_CPUS", 8)),
    "build_memory": ENV.get("BUILD_MEMORY", "16g").strip(),
    "max_gpu": max(1, env_int("MAX_GPU_RUNS", 3)),
    "test_memory": ENV.get("TEST_MEMORY", "32g").strip(),
    "test_parallel": env_int("TEST_PARALLEL", 16),
    "validation_tests": ENV.get("VALIDATION_TESTS", "^(agc|shader|recompiler|videoout|video_out|guest_video_out|vulkan)|gpu").strip(),
    "timeout": env_int("RUN_TIMEOUT_MINUTES", 120) * 60,
    "ccache_max": ENV.get("CCACHE_MAXSIZE", "60G").strip(),
    "gpu_name": ENV.get("GPU_NAME", "NVIDIA").strip(),
    "data": Path(ENV.get("DATA_DIR", "/data")),
    "uid": ENV.get("HOST_UID", "1000"),
    "gid": ENV.get("HOST_GID", "1000"),
    "image": ENV.get("RUNNER_IMAGE", "anyps5-gpu-ci-runner:latest"),
    "once": env_bool("RUN_ONCE"),
    "only_pr": env_int("ONLY_PR", 0),
}

DATA = CFG["data"]
MIRROR = DATA / "mirror.git"
TREES = DATA / "trees"
MAIN_SRC = TREES / "main" / "src"
MAIN_BUILD = TREES / "main" / "build"
BASELINES = DATA / "baselines"
RUNS = DATA / "runs"
FFMPEG = DATA / "ffmpeg"
CCACHE = DATA / "ccache"
STATE_FILE = DATA / "state.json"
UPSTREAM = f"https://github.com/{CFG['repo']}.git"
FFMPEG_URL = "https://github.com/KytyPS5/ext-ffmpeg-core/releases/download/{sha}/ffmpeg-linux-x64.zip"
MAX_INFRA_ATTEMPTS = 3
MAIN_HISTORY = 10
TEST_CONFIG = "single-gpu-pass-validation-uffd"
STAGES = ("prepare", "wait_build", "build", "wait_gpu", "test", "report")

M_CYCLE = Gauge("anyps5ci_last_cycle_timestamp_seconds", "End of the last PR list refresh")
M_OPEN = Gauge("anyps5ci_open_prs", "Open PRs against main")
M_ELIGIBLE = Gauge("anyps5ci_eligible_prs", "Open PRs that pass the filter")
M_QUEUE = Gauge("anyps5ci_queue_length", "PRs waiting to start")
M_STAGE = Gauge("anyps5ci_jobs", "Jobs per stage", ["stage"])
M_JOB = Gauge("anyps5ci_job_stage", "Stage of each running job", ["pr", "stage"])
M_LIMIT = Gauge("anyps5ci_pool_size", "Configured pool sizes", ["pool"])
M_WAITING = Gauge("anyps5ci_waiting_for_baseline", "PRs based on a main newer than the newest finished baseline")
M_BASELINE_BUSY = Gauge("anyps5ci_baseline_running", "1 while a main baseline runs")
M_RUNS = Counter("anyps5ci_runs_total", "Finished runs", ["outcome"])
M_DURATION = Gauge("anyps5ci_last_run_duration_seconds", "Duration of the last run", ["kind"])
M_RESULT = Gauge("anyps5ci_pr_result_timestamp_seconds", "Latest result per PR",
                 ["pr", "outcome", "head", "main", "title", "author"])
M_TESTS = Gauge("anyps5ci_pr_tests", "Test counts of the latest run per PR", ["pr", "status"])
M_NEW_VUIDS = Gauge("anyps5ci_pr_new_validation_ids", "New validation IDs vs main", ["pr"])
M_BASELINE = Gauge("anyps5ci_baseline_info", "Main baseline", ["main", "build", "failed_tests"])
M_COMMENTS = Counter("anyps5ci_comments_total", "Comments created or edited on GitHub", ["action"])
M_RATE = Gauge("anyps5ci_github_rate_remaining", "GitHub API calls remaining")
M_ERRORS = Counter("anyps5ci_errors_total", "Poller errors", ["where"])
M_POSTING = Gauge("anyps5ci_posting_enabled", "1 when POST_COMMENTS=true")
M_TESTS_DONE = Counter("anyps5ci_tests_finished_total", "Tests finished across all runs, from ctest checkpoints")
M_TESTS_RUNNING = Gauge("anyps5ci_test_processes", "ctest jobs allowed on the GPU across all runs")

STOP = threading.Event()
STATE_LOCK = threading.RLock()
FFMPEG_LOCK = threading.Lock()
BASELINE_LOCK = threading.Lock()
BUILD_SLOTS = threading.Semaphore(CFG["max_builds"])
GPU_SLOTS = threading.Semaphore(CFG["max_gpu"])
STAGE_LOCK = threading.Lock()
JOB_STAGES = {}


def log(*parts):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *parts, flush=True)


def set_stage(key, stage):
    with STAGE_LOCK:
        old = JOB_STAGES.pop(key, None)
        if old:
            M_JOB.remove(key, old)
        if stage:
            JOB_STAGES[key] = stage
            M_JOB.labels(key, stage).set(1)
        for name in STAGES:
            M_STAGE.labels(name).set(sum(1 for s in JOB_STAGES.values() if s == name))


def run(cmd, cwd=None, check=True, timeout=None, env=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                            env={**ENV, **(env or {})}, errors="replace")
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result


def git(*args, cwd=None, check=True, timeout=1800):
    base = ["git", "-c", "protocol.file.allow=user", "-c", "core.hooksPath=/dev/null",
            "-c", "advice.detachedHead=false", "-c", "user.name=anyps5-gpu-ci", "-c", "user.email=ci@localhost"]
    return run(base + list(args), cwd=cwd, check=check, timeout=timeout)


class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo, self.login = token, repo, None
        self.lock = threading.Lock()

    def request(self, method, path, body=None):
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "anyps5-gpu-ci"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as response:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                M_RATE.set(int(remaining))
            link = response.headers.get("Link", "")
            payload = response.read()
            return (json.loads(payload) if payload else None), link

    def paginate(self, path):
        items, url = [], path
        while url:
            page, link = self.request("GET", url)
            items.extend(page)
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return items

    def open_prs(self):
        return self.paginate(f"/repos/{self.repo}/pulls?state=open&base=main&sort=updated&direction=desc&per_page=100")

    def whoami(self):
        with self.lock:
            if self.login is None and self.token:
                self.login = self.request("GET", "/user")[0]["login"]
            return self.login

    def create_gist(self, description, files):
        created, _ = self.request("POST", "/gists", {"description": description, "public": False, "files": files})
        M_COMMENTS.labels("gist").inc()
        return created["html_url"]

    def post_comment(self, number, head, body, marker):
        me = self.whoami()
        tag = f"<!-- head {head} -->"
        body = body.replace(marker, f"{marker}\n{tag}", 1)
        for comment in self.paginate(f"/repos/{self.repo}/issues/{number}/comments?per_page=100"):
            if comment["user"]["login"] != me or not comment["body"].startswith(marker):
                continue
            if tag in comment["body"] or f"PR head `{head[:7]}`" in comment["body"]:
                if comment["body"] != body:
                    self.request("PATCH", f"/repos/{self.repo}/issues/comments/{comment['id']}", {"body": body})
                    M_COMMENTS.labels("edited").inc()
                return comment["html_url"]
        created, _ = self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        M_COMMENTS.labels("created").inc()
        return created["html_url"]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"prs": {}, "baselines": {}}


def record_main_history(state, key, result):
    history = [h for h in state.setdefault("main_history", []) if h["main"] != key]
    history.append({"main": key, "finished": time.time(), "failed": result["failed"], "flaky": result["flaky"]})
    state["main_history"] = history[-MAIN_HISTORY:]


def main_unstable(state):
    with STATE_LOCK:
        history = list(state.get("main_history", []))
    counts = {}
    for entry in history:
        for name in set(entry["failed"]) | set(entry["flaky"]):
            counts[name] = counts.get(name, 0) + 1
    return {name: (count, len(history)) for name, count in counts.items()}


def seed_main_history(state):
    if state.get("main_history"):
        return
    for directory in sorted(BASELINES.iterdir(), key=lambda p: p.stat().st_mtime):
        if (directory / "ctest.xml").exists():
            record_main_history(state, directory.name, report.load_run(directory))


def save_state(state):
    with STATE_LOCK:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        tmp.replace(STATE_FILE)


def fetch_mirror():
    if not MIRROR.exists():
        git("init", "--bare", "-q", str(MIRROR))
        git("remote", "add", "origin", UPSTREAM, cwd=MIRROR)
        git("config", "remote.origin.fetch", "+refs/heads/main:refs/heads/main", cwd=MIRROR)
        git("config", "--add", "remote.origin.fetch", "+refs/pull/*/head:refs/pull/*/head", cwd=MIRROR)
    git("config", "gc.auto", "0", cwd=MIRROR)
    git("fetch", "--quiet", "--prune", "origin", cwd=MIRROR)
    return git("rev-parse", "refs/heads/main", cwd=MIRROR).stdout.strip()


def changed_paths(main_sha, head):
    result = git("diff", "--name-only", "-z", f"{main_sha}...{head}", cwd=MIRROR, check=False)
    if result.returncode:
        return None
    return [p for p in result.stdout.split("\0") if p]


def based_on_newer_main(main_sha, newest_main, head):
    base = git("merge-base", head, newest_main, cwd=MIRROR, check=False).stdout.strip()
    if not base:
        return False
    return git("merge-base", "--is-ancestor", base, main_sha, cwd=MIRROR, check=False).returncode != 0


def checkout_tree(src, main_sha, pr_number=None, pr_head=None):
    if src == MAIN_SRC and (src / ".git").exists():
        git("fetch", "--quiet", str(MIRROR), "refs/heads/main", cwd=src)
        git("checkout", "--quiet", "--force", "--detach", main_sha, cwd=src)
        git("clean", "-qffdx", cwd=src)
    else:
        if src.exists():
            shutil.rmtree(src)
        src.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--shared", "--no-checkout", str(MIRROR), str(src))
        git("checkout", "--quiet", "--detach", main_sha, cwd=src)
        seed = MAIN_SRC / ".git" / "modules"
        if src != MAIN_SRC and seed.exists():
            with BASELINE_LOCK:
                run(["cp", "-al", str(seed), str(src / ".git" / "modules")])
    git("remote", "set-url", "origin", UPSTREAM, cwd=src)
    if pr_number is not None:
        actual = git("rev-parse", f"refs/pull/{pr_number}/head", cwd=MIRROR).stdout.strip()
        if actual != pr_head:
            return "stale"
        git("fetch", "--quiet", str(MIRROR), f"refs/pull/{pr_number}/head", cwd=src)
        merged = git("merge", "--no-ff", "--no-edit", "--quiet", pr_head, cwd=src, check=False)
        if merged.returncode:
            return "conflict"
    git("submodule", "sync", "--quiet", "--recursive", cwd=src)
    git("submodule", "update", "--init", "--recursive", "--force", "--jobs", "8", cwd=src, timeout=3600)
    return "ok"


def ffmpeg_dir(src):
    full = git("rev-parse", "HEAD:3rdparty/ffmpeg-core", cwd=src).stdout.strip()
    sha = full[:12]
    target = FFMPEG / f"ffmpeg-{sha}"
    with FFMPEG_LOCK:
        if (target / "include" / "libavcodec" / "avcodec.h").exists():
            return target
        FFMPEG.mkdir(parents=True, exist_ok=True)
        archive = FFMPEG / f"{sha}-ffmpeg-linux-x64.zip"
        req = urllib.request.Request(FFMPEG_URL.format(sha=sha), headers={"User-Agent": "anyps5-gpu-ci"})
        with urllib.request.urlopen(req, timeout=300) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out)
        tmp = FFMPEG / f".ffmpeg-{sha}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        tmp.replace(target)
        archive.unlink()
    if not (target / "include" / "libavcodec" / "avcodec.h").exists():
        raise RuntimeError(f"FFmpeg package {sha} has no include/libavcodec/avcodec.h")
    return target


def run_container(name, phase, src, build, out, ffmpeg, writable_cache, extra_env=None, extra_mounts=()):
    for path in (build, out):
        path.mkdir(parents=True, exist_ok=True)
    CCACHE.mkdir(parents=True, exist_ok=True)
    mounts = [(src, src, "rw"), (build, build, "rw"), (out, out, "rw"), (MIRROR, MIRROR, "ro"), (ffmpeg, ffmpeg, "ro"),
              *extra_mounts]
    gpu = phase in ("test", "confirm")
    env = {"PHASE": phase, "SRC": src, "BUILD": build, "OUT": out, "FFMPEG_DIR": ffmpeg,
           "JOBS": 0 if writable_cache else CFG["build_cpus"],
           "TEST_PARALLEL": CFG["test_parallel"], "CPU_TEST_PARALLEL": CFG["cpu_test_parallel"],
           "VALIDATION_TESTS": CFG["validation_tests"], "GPU_NAME": CFG["gpu_name"],
           "CCACHE_BASEDIR": TREES, "CCACHE_NOHASHDIR": "true", "CCACHE_COMPILERCHECK": "content",
           "CCACHE_MAXSIZE": CFG["ccache_max"], "HOME": "/tmp", **(extra_env or {})}
    if writable_cache:
        mounts.append((CCACHE, CCACHE, "rw"))
        env["CCACHE_DIR"] = CCACHE
    else:
        mounts.append((CCACHE, "/ccache-main", "ro"))
        env.update({"CCACHE_DIR": "/ccache-main", "CCACHE_READONLY": "true", "CCACHE_TEMPDIR": "/tmp/ccache-tmp"})
    cmd = ["docker", "run", "--name", name, "--network", "none",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--security-opt", f"seccomp={Path(__file__).with_name('seccomp.json')}",
           "--pids-limit", "32768", "--shm-size", "8g",
           "--read-only", "--tmpfs", "/tmp:rw,exec,size=16g",
           "--user", f"{CFG['uid']}:{CFG['gid']}", "--label", "anyps5-gpu-ci=runner"]
    if gpu:
        cmd += ["--device", "nvidia.com/gpu=all", "--memory", CFG["test_memory"]]
    elif not writable_cache:
        cmd += ["--cpus", str(CFG["build_cpus"]), "--memory", CFG["build_memory"]]
    for host, container, mode in mounts:
        cmd += ["-v", f"{host}:{container}:{mode}"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(CFG["image"])
    run(["docker", "rm", "-f", name], check=False)
    started = time.time()
    oom = False
    try:
        result = run(cmd, check=False, timeout=CFG["timeout"])
        code = result.returncode
        with open(out / "container.log", "a") as fh:
            fh.write(f"--- {phase}\n{result.stdout}{result.stderr}")
        inspect = run(["docker", "inspect", "-f", "{{.State.OOMKilled}}", name], check=False)
        oom = inspect.stdout.strip() == "true"
    except subprocess.TimeoutExpired:
        run(["docker", "kill", name], check=False)
        code = "timeout"
    finally:
        run(["docker", "rm", "-f", name], check=False)
    return code, oom, time.time() - started


class Baselines:
    def __init__(self, state):
        self.state = state
        self.events = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="baseline")
        self.in_use = {}

    def ensure(self, main_sha):
        key = main_sha[:12]
        with self.lock:
            if key not in self.events:
                event = threading.Event()
                self.events[key] = event
                with STATE_LOCK:
                    done = (self.state["baselines"].get(key, {}).get("config") == TEST_CONFIG
                            and (BASELINES / key / "ctest.xml").exists())
                if done:
                    event.set()
                else:
                    self.pool.submit(self.build, main_sha, event)
        return key

    def latest_ready(self):
        with STATE_LOCK:
            ready = [b for k, b in self.state["baselines"].items()
                     if b.get("config") == TEST_CONFIG and (BASELINES / k / "ctest.xml").exists()]
        return max(ready, key=lambda b: b["finished"], default=None)

    def target(self, newest_main):
        ready = self.latest_ready()
        if ready is None:
            self.ensure(newest_main)
            return newest_main
        if ready["main"] != newest_main and time.time() - ready["finished"] >= CFG["baseline_interval"]:
            self.ensure(newest_main)
        return ready["main"]

    def request(self, main_sha):
        key = self.ensure(main_sha)
        with self.lock:
            self.in_use[key] = self.in_use.get(key, 0) + 1
        return key

    def release(self, key):
        with self.lock:
            self.in_use[key] -= 1

    def wait(self, key):
        self.events[key].wait()
        return BASELINES / key

    def previous_for(self, main_sha):
        prev = self.latest_ready()
        if prev is None or not prev.get("build_ok"):
            return None
        with STATE_LOCK:
            last_full = self.state.get("last_full_baseline", 0)
        if time.time() - last_full >= CFG["baseline_full"]:
            return None
        if git("merge-base", "--is-ancestor", prev["main"], main_sha, cwd=MIRROR, check=False).returncode:
            return None
        return prev

    def build(self, main_sha, event):
        key = main_sha[:12]
        out = BASELINES / key
        try:
            prev = self.previous_for(main_sha)
            kind = f"incremental from {prev['main'][:12]}" if prev else "full"
            log(f"baseline: building main {key} ({kind})")
            M_BASELINE_BUSY.set(1)
            started = time.time()
            with BASELINE_LOCK:
                checkout_tree(MAIN_SRC, main_sha)
            ffmpeg = ffmpeg_dir(MAIN_SRC)
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            shutil.copytree(MAIN_SRC / "tools", out / "ci-tools")
            if prev:
                changed = git("diff", "--name-only", "-z", prev["main"], main_sha, cwd=MIRROR).stdout
                (out / "changed.txt").write_text(changed)
            run_container("anyps5-run-main-build", "build", MAIN_SRC, MAIN_BUILD, out, ffmpeg, writable_cache=True,
                          extra_env={"MODE": "baseline"}, extra_mounts=[(out / "ci-tools", "/ci-tools", "ro")])
            result = report.load_run(out)
            if result["build_ok"]:
                with GPU_SLOTS:
                    run_container("anyps5-run-main-test", "test", MAIN_SRC, MAIN_BUILD, out, ffmpeg, writable_cache=True)
                if prev and not (out / "select.tests").exists():
                    prev, kind = None, "full (selection chose a full run)"
                if prev:
                    carried, ran = report.carry_over(out, BASELINES / prev["main"][:12])
                    log(f"baseline: main {key} ran {ran} tests, carried {carried} from {prev['main'][:12]}")
            result = report.load_run(out)
            M_DURATION.labels("baseline").set(time.time() - started)
            with STATE_LOCK:
                self.state["baselines"][key] = {"main": main_sha, "finished": time.time(),
                                                "build_ok": result["build_ok"], "failed": result["failed"],
                                                "full": not prev, "config": TEST_CONFIG}
                if result["tests"] and not prev:
                    record_main_history(self.state, key, result)
                    self.state["last_full_baseline"] = time.time()
                save_state(self.state)
            M_BASELINE.clear()
            M_BASELINE.labels(key, "ok" if result["build_ok"] else "failed", str(len(result["failed"]))).set(time.time())
            log(f"baseline: main {key} ({kind}) build_ok={result['build_ok']} failed={len(result['failed'])} "
                f"in {time.time() - started:.0f}s")
            self.prune()
        except Exception:
            M_ERRORS.labels("baseline").inc()
            log("baseline failed:\n" + traceback.format_exc())
        finally:
            M_BASELINE_BUSY.set(0)
            event.set()

    def prune(self):
        with self.lock, STATE_LOCK:
            keep = {k for k, n in self.in_use.items() if n > 0}
            dirs = sorted(BASELINES.iterdir(), key=lambda p: p.stat().st_mtime)
            for old in dirs[:-3]:
                if old.name not in keep:
                    shutil.rmtree(old, ignore_errors=True)
                    self.state["baselines"].pop(old.name, None)
                    self.events.pop(old.name, None)


def host_info():
    os_name = "unknown"
    try:
        for line in Path("/host/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return f"{os_name}, kernel {os.uname().release}"


def needs_run(entry, head):
    if not entry or entry.get("head") != head:
        return True
    if entry.get("outcome") == "infra-error":
        return entry.get("attempts", 1) < MAX_INFRA_ATTEMPTS
    return False


def eligible(pr, main_sha):
    number = pr["number"]
    if number in CFG["exclude"]:
        return False
    if CFG["only_pr"] and number != CFG["only_pr"]:
        return False
    if number in CFG["include"] or CFG["only_pr"]:
        return True
    if CFG["skip_drafts"] and pr.get("draft"):
        return False
    if CFG["max_age_days"]:
        updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - updated).days > CFG["max_age_days"]:
            return False
    if CFG["filter"] == "all":
        return True
    paths = changed_paths(main_sha, pr["head"]["sha"])
    if paths is None:
        return False
    return any(p.startswith(prefix) for p in paths for prefix in CFG["gpu_paths"])


def test_paths(main_sha, head):
    changed = git("diff", "--name-only", f"{main_sha}...{head}", cwd=MIRROR, check=False).stdout.split()
    return [p for p in changed if "/tests/" in f"/{p}"]


def compare_on_main(number, main_sha, head, run_dir, paths, failing):
    out = run_dir / "main-tests"
    out.mkdir(exist_ok=True)
    tree = TREES / f"pr-{number}-main"
    src, build = tree / "src", tree / "build"
    try:
        checkout_tree(src, main_sha)
        ffmpeg = ffmpeg_dir(src)
        patch = git("diff", "--binary", f"{main_sha}...{head}", "--", *paths, cwd=MIRROR).stdout
        (out / "tests.patch").write_text(patch)
        applied = subprocess.run(["git", "apply", "--3way", "-"], input=patch, cwd=src, capture_output=True, text=True)
        if applied.returncode:
            (out / "apply.log").write_text(applied.stdout + applied.stderr)
            (out / "status.txt").write_text("patch-failed")
            return
        with BUILD_SLOTS:
            run_container(f"anyps5-run-pr{number}-main-build", "build", src, build, out, ffmpeg,
                          writable_cache=False, extra_env={"CPU_TESTS": 0})
        if not report.load_run(out)["build_ok"]:
            (out / "status.txt").write_text("build-failed")
            return
        pattern = "^(" + "|".join(re.escape(t) for t in failing) + ")$"
        with GPU_SLOTS:
            run_container(f"anyps5-run-pr{number}-main-test", "confirm", src, build, out, ffmpeg,
                          writable_cache=False, extra_env={"CONFIRM_TESTS": pattern, "CONFIRM_REPEAT": 1})
        (out / "status.txt").write_text("ok")
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def flaky_seen(state, now):
    history = state.get("flaky_history", {})
    return {name: sum(1 for t in times if now - t < 7 * 86400) for name, times in history.items()}


def record_flaky(state, names, now):
    history = state.setdefault("flaky_history", {})
    for name in names:
        history[name] = [t for t in history.get(name, []) if now - t < 7 * 86400] + [now]


def publish_result(pr, entry):
    number = str(pr["number"])
    for labels in list(M_RESULT._metrics):
        if labels[0] == number:
            M_RESULT.remove(*labels)
    M_RESULT.labels(number, entry["outcome"], entry["head"][:7], entry["main"][:7],
                    pr["title"][:80], pr["user"]["login"]).set(entry["finished"])
    for status, count in entry.get("counts", {}).items():
        M_TESTS.labels(number, status).set(count)
    M_NEW_VUIDS.labels(number).set(entry.get("new_vuids", 0))


def run_pr(state, gh, baselines, pr, main_sha):
    number, head = pr["number"], pr["head"]["sha"]
    key = str(number)
    tree = TREES / f"pr-{number}"
    src, build = tree / "src", tree / "build"
    run_dir = RUNS / f"pr-{number}" / f"{head[:12]}-{main_sha[:12]}"
    base_key = baselines.request(main_sha)
    started = time.time()
    try:
        log(f"PR #{number}: head {head[:12]} onto main {main_sha[:12]}")
        set_stage(key, "prepare")
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        status = checkout_tree(src, main_sha, number, head)
        if status == "stale":
            log(f"PR #{number}: has newer commits than the fetched head, skipping until next cycle")
            shutil.rmtree(run_dir, ignore_errors=True)
            return
        ffmpeg, infra = None, None
        if status == "ok":
            try:
                ffmpeg = ffmpeg_dir(src)
            except (urllib.error.URLError, RuntimeError, OSError) as error:
                (run_dir / "ffmpeg.log").write_text(str(error))
                status, infra = "ffmpeg", f"FFmpeg prebuilt unavailable: {error}"
        if status == "ok":
            extra_env, extra_mounts = {}, []
            base_dir = BASELINES / base_key
            if baselines.events[base_key].is_set() and (base_dir / "test-graph.json").exists():
                changed = git("diff", "--name-only", "-z", main_sha, "HEAD", cwd=src).stdout
                (run_dir / "changed.txt").write_text(changed)
                extra_env = {"GRAPH": base_dir / "test-graph.json"}
                extra_mounts = [(base_dir / "test-graph.json", base_dir / "test-graph.json", "ro"),
                                (base_dir / "ci-tools", "/ci-tools", "ro")]
            set_stage(key, "wait_build")
            with BUILD_SLOTS:
                set_stage(key, "build")
                code, oom, seconds = run_container(f"anyps5-run-pr{number}-build", "build", src, build, run_dir,
                                                   ffmpeg, writable_cache=False, extra_env=extra_env,
                                                   extra_mounts=extra_mounts)
            M_DURATION.labels("build").set(seconds)
            tooling = report.tooling_error(run_dir)
            if oom or code == "timeout":
                status, infra = "infra", f"build container {'ran out of memory' if oom else 'timed out'}"
            elif tooling:
                status, infra = "infra", f"build tooling error: {tooling}"
            elif report.load_run(run_dir)["build_ok"]:
                if baselines.events[base_key].is_set():
                    known = set(report.load_run(BASELINES / base_key)["failed"]) | set(main_unstable(state))
                    (run_dir / "known_failures.txt").write_text("".join(f"{name}\n" for name in known))
                set_stage(key, "wait_gpu")
                with GPU_SLOTS:
                    set_stage(key, "test")
                    code, oom, seconds = run_container(f"anyps5-run-pr{number}-test", "test", src, build, run_dir,
                                                       ffmpeg, writable_cache=False)
                    if not oom and code != "timeout" and baselines.events[base_key].is_set():
                        suspects = report.new_validation_tests(run_dir, BASELINES / base_key)
                        if suspects:
                            pattern = "^(" + "|".join(re.escape(t) for t in suspects) + ")$"
                            run_container(f"anyps5-run-pr{number}-confirm", "confirm", src, build, run_dir, ffmpeg,
                                          writable_cache=False, extra_env={"CONFIRM_TESTS": pattern, "CONFIRM_REPEAT": 2})
                M_DURATION.labels("test").set(seconds)
                if oom or code == "timeout":
                    status, infra = "infra", f"test container {'ran out of memory' if oom else 'timed out'}"
                elif baselines.events[base_key].is_set():
                    paths = test_paths(main_sha, head)
                    failing = report.new_failures(report.load_run(run_dir), report.load_run(BASELINES / base_key),
                                                  main_unstable(state))
                    if paths and failing:
                        set_stage(key, "compare_main")
                        compare_on_main(number, main_sha, head, run_dir, paths, failing)
        set_stage(key, "report")
        result = report.load_run(run_dir) if status == "ok" else None
        base = report.load_run(baselines.wait(base_key)) if status == "ok" else None
        context = {"repo": CFG["repo"], "number": number, "head": head, "main": main_sha,
                   "status": status, "marker": CFG["marker"], "host": host_info(),
                   "finished": datetime.now(timezone.utc), "duration": time.time() - started,
                   "validation_tests": CFG["validation_tests"], "main_unstable": main_unstable(state),
                   "test_paths": test_paths(main_sha, head) if status == "ok" else [],
                   "settings": {"gpu_parallel": CFG["test_parallel"], "cpu_parallel": CFG["cpu_test_parallel"]}}
        with STATE_LOCK:
            context["flaky_seen"] = flaky_seen(state, time.time())
        if CFG["post"] and CFG["gist_logs"] and status == "ok":
            try:
                context["logs_url"] = gh.create_gist(
                    f"AnyPS5 NVIDIA GPU test logs: {CFG['repo']}#{number}, head {head[:12]} onto main {main_sha[:12]}",
                    report.log_files(run_dir))
            except (urllib.error.URLError, OSError) as error:
                M_ERRORS.labels("gist").inc()
                log(f"PR #{number}: log upload failed: {error}")
        outcome, body, summary = report.render(context, result, base)
        (run_dir / "report.md").write_text(body)
        if infra:
            (run_dir / "infra.txt").write_text(infra)
        with STATE_LOCK:
            previous = state["prs"].get(key, {})
        attempts = previous.get("attempts", 0) + 1 if outcome == "infra-error" and previous.get("head") == head else 1
        entry = {"head": head, "main": main_sha, "outcome": outcome, "finished": time.time(),
                 "title": pr["title"], "run_dir": str(run_dir), "attempts": attempts, **summary}
        if infra:
            entry["infra"] = infra
        M_RUNS.labels(outcome).inc()
        M_DURATION.labels("pr").set(time.time() - started)
        if CFG["post"] and outcome != "infra-error":
            try:
                entry["comment"] = gh.post_comment(number, head, body, CFG["marker"])
            except urllib.error.HTTPError as error:
                M_ERRORS.labels("post").inc()
                log(f"PR #{number}: posting failed: {error.code} {error.read()[:300]!r}")
        with STATE_LOCK:
            state["prs"][key] = entry
            if result:
                record_flaky(state, result["flaky"], time.time())
            save_state(state)
        publish_result(pr, entry)
        for old in sorted((RUNS / f"pr-{number}").iterdir(), key=lambda p: p.stat().st_mtime)[:-3]:
            shutil.rmtree(old, ignore_errors=True)
        log(f"PR #{number}: {outcome}{' (' + infra + ')' if infra else ''} in {time.time() - started:.0f}s")
    except Exception:
        M_ERRORS.labels("job").inc()
        log(f"PR #{number}: job failed:\n" + traceback.format_exc())
    finally:
        shutil.rmtree(tree, ignore_errors=True)
        baselines.release(base_key)
        set_stage(key, None)


class Scheduler:
    def __init__(self, state, gh):
        self.state, self.gh = state, gh
        self.baselines = Baselines(state)
        self.workers = CFG["max_builds"] + CFG["max_gpu"]
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="job")
        self.in_flight = {}
        self.prs, self.candidates, self.main_sha, self.newest_main, self.refreshed = [], [], None, None, 0
        self.checkpoints = {}
        self.newer_base = {}

    def track_tests(self):
        seen = {}
        for path in TREES.glob("*/build/Testing/Temporary/CTestCheckpoint.txt"):
            try:
                lines = path.read_bytes().count(b"\n")
            except OSError:
                continue
            last = self.checkpoints.get(path, 0)
            M_TESTS_DONE.inc(lines - last if lines >= last else lines)
            seen[path] = lines
        self.checkpoints = seen

    def refresh(self):
        self.newest_main = fetch_mirror()
        self.main_sha = self.baselines.target(self.newest_main)
        self.prs = self.gh.open_prs()
        self.refreshed = time.time()
        M_OPEN.set(len(self.prs))
        open_numbers = {str(pr["number"]) for pr in self.prs}
        with STATE_LOCK:
            for number in list(self.state["prs"]):
                if number not in open_numbers and int(number) not in self.in_flight:
                    self.state["prs"].pop(number)
                    for labels in list(M_RESULT._metrics):
                        if labels[0] == number:
                            M_RESULT.remove(*labels)
        self.candidates = [pr for pr in self.prs if eligible(pr, self.main_sha)]
        M_ELIGIBLE.set(len(self.candidates))
        for pr in self.candidates:
            entry = self.state["prs"].get(str(pr["number"]))
            if entry:
                publish_result(pr, entry)
        M_CYCLE.set(time.time())

    def based_on_newer(self, head, main_sha):
        key = (head, main_sha)
        if key not in self.newer_base:
            self.newer_base[key] = based_on_newer_main(main_sha, self.newest_main, head)
        return self.newer_base[key]

    def waits_for_baseline(self, pr):
        return self.based_on_newer(pr["head"]["sha"], self.main_sha)

    def stale_result(self, entry):
        if not entry or entry.get("outcome") in ("merge-conflict", "infra-error"):
            return False
        return self.based_on_newer(entry["head"], entry["main"])

    def queue(self):
        with STATE_LOCK:
            entries = {pr["number"]: self.state["prs"].get(str(pr["number"])) for pr in self.candidates}
        pending = [pr for pr in self.candidates if pr["number"] not in self.in_flight
                   and (needs_run(entries[pr["number"]], pr["head"]["sha"]) or self.stale_result(entries[pr["number"]]))]
        ready = [pr for pr in pending if not self.waits_for_baseline(pr)]
        M_WAITING.set(len(pending) - len(ready))
        return ready

    def submit(self):
        for number, future in list(self.in_flight.items()):
            if future.done():
                del self.in_flight[number]
        queue = self.queue()
        free = self.workers - len(self.in_flight)
        for pr in queue[:max(0, free)]:
            self.in_flight[pr["number"]] = self.pool.submit(run_pr, self.state, self.gh, self.baselines, pr, self.main_sha)
        M_QUEUE.set(max(0, len(queue) - max(0, free)))
        return len(queue)

    def loop(self):
        log(f"start: repo {CFG['repo']}, filter {CFG['filter']}, builds {CFG['max_builds']} x {CFG['build_cpus']} cpus, "
            f"gpu runs {CFG['max_gpu']}, posting {'ON' if CFG['post'] else 'off (dry run)'}")
        while not STOP.is_set():
            try:
                if time.time() - self.refreshed >= CFG["interval"] or not self.refreshed:
                    self.refresh()
                    log(f"refresh: newest main {self.newest_main[:12]}, testing onto {self.main_sha[:12]}, "
                        f"{len(self.prs)} open, {len(self.candidates)} eligible, "
                        f"{len(self.queue())} need a run, {len(self.in_flight)} running")
                elif self.newest_main:
                    target = self.baselines.target(self.newest_main)
                    if target != self.main_sha:
                        log(f"baseline ready: now testing onto main {target[:12]}")
                        self.main_sha = target
                self.submit()
                self.track_tests()
            except Exception:
                M_ERRORS.labels("cycle").inc()
                log("refresh failed:\n" + traceback.format_exc())
                self.refreshed = time.time()
            if CFG["once"] and self.refreshed:
                for future in list(self.in_flight.values()):
                    future.result()
                break
            STOP.wait(30)
        log("stopping: waiting for running jobs")
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.baselines.pool.shutdown(wait=True, cancel_futures=True)


def cleanup_leftovers():
    names = run(["docker", "ps", "-aq", "--filter", "label=anyps5-gpu-ci=runner"], check=False).stdout.split()
    if names:
        run(["docker", "rm", "-f", *names], check=False)
    for tree in TREES.glob("pr-*"):
        shutil.rmtree(tree, ignore_errors=True)
    for cache in RUNS.glob("pr-*/.ccache-*"):
        shutil.rmtree(cache, ignore_errors=True)


def main():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    for path in (TREES, BASELINES, RUNS, FFMPEG, CCACHE):
        path.mkdir(parents=True, exist_ok=True)
    start_http_server(9200)
    M_POSTING.set(1 if CFG["post"] else 0)
    M_LIMIT.labels("build").set(CFG["max_builds"])
    M_LIMIT.labels("gpu").set(CFG["max_gpu"])
    M_TESTS_RUNNING.set(CFG["max_gpu"] * CFG["test_parallel"])
    for stage in STAGES:
        M_STAGE.labels(stage).set(0)
    gh = GitHub(CFG["token"], CFG["repo"])
    if CFG["post"] and not CFG["token"]:
        log("POST_COMMENTS=true needs GITHUB_TOKEN; staying in dry-run")
        CFG["post"] = False
    cleanup_leftovers()
    state = load_state()
    seed_main_history(state)
    save_state(state)
    Scheduler(state, gh).loop()


if __name__ == "__main__":
    sys.exit(main())
