#!/usr/bin/env bash
set -euo pipefail

# llvm-perf/scripts/prepare_repos.sh
#
# Add repository setup command to clone suites and auto-generate benchmark snippets.
#
# What this script does:
# 1) Clones/updates PolyBench and LLVM test-suite into ./benchmarks/
# 2) Runs the Python generator to emit ready-to-paste [[benchmarks]] TOML blocks
#
# Usage:
#   ./scripts/prepare_repos.sh
#   ./scripts/prepare_repos.sh --only polybench
#   ./scripts/prepare_repos.sh --only llvm-test-suite
#   ./scripts/prepare_repos.sh --no-clone
#   ./scripts/prepare_repos.sh --max-polybench 40
#   ./scripts/prepare_repos.sh --output configs/generated_benchmarks.toml
#
#   # Forward extra args to setup_benchmarks.py after '--'
#   ./scripts/prepare_repos.sh -- --sort
#
# Notes:
# - Requires python3 + git
# - Must be executed from project root or anywhere inside project tree

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ONLY="all"
NO_CLONE=0
MAX_POLYBENCH="25"
POLYBENCH_ROOT="benchmarks/polybench"
LLVM_TS_ROOT="benchmarks/llvm-test-suite"
OUTPUT="configs/generated_benchmarks.toml"
EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./scripts/prepare_repos.sh [options] [-- <extra args for setup_benchmarks.py>]

Options:
  --only <name>                 One of: all | polybench | llvm-test-suite (default: all)
  --no-clone                    Do not clone/fetch repos; inspect local trees only
  --max-polybench <N>           Max number of PolyBench kernels to include (default: 25)
  --polybench-root <path>       PolyBench destination path (default: benchmarks/polybench)
  --llvm-test-suite-root <path> LLVM test-suite destination path (default: benchmarks/llvm-test-suite)
  --output <path>               Output TOML snippet file (default: configs/generated_benchmarks.toml)
  -h, --help                    Show this help

Examples:
  ./scripts/prepare_repos.sh
  ./scripts/prepare_repos.sh --only polybench --max-polybench 50
  ./scripts/prepare_repos.sh --only llvm-test-suite
  ./scripts/prepare_repos.sh --no-clone --output /tmp/bench_snippets.toml
  ./scripts/prepare_repos.sh -- --sort
EOF
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      ONLY="${2:-}"
      shift 2
      ;;
    --no-clone)
      NO_CLONE=1
      shift
      ;;
    --max-polybench)
      MAX_POLYBENCH="${2:-}"
      shift 2
      ;;
    --polybench-root)
      POLYBENCH_ROOT="${2:-}"
      shift 2
      ;;
    --llvm-test-suite-root)
      LLVM_TS_ROOT="${2:-}"
      shift 2
      ;;
    --output)
      OUTPUT="${2:-}"
      shift 2
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
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! have_cmd python3; then
  echo "[ERROR] python3 is required but was not found in PATH." >&2
  exit 1
fi

if ! have_cmd git; then
  echo "[ERROR] git is required but was not found in PATH." >&2
  exit 1
fi

if [[ ! -f "scripts/setup_benchmarks.py" ]]; then
  echo "[ERROR] scripts/setup_benchmarks.py not found." >&2
  exit 1
fi

if [[ "$ONLY" != "all" && "$ONLY" != "polybench" && "$ONLY" != "llvm-test-suite" ]]; then
  echo "[ERROR] Invalid --only value: $ONLY" >&2
  echo "        Allowed values: all | polybench | llvm-test-suite" >&2
  exit 1
fi

if ! [[ "$MAX_POLYBENCH" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] --max-polybench must be a non-negative integer." >&2
  exit 1
fi

echo "[INFO] root_dir=$ROOT_DIR"
echo "[INFO] only=$ONLY"
echo "[INFO] no_clone=$NO_CLONE"
echo "[INFO] max_polybench=$MAX_POLYBENCH"
echo "[INFO] polybench_root=$POLYBENCH_ROOT"
echo "[INFO] llvm_test_suite_root=$LLVM_TS_ROOT"
echo "[INFO] output=$OUTPUT"

CMD=(
  python3 scripts/setup_benchmarks.py
  --only "$ONLY"
  --polybench-root "$POLYBENCH_ROOT"
  --llvm-test-suite-root "$LLVM_TS_ROOT"
  --max-polybench "$MAX_POLYBENCH"
  --output "$OUTPUT"
)

if [[ "$NO_CLONE" -eq 1 ]]; then
  CMD+=(--no-clone)
fi

if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
  CMD+=("${EXTRA_ARGS[@]}")
fi

echo "[INFO] Running:"
printf '  %q' "${CMD[@]}"
echo

"${CMD[@]}"

echo "[INFO] Done."
echo "[INFO] Generated benchmark snippet file:"
echo "  $OUTPUT"
echo
echo "[INFO] Next step:"
echo "  Review $OUTPUT and merge desired [[benchmarks]] entries into configs/benchmarks.toml"
