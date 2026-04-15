#!/usr/bin/env bash
set -euo pipefail

# llvm-perf/scripts/x.sh
# Quick native smoke-test runner (outside Docker).
#
# Usage:
#   ./scripts/x.sh
#   ./scripts/x.sh --config configs/benchmarks.toml
#   ./scripts/x.sh --quick
#   ./scripts/x.sh --benchmarks micro_sum_loop,micro_branchy --step 25 --max-limit 150
#
# What it does:
# 1) checks required local tools
# 2) creates a local virtual env if missing
# 3) installs minimal Python deps
# 4) runs the orchestrator with sane smoke defaults
#
# Notes:
# - This helper is for fast local validation, not final publication-grade runs.
# - For reproducible large runs, prefer Docker / Linux host setup.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="configs/benchmarks.toml"
RUNS=""
WARMUP=""
STEP=""
MAX_LIMIT=""
BENCHMARKS=""
PROFILE_RUNS=""
PROFILE_WARMUP=""
CLEANUP_PROFILE=0
EXTRA_ARGS=()
QUICK=0
NO_VENV=0
NO_INSTALL=0
CLEAN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/x.sh [options]

Options:
  --config <path>          Config TOML path (default: configs/benchmarks.toml)
  --runs <N>               Override measurement runs
  --warmup <N>             Override warmup runs
  --step <N>               Bisect step
  --max-limit <N>          Clamp max bisect limit
  --benchmarks <ids>       Comma-separated benchmark IDs
  --quick                  Very fast smoke mode (runs=3, warmup=1, step=25, max-limit=50,
                           profile-runs=3, profile-warmup=1, cleanup-profile)
  --profile-runs <N>       Override profiling measurement runs
  --profile-warmup <N>     Override profiling warmup runs
  --cleanup-profile        Delete profiler output files after parsing
  --clean                  Remove the local virtualenv before running
  --no-venv                Use system Python instead of .venv
  --no-install             Skip dependency installation
  --                       Forward remaining args to orchestrator
  -h, --help               Show this help

Examples:
  ./scripts/x.sh --quick
  ./scripts/x.sh --runs 10 --warmup 2 --benchmarks micro_sum_loop
  ./scripts/x.sh --step 10 --max-limit 100
EOF
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="${2:-}"
      shift 2
      ;;
    --runs)
      RUNS="${2:-}"
      shift 2
      ;;
    --warmup)
      WARMUP="${2:-}"
      shift 2
      ;;
    --step)
      STEP="${2:-}"
      shift 2
      ;;
    --max-limit)
      MAX_LIMIT="${2:-}"
      shift 2
      ;;
    --benchmarks)
      BENCHMARKS="${2:-}"
      shift 2
      ;;
    --profile-runs)
      PROFILE_RUNS="${2:-}"
      shift 2
      ;;
    --profile-warmup)
      PROFILE_WARMUP="${2:-}"
      shift 2
      ;;
    --cleanup-profile)
      CLEANUP_PROFILE=1
      shift
      ;;
    --quick)
      QUICK=1
      shift
      ;;
    --clean)
      CLEAN=1
      shift
      ;;
    --no-venv)
      NO_VENV=1
      shift
      ;;
    --no-install)
      NO_INSTALL=1
      shift
      ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$CLEAN" -eq 1 ]]; then
  echo "[INFO] Cleaning local virtual environment (.venv)"
  rm -rf .venv
  echo "[INFO] Cleaning artifacts/ and results/ directories"
  rm -rf artifacts/ results/
  exit 0
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "[ERROR] Config file not found: $CONFIG_PATH" >&2
  exit 1
fi

if ! have_cmd python3; then
  echo "[ERROR] python3 is required." >&2
  exit 1
fi

if ! have_cmd clang; then
  echo "[ERROR] clang is required." >&2
  exit 1
fi

if ! have_cmd opt; then
  echo "[ERROR] opt is required." >&2
  exit 1
fi

if ! have_cmd hyperfine; then
  echo "[ERROR] hyperfine is required." >&2
  exit 1
fi

echo "[INFO] root:      $ROOT_DIR"
echo "[INFO] config:    $CONFIG_PATH"
echo "[INFO] quick:     $QUICK"
echo "[INFO] clang:     $(command -v clang)"
echo "[INFO] opt:       $(command -v opt)"
echo "[INFO] hyperfine: $(command -v hyperfine)"
echo "[INFO] perf:      $(command -v perf || echo 'not found (Linux only)')"
echo "[INFO] xctrace:   $(command -v xcrun && echo '(xcrun xctrace available)' || echo 'not found')"
echo "[INFO] platform:  $(uname -s)"

PYTHON_BIN="python3"

if [[ "$NO_VENV" -eq 0 ]]; then
  if [[ ! -d ".venv" ]]; then
    echo "[INFO] Creating local virtual environment (.venv)"
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "[ERROR] Python binary not found in virtualenv." >&2
    exit 1
  fi
fi

if [[ "$NO_INSTALL" -eq 0 ]]; then
  if [[ -f "requirements.txt" ]]; then
    echo "[INFO] Installing Python dependencies"
    "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null
    "$PYTHON_BIN" -m pip install -r requirements.txt
  fi
fi

ORCH_CMD=(
  "$PYTHON_BIN" "scripts/orchestrator.py"
  "--config" "$CONFIG_PATH"
)

if [[ "$QUICK" -eq 1 ]]; then
  # Quick defaults unless explicitly overridden
  [[ -z "$RUNS" ]] && RUNS="3"
  [[ -z "$WARMUP" ]] && WARMUP="1"
  [[ -z "$STEP" ]] && STEP="25"
  [[ -z "$MAX_LIMIT" ]] && MAX_LIMIT="50"
  [[ -z "$PROFILE_RUNS" ]] && PROFILE_RUNS="3"
  [[ -z "$PROFILE_WARMUP" ]] && PROFILE_WARMUP="1"
  [[ "$CLEANUP_PROFILE" -eq 0 ]] && CLEANUP_PROFILE=1

  # For local quick smoke, we could also disable profiler entirely to speed up (and since it's less critical for quick smoke). But let's keep it on by default for better coverage, and allow overriding with -- --disable-profiler if desired.
  # ORCH_CMD+=("--disable-profiler")
fi

[[ -n "$RUNS" ]] && ORCH_CMD+=("--runs" "$RUNS")
[[ -n "$WARMUP" ]] && ORCH_CMD+=("--warmup" "$WARMUP")
[[ -n "$STEP" ]] && ORCH_CMD+=("--step" "$STEP")
[[ -n "$MAX_LIMIT" ]] && ORCH_CMD+=("--max-limit" "$MAX_LIMIT")
[[ -n "$BENCHMARKS" ]] && ORCH_CMD+=("--benchmarks" "$BENCHMARKS")
[[ -n "$PROFILE_RUNS" ]] && ORCH_CMD+=("--profile-runs" "$PROFILE_RUNS")
[[ -n "$PROFILE_WARMUP" ]] && ORCH_CMD+=("--profile-warmup" "$PROFILE_WARMUP")
[[ "$CLEANUP_PROFILE" -eq 1 ]] && ORCH_CMD+=("--cleanup-profile")

if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
  ORCH_CMD+=("${EXTRA_ARGS[@]}")
fi

echo "[INFO] Running orchestrator:"
printf '  %q' "${ORCH_CMD[@]}"
echo

set +e
"${ORCH_CMD[@]}"
EXIT_CODE=$?
set -e

echo
if [[ "$EXIT_CODE" -eq 0 ]]; then
  echo "[INFO] Local smoke test completed successfully."
else
  echo "[WARN] Orchestrator finished with exit code: $EXIT_CODE"
fi

echo "[INFO] Check outputs under:"
echo "  - results/"
echo "  - artifacts/"

exit "$EXIT_CODE"
