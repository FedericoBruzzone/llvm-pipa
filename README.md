# LLVM Pass Incremental Performance Analysis (`llvm-pipa`)

`llvm-pipa` is a framework to run an incremental study of LLVM optimization passes.

The implemented methodology is:

1. Build a baseline at `O0`
2. Build progressive `O1`
3. Measure deltas:
   - versus baseline (`Pn - O0`)
   - versus previous variant (`Pn - Pn-1`)

---

## Why this project exists

The goal is to isolate incremental pass impact on:

- runtime performance
- compile-time cost
- binary size
- hardware performance counters (`perf stat` on Linux, `xctrace` on macOS)

This is designed for reproducible empirical analysis and paper-grade data collection.

---

## Single source of configuration: one TOML

This project uses one config file only:

- `configs/benchmarks.toml`

It contains:

- experiment setup
- LLVM tool settings
- pass variant policy
- benchmark list
- tool toggles
- output schema

---

## Core pipeline (`scripts/orchestrator.py`)

For each enabled benchmark, the orchestrator:

1. Emits LLVM IR from source
2. Discovers pass sequence with `opt --print-pipeline-passes -O1 -S /dev/null`
3. Generates variants:
   - `O0`
   - `O1_bisect_<N>`
4. Compiles each variant
5. Measures runtime:
   - `hyperfine` if available
   - internal timing fallback otherwise
6. Runs profiling if available:
   - `perf stat` on Linux (cycles, instructions, cache/branch misses)
   - `xctrace` on macOS (CPU Counters)
7. Exports CSV + JSON + markdown summary

---

## Docker-first usage

### 1) Run in Docker

```bash
./scripts/run_in_docker.sh
```

This command:

- builds the image from `Dockerfile`
- mounts repo at `/workspace`
- runs the orchestrator with `configs/benchmarks.toml`
- writes outputs to local `results/` (per-run), `artifacts/`

### 2) Useful options

```bash
./scripts/run_in_docker.sh --build-only
./scripts/run_in_docker.sh --run-only
./scripts/run_in_docker.sh --config configs/benchmarks.toml
./scripts/run_in_docker.sh -- --runs 10 --warmup 2 --step 10
./scripts/run_in_docker.sh --docker-run-arg "--cpus=4"
```

---

## PolyBench + LLVM test-suite setup workflow

### Step A: prepare benchmark repositories and generate entries

```bash
./scripts/prepare_repos.sh
```

What it does:

1. clones/updates `benchmarks/polybench`
2. clones/updates `benchmarks/llvm-test-suite`
3. generates benchmark entries in:
   - `configs/generated_benchmarks.toml`

Useful variants:

```bash
./scripts/prepare_repos.sh --only polybench
./scripts/prepare_repos.sh --only llvm-test-suite
./scripts/prepare_repos.sh --max-polybench 40
./scripts/prepare_repos.sh --no-clone -- --sort
```

### Step B: merge generated entries

Review `configs/generated_benchmarks.toml`, then merge desired `[[benchmarks]]` blocks into:

- `configs/benchmarks.toml`

### Step C: run selected suite subset

Example (small starter subset):

```bash
./scripts/run_in_docker.sh -- \
  --benchmarks polybench_datamining_correlation,llvmts_benchmarks_llvm_test_suite_singlesource_benchmarks_misc_mandel \
  --runs 10 \ 
  --warmup 2 \ 
  --step 10 
```


## Local smoke test (optional)

```bash
./scripts/run_local.sh --quick
```

Examples:

```bash
./scripts/run_local.sh --runs 10 --warmup 2
./scripts/run_local.sh --benchmarks micro_sum_loop,micro_branchy --step 10 --max-limit 120
./scripts/run_local.sh --quick -- --profiler none
```

---

## Direct orchestrator CLI

Base:

```bash
python3 scripts/orchestrator.py --config configs/benchmarks.toml
```

Example with subset:

```bash
python3 scripts/orchestrator.py \
  --config configs/benchmarks.toml \
  --benchmarks polybench_datamining_correlation,llvmts_benchmarks_llvm_test_suite_singlesource_benchmarks_misc_mandel \
  --runs 10 \
  --warmup 2 \
  --step 10
```

Important flags:

- `--runs`, `--warmup`
- `--step`
- `--max-limit`
- `--explicit-limits 0,5,10,20`
- `--benchmarks id1,id2`
- `--disable-hyperfine`
- `--profiler {auto,perf,xctrace,none}`
- `--no-o0`
- `--no-full-o1`
- `--fail-fast`
- `--no-randomize`
- `--seed <N>`

---

## Output files

Default outputs:

- `results/run_<run_id>/main_<run_id>.csv`
- `results/run_<run_id>/passes_<run_id>.csv`
- `results/run_<run_id>/compile_metrics_<run_id>.csv`
- `results/run_<run_id>/runtime_metrics_<run_id>.csv`
- `results/run_<run_id>/profile_metrics_<run_id>.csv`
- `results/run_<run_id>/errors_<run_id>.csv`
- `results/run_<run_id>/incremental_deltas_<run_id>.csv`
- `results/run_<run_id>/run_<run_id>.json`
- `results/run_<run_id>/run_<run_id>.md`
- `results/run_<run_id>/logs/` (compilation and profiling logs)


Intermediate artifacts:

- `artifacts/run_<timestamp>/...`
  - generated IR
  - binaries
  - compile logs
  - profiler outputs

---

## Key metrics columns

`main.csv` and `incremental_deltas.csv` include:

- `speedup_vs_O0`
- `delta_vs_O0_*`
- `delta_prev_*` (pure incremental delta)
- `speedup_vs_prev`
- `prev_variant`
- `variant_limit`

---

## Troubleshooting

### PolyBench clone asks for credentials
Use the setup script anyway: it includes fallback strategies and can still proceed with alternate sources when available.

### `hyperfine` missing
No hard failure: orchestrator uses internal timing fallback.

### `perf` / `xctrace` missing
No hard failure: profiling is skipped when the tool is unavailable.

### Run too long
Use:
- larger `--step`
- lower `--max-limit`
- smaller benchmark subset via `--benchmarks`

### Timeout issues
Increase:
- `--timeout-seconds`
- `--run-timeout-seconds`
- `--compile-timeout-seconds`