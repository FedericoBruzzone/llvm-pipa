# LLVM Pass Incremental Performance Analysis (`llvm-pipa`)

`llvm-pipa` is a framework to run an incremental study of LLVM optimization passes.

The implemented methodology is:

1. Build a baseline at `O0`
2. Build progressive `O1`
3. Measure deltas:
   - versus baseline (`Pn - O0`)
   - versus previous variant (`Pn - Pn-1`)

## Why this project exists

The goal is to isolate incremental pass impact on:

- runtime performance
- compile-time cost
- binary size
- hardware performance counters (`perf stat` on Linux, `xctrace` on macOS)

This is designed for reproducible empirical analysis and paper-grade data collection.

## Single source of configuration

This project uses one config file only:

- `configs/benchmarks.toml`

It contains:

- experiment setup
- LLVM tool settings
- pass variant policy
- benchmark list
- tool toggles
- output schema

## Core pipeline (`scripts/orchestrator.py`)

For each enabled benchmark, the orchestrator:

1. Emits LLVM IR from source
2. Discovers pass sequence with `opt --print-pipeline-passes -O1 -S /dev/null`
3. Generates variants:
   - `O0`
   - `O1_custom_<N>`
4. Compiles each variant
5. Measures runtime:
   - `hyperfine` only
6. Runs profiling if available:
   - `perf stat` on Linux (cycles, instructions, cache/branch misses)
   - `xctrace` on macOS (CPU Counters)
7. Exports CSV + JSON + markdown summary

## Working locally

Use `scripts/x.sh` for the normal local workflow. It wraps the orchestrator with sane defaults, creates/activates `.venv`, installs requirements, and runs the experiment with the selected config.

Although it is designed for local usage, it can also be used in Docker if desired. For Docker usage and examples, see [docker/DOCKER_README.md](docker/DOCKER_README.md).

Quick local smoke test:

```bash
./scripts/x.sh --quick
```

Examples:

```bash
./scripts/x.sh --runs 10 --warmup 2
./scripts/x.sh --benchmarks micro_sum_loop,micro_branchy --step 10 --max-limit 120
./scripts/x.sh --quick -- --disable-profiler
```

`x.sh` supports these options directly because it is a wrapper around the orchestrator designed for local workflow convenience.

When the orchestrator runs with the default config, it uses:

- `--runs` = 30
- `--warmup` = 5
- `--step` = 1
- `--max-limit` = none (no clamp)

The `--quick` wrapper mode overrides these defaults to:

- `--runs` = 3
- `--warmup` = 1
- `--step` = 25
- `--max-limit` = 50

Passing common orchestrator-style flags before `--` allows the wrapper to apply them itself, while still letting you forward additional raw orchestrator args after `--`.

- `--config <path>`: Config TOML path (default: `configs/benchmarks.toml`)
- `--runs <N>`: Override measurement runs
- `--warmup <N>`: Override warmup runs
- `--step <N>`: Bisect step
- `--max-limit <N>`: Clamp max bisect limit
- `--benchmarks <ids>`: Comma-separated benchmark IDs
- `--quick`: Very fast smoke mode (runs=3, warmup=1, step=25, max-limit=50)
- `--no-venv`: Use system Python instead of `.venv`
- `--no-install`: Skip dependency installation
- `--`: Forward remaining args to `scripts/orchestrator.py`

This means `./scripts/x.sh --runs 10` is a valid local command, while `./scripts/x.sh --quick -- --disable-profiler` forwards only the extra orchestrator-only argument `--disable-profiler`.
For the case `./scripts/x.sh --runs 10 -- --runs 100`, the wrapper still parses `--runs 10`, but the orchestrator receives both `--runs 10` and `--runs 100`; because duplicated options are allowed, the last value usually wins, so `100` will be used. This is not recommended because it creates ambiguity.

If you want finer-grained control, run the orchestrator directly:

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

Important orchestrator flags:

- `--config <path>`: Config TOML path
- `--runs <N>`: number of measurement runs per benchmark
- `--warmup <N>`: number of warmup runs before timing
- `--step <N>`: bisect step size for variant generation
- `--max-limit <N>`: clamp the maximum variant limit
- `--explicit-limits 0,5,10,20`: run only the listed variant limits
- `--benchmarks id1,id2`: select one or more benchmark IDs
- `--disable-profiler`: skip profiling completely
- `--no-o0`: omit the O0 baseline variant
- `--no-full-o1`: omit the full O1 variant
- `--fail-fast`: stop on first error
- `--no-randomize`: keep benchmark order deterministic
- `--seed <N>`: fix the randomization seed for repeatability

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

