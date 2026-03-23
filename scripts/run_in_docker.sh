#!/usr/bin/env bash
set -euo pipefail

# llvm-perf/scripts/run_in_docker.sh
# Build and run the project using the repository Dockerfile, then execute the orchestrator.
#
# Usage:
#   ./scripts/run_in_docker.sh
#   ./scripts/run_in_docker.sh --config configs/benchmarks.toml
#   ./scripts/run_in_docker.sh -- --runs 10 --warmup 2 --step 10
#   ./scripts/run_in_docker.sh --build-only
#   ./scripts/run_in_docker.sh --run-only
#
# Notes:
# - Uses Dockerfile in repo root (no temporary Dockerfile generation).
# - Forwards all args after '--' directly to scripts/orchestrator.py.
# - Mounts the current repo in /workspace so outputs are written to your local tree.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="configs/benchmarks.toml"
IMAGE_NAME="llvm-perf:latest"
CONTAINER_NAME="llvm-perf-runner"
MOUNT_PATH="/workspace"

BUILD_ONLY=0
RUN_ONLY=0
NO_CACHE=0
PULL=0
NO_REQUIREMENTS=0

DOCKER_BUILD_ARGS=()
DOCKER_RUN_ARGS=()
ORCH_EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./scripts/run_in_docker.sh [options] [-- <orchestrator args>]

Options:
  --config <path>               Config path passed to orchestrator (default: configs/benchmarks.toml)
  --image-name <name>           Docker image tag (default: llvm-perf:latest)
  --container-name <name>       Docker container name (default: llvm-perf-runner)
  --mount-path <path>           In-container mount path (default: /workspace)

  --build-only                  Build image only, do not run
  --run-only                    Run container only, do not build
  --no-cache                    Build with --no-cache
  --pull                        Build with --pull
  --no-requirements             Skip pip install -r requirements.txt in container

  --docker-build-arg <arg>      Extra arg forwarded to docker build (repeatable)
  --docker-run-arg <arg>        Extra arg forwarded to docker run (repeatable)

  -h, --help                    Show this help

Examples:
  ./scripts/run_in_docker.sh
  ./scripts/run_in_docker.sh --config configs/benchmarks.toml -- --runs 10 --warmup 2
  ./scripts/run_in_docker.sh --build-only
  ./scripts/run_in_docker.sh --run-only --docker-run-arg "--cpus=4"
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
    --image-name)
      IMAGE_NAME="${2:-}"
      shift 2
      ;;
    --container-name)
      CONTAINER_NAME="${2:-}"
      shift 2
      ;;
    --mount-path)
      MOUNT_PATH="${2:-}"
      shift 2
      ;;
    --build-only)
      BUILD_ONLY=1
      shift
      ;;
    --run-only)
      RUN_ONLY=1
      shift
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --pull)
      PULL=1
      shift
      ;;
    --no-requirements)
      NO_REQUIREMENTS=1
      shift
      ;;
    --docker-build-arg)
      DOCKER_BUILD_ARGS+=("${2:-}")
      shift 2
      ;;
    --docker-run-arg)
      DOCKER_RUN_ARGS+=("${2:-}")
      shift 2
      ;;
    --)
      shift
      ORCH_EXTRA_ARGS+=("$@")
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

if [[ "$BUILD_ONLY" -eq 1 && "$RUN_ONLY" -eq 1 ]]; then
  echo "[ERROR] --build-only and --run-only cannot be used together." >&2
  exit 1
fi

if ! have_cmd docker; then
  echo "[ERROR] docker is required but not found in PATH." >&2
  echo "[HINT] Install Docker Desktop (macOS/Windows) or Docker Engine (Linux)." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "[ERROR] Docker daemon is not reachable." >&2
  echo "[HINT] Start Docker Desktop (or dockerd) and wait until it is fully ready." >&2
  echo "[HINT] Then retry: ./scripts/run_in_docker.sh -- --runs 3 --warmup 1" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/Dockerfile" ]]; then
  echo "[ERROR] Dockerfile not found at repository root." >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "[ERROR] Config file not found: $CONFIG_PATH" >&2
  exit 1
fi

build_image() {
  local args=()
  [[ "$NO_CACHE" -eq 1 ]] && args+=(--no-cache)
  [[ "$PULL" -eq 1 ]] && args+=(--pull)
  if [[ "${#DOCKER_BUILD_ARGS[@]}" -gt 0 ]]; then
    args+=("${DOCKER_BUILD_ARGS[@]}")
  fi

  echo "[INFO] Building image '$IMAGE_NAME' using ./Dockerfile"
  docker build ${args+"${args[@]}"} -t "$IMAGE_NAME" -f "$ROOT_DIR/Dockerfile" "$ROOT_DIR"
  echo "[INFO] Build completed."
}

run_container() {
  local req_step=""
  if [[ "$NO_REQUIREMENTS" -eq 0 ]]; then
    req_step='if [ -f requirements.txt ]; then python3 -m pip install --no-cache-dir -r requirements.txt; fi;'
  fi

  local orch_args_shell=""
  if [[ "${#ORCH_EXTRA_ARGS[@]}" -gt 0 ]]; then
    local a
    for a in "${ORCH_EXTRA_ARGS[@]}"; do
      orch_args_shell+=" $(printf '%q' "$a")"
    done
  fi

  local inner_cmd
  inner_cmd="
set -euo pipefail
echo '[INFO] Container started in:' \$(pwd)
python3 --version
clang --version || true
opt --version || true
${req_step}
python3 scripts/orchestrator.py --config $(printf '%q' "$CONFIG_PATH")${orch_args_shell}
"

  echo "[INFO] Running container '$CONTAINER_NAME'"
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --privileged \
    -v "$ROOT_DIR:$MOUNT_PATH" \
    -w "$MOUNT_PATH" \
    ${DOCKER_RUN_ARGS+"${DOCKER_RUN_ARGS[@]}"} \
    "$IMAGE_NAME" \
    bash -lc "$inner_cmd"

  echo "[INFO] Run completed."
}

echo "[INFO] root_dir=$ROOT_DIR"
echo "[INFO] config=$CONFIG_PATH"
echo "[INFO] image=$IMAGE_NAME"
echo "[INFO] container=$CONTAINER_NAME"
echo "[INFO] mount_path=$MOUNT_PATH"

if [[ "$RUN_ONLY" -eq 0 ]]; then
  build_image
fi

if [[ "$BUILD_ONLY" -eq 0 ]]; then
  run_container
fi

echo "[INFO] Done."
