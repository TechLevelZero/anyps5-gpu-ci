# AnyPS5 GPU CI

A self-hosted CI that tests open pull requests of [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5) on real
GPU hardware. Upstream CI runs the suite on lavapipe; this builds each PR merged onto `main`, runs the GPU tests on an
actual NVIDIA card with the Vulkan validation layer, compares everything against `main` on the same machine and
(when enabled) posts a report comment on the PR with the full logs in a gist.

Everything runs in Docker: a poller that schedules work, sandboxed runner containers that build and test, and
Prometheus + Grafana for monitoring. Reports are produced by plain Python (`poller/report.py`) from ctest's JUnit
output and the build logs; no LLM is involved in writing them.

## Requirements

- Linux host with Docker and the NVIDIA Container Toolkit with CDI enabled (`--device nvidia.com/gpu=all`).
- An NVIDIA GPU with a working Vulkan driver on the host.
- Plenty of CPU and disk: a PR build is about 6,400 ninja steps. The defaults in `config.env.example` were tuned on a
  160-core VM with 187 GB RAM; scale `MAX_BUILDS`, `BUILD_CPUS` and `CPU_TEST_PARALLEL` down for smaller machines.
  Allow about 150 GB of disk for the mirror, ccache, baselines and run logs.

## Setup

```sh
./install.sh            # creates config.env, fills in the host values, builds the images
$EDITOR config.env      # GITHUB_TOKEN, GRAFANA_ADMIN_PASSWORD; leave POST_COMMENTS=false at first
docker compose up -d
```

`config.env` is the only config file; every setting is documented in `config.env.example`. Start with
`POST_COMMENTS=false` (dry run): reports are written to `data/runs/` and shown in Grafana, and nothing is posted.

GitHub token: a classic token with only the `public_repo` and `gist` scopes. A fine-grained token cannot comment on a
repository owned by someone else. Without a token the dry run still works, within GitHub's 60 requests an hour.

Grafana: `http://<host>:3000`, dashboards "AnyPS5 GPU CI" and "Docker containers".

## How a run works

1. **Poll.** Every `POLL_INTERVAL_SECONDS` the poller fetches `main` and every `refs/pull/*/head` into
   `data/mirror.git` and picks the eligible PRs: open, not draft, updated within `MAX_PR_AGE_DAYS` and, with
   `PR_FILTER=gpu-paths`, changing a path under `GPU_PATHS`. Each PR head is tested once; a new push queues a new run.
2. **Baseline.** `main` is built and fully tested on the same GPU at most every `BASELINE_FULL_HOURS`, with
   incremental baselines (only the tests affected by `main`'s changes) every `BASELINE_INTERVAL_MINUTES`.
   PRs are merged onto the newest `main` that has a finished baseline. A PR whose branch is based on a newer `main`
   waits for a baseline that contains its merge base, so `main`'s own changes are never counted as the PR's.
3. **Build.** The PR is merged onto that `main` (`--no-ff`, like upstream `build.yml`). Submodules and the FFmpeg
   prebuilt are fetched by the poller; the build container then runs with no network, no capabilities, a read-only
   root and the shared ccache mounted read-only.
4. **CPU-only tests** run in the build container without a Vulkan driver. Any that fail or skip there are re-run on
   the GPU.
5. **GPU tests** (`VALIDATION_TESTS`) run once on the GPU with `ANYPS5_REQUIRE_VULKAN=1`,
   `ANYPS5_NO_SHADER_CACHE=1` and `VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation`, the same way `main` ran.
6. **Confirm.** Before anything counts against the PR:
   - failing tests are re-run alone; tests that pass on retry are listed as "passed on retry";
   - failures already on `main`, or unstable in the last 10 `main` runs, are listed but not counted;
   - new `VUID-` / `SYNC-HAZARD-` IDs are re-checked by running the test alone twice; IDs seen only once are listed as
     intermittent.
7. **Report.** The comment shows the build result, test counts, new failures with log excerpts, new validation IDs,
   slow tests and a reproduce recipe. With posting on, each tested run uploads its logs to a secret gist linked from
   the comment, and a new comment is posted for each new push (re-testing the same push edits its own comment).

Outcomes: `passed`, `tests-failed`, `validation-failed`, `build-failed`, `merge-conflict` and `infra-error`.
Infrastructure errors are never posted and are retried up to 3 times.

## Operating

| | |
|---|---|
| Follow the poller | `docker compose logs -f poller` |
| Latest reports | `data/runs/pr-<N>/<head>-<main>/report.md` |
| Run one PR now (dry run) | `docker compose run --rm -e ONLY_PR=1234 -e RUN_ONCE=true -e POST_COMMENTS=false poller` |
| Apply config changes | `docker compose up -d` |
| Rebuild images after edits | `./install.sh && docker compose up -d` |

Restarting the poller cancels the runs in progress; their PRs are queued again.

## Tuning notes

Measured on an RTX 2000 Ada with driver 595:

- The limit is not GPU load (about 3 % utilisation) but the NVIDIA kernel driver's global resource-manager lock,
  taken heavily while Vulkan devices are created and destroyed. Throughput across all GPU runs peaks at about 8
  concurrent tests (about 3.2 tests/s) and drops above that, so the defaults are `MAX_GPU_RUNS=3` and
  `TEST_PARALLEL=4`. A second NVIDIA card in the same VM shares that lock; a second machine or another vendor scales.
- Running CPU-only tests in the build container (about 1,500 tests in 40 s on 32 cores) and merging the normal and
  validation passes brought a PR run from 85–150 min to about 25–30 min, about 15 PRs an hour.
- Watch the "Tests finished per second" panel (`anyps5ci_tests_finished_total`) when changing these.

## License

MIT, see [LICENSE](LICENSE).
