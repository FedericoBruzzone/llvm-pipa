# Notes

## TODO

- Use the correct pipeline (nested passes for function-level, module-level, etc.) 

## Docker-based workflow

**Step 1: Start Colima**: `/opt/homebrew/opt/colima/bin/colima start`

**Step 2: Build and run in Docker**: `./scripts/run_in_docker.sh --build-only`

**Step 3: Run benchmarks**: `./scripts/run_in_docker.sh --run-only --no-requirements -- --step 50`

**To run a bash shell in the container**: `docker run --rm -it -v "$(pwd):/workspace" -w /workspace llvm-perf:latest bash`


./scripts/run_in_docker.sh --run-only --no-requirements -- --step 5 --warmup 0 --runs 2   
## Local-based workflow (macOS)

**Step 1: Install dependencies**:

`brew install hyperfine`

Xcode Command Line Tools are required for `xctrace` profiling (hardware performance counters):

`xcode-select --install`

**Step 2: Run benchmarks** (profiler auto-detects: `xctrace` on macOS, `perf` on Linux):

`./scripts/run_local.sh --step 10 --max-limit 100`

Override profiler backend manually:

`./scripts/run_local.sh --step 10 -- --profiler xctrace`
`./scripts/run_local.sh --step 10 -- --profiler none`


## Local-based workflow (Linux)

**Step 1: Install dependencies**:

```bash
sudo apt install hyperfine linux-tools-common linux-tools-generic
# or: sudo apt install linux-tools-$(uname -r)
```

**Step 2: Run benchmarks** (`perf stat` auto-selected on Linux):

`./scripts/run_local.sh --step 10 --max-limit 100`

