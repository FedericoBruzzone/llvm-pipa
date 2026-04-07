# [DOCKER] LLVM Pass Incremental Performance Analysis (`llvm-pipa`)

## Run in Docker

```bash
./scripts/run_in_docker.sh
```

This command:

- builds the image from `Dockerfile`
- mounts repo at `/workspace`
- runs the orchestrator with `configs/benchmarks.toml`
- writes outputs to local `results/` (per-run), `artifacts/`

## Useful options

```bash
./scripts/run_in_docker.sh --build-only
./scripts/run_in_docker.sh --run-only
./scripts/run_in_docker.sh --config configs/benchmarks.toml
./scripts/run_in_docker.sh -- --runs 10 --warmup 2 --step 10
./scripts/run_in_docker.sh --docker-run-arg "--cpus=4"
```

## Run selected suite subset

Example (small starter subset):

```bash
./scripts/run_in_docker.sh -- \
  --benchmarks polybench_datamining_correlation,llvmts_benchmarks_llvm_test_suite_singlesource_benchmarks_misc_mandel \
  --runs 10 \ 
  --warmup 2 \ 
  --step 10 
```


## NOTES

**Step 1: Start Colima**: `/opt/homebrew/opt/colima/bin/colima start`

**Step 2: Build and run in Docker**: `./scripts/run_in_docker.sh --build-only`

**Step 3: Run benchmarks**: `./scripts/run_in_docker.sh --run-only --no-requirements -- --step 50`

**To run a bash shell in the container**: `docker run --rm -it -v "$(pwd):/workspace" -w /workspace llvm-perf:latest bash`