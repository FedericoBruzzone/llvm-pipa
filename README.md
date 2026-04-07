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
   - `O1_custom_<N>` (by default, composite LLVM passes are recursively expanded into incremental sub-variants)
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
./scripts/x.sh --quick -- --no-recursive-expansion
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
- `--no-recursive-expansion`: disable recursive expansion of composite LLVM passes and use the original top-level pass list
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


## Detailed Metric Specification

This table provides the exhaustive list of the 104 metrics collected and calculated by our evaluation framework for each optimization stage.

|Category | Metric Names | Count |
|---|---|---|
|Absolute Execution |	runtime_mean_seconds, runtime_median_seconds, runtime_stddev_seconds, runtime_ci95_lower, runtime_ci95_upper | 5
|Compilation & Size |	compile_time_wall_seconds, opt_wall_seconds, binary_size_bytes, text_section_size | 4
|IR Structural	| ir_instruction_count, ir_basic_block_count, ir_phi_node_count, ir_globals_count, ir_call_site_count | 5
|Hardware Telemetry |	profile_ir, profile_instructions, profile_cycles, profile_ipc, profile_cpi, profile_drefs, profile_d1_misses, profile_ll_misses, profile_branch_misses, profile_branch_miss_rate, profile_cache_references, profile_stalls_frontend, profile_stalls_backend, profile_l1_i_misses, profile_max_rss, profile_page_faults, profile_context_switches | 17
|Cumulative Delta (vs. -O0) |	delta_vs_O0_runtime_mean_seconds, delta_vs_O0_compile_time_wall_seconds, delta_vs_O0_binary_size_bytes, delta_vs_O0_ir_instruction_count, delta_vs_O0_ir_phi_node_count, delta_vs_O0_ir_globals_count, delta_vs_O0_ir_call_site_count, delta_vs_O0_text_section_size, delta_vs_O0_profile_ir, delta_vs_O0_profile_d1_misses, delta_vs_O0_profile_ll_misses, delta_vs_O0_profile_instructions, delta_vs_O0_profile_cycles, delta_vs_O0_profile_ipc, delta_vs_O0_profile_cpi, delta_vs_O0_profile_branch_misses, delta_vs_O0_profile_cache_references, delta_vs_O0_profile_stalls_frontend, delta_vs_O0_profile_stalls_backend, delta_vs_O0_profile_l1_i_misses, delta_vs_O0_profile_max_rss, delta_vs_O0_profile_page_faults, delta_vs_O0_profile_context_switches, speedup_vs_O0, effect_size_vs_O0, pvalue_vs_O0 | 26
|Incremental Delta (vs. Prev) |	delta_prev_runtime_mean_seconds, delta_prev_compile_time_wall_seconds, delta_prev_binary_size_bytes, delta_prev_ir_instruction_count, delta_prev_ir_phi_node_count, delta_prev_ir_globals_count, delta_prev_ir_call_site_count, delta_prev_text_section_size, delta_prev_profile_ir, delta_prev_profile_d1_misses, delta_prev_profile_ll_misses, delta_prev_profile_instructions, delta_prev_profile_cycles, delta_prev_profile_ipc, delta_prev_profile_cpi, delta_prev_profile_branch_misses, delta_prev_profile_cache_references, delta_prev_profile_stalls_frontend, delta_prev_profile_stalls_backend, delta_prev_profile_l1_i_misses, delta_prev_profile_max_rss, delta_prev_profile_page_faults, delta_prev_profile_context_switches, speedup_vs_prev, effect_size_vs_prev, pvalue_vs_prev | 26
|Tracking Metadata |	benchmark_id, variant, variant_limit, prev_variant, timestamp | 5
|Inter-state Energy |	(Reserved for future energy/power delta analysis placeholders) |	16
|TOTAL METRICS | |		104

## License

This repository is licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE][github-license-apache] or http://www.apache.org/licenses/LICENSE-2.0)

- MIT license ([LICENSE-MIT][github-license-mit] or http://opensource.org/licenses/MIT)

at your option.

Please review the license file provided in the repository for more information regarding the terms and conditions of the license.

## Contact

If you have any questions, suggestions, or feedback, do not hesitate to [contact me](https://federicobruzzone.github.io/).