#!/bin/bash

# -----------------------------------------------------------------------------
# BENCHMARK LIST
# -----------------------------------------------------------------------------
BENCHMARKS=(
  "polybench_datamining_correlation"
  "polybench_datamining_covariance"
  "polybench_linear_algebra_blas_gemm"
  "polybench_linear_algebra_blas_gemver"
  "polybench_linear_algebra_blas_gesummv"
  "polybench_linear_algebra_blas_symm"
  "polybench_linear_algebra_blas_syr2k"
  "polybench_linear_algebra_blas_syrk"
  "polybench_linear_algebra_blas_trmm"
  "polybench_linear_algebra_kernels_2mm"
  "polybench_linear_algebra_kernels_3mm"
  "polybench_linear_algebra_kernels_atax"
  "polybench_linear_algebra_kernels_bicg"
  "polybench_linear_algebra_kernels_doitgen"
  "polybench_linear_algebra_kernels_mvt"
  "polybench_linear_algebra_solvers_cholesky"
  "polybench_linear_algebra_solvers_durbin"
  "polybench_linear_algebra_solvers_gramschmidt"
  "polybench_linear_algebra_solvers_lu"
  "polybench_linear_algebra_solvers_ludcmp"
  "polybench_linear_algebra_solvers_trisolv"
  "polybench_medley_deriche"
  "polybench_medley_floyd_warshall"
  "polybench_medley_nussinov"
  "polybench_stencils_adi"
  "polybench_stencils_fdtd_2d"
  "polybench_stencils_heat_3d"
  "polybench_stencils_jacobi_1d"
  "polybench_stencils_jacobi_2d"
  "polybench_stencils_seidel_2d"
)

# -----------------------------------------------------------------------------
# ROOT CHECK
# -----------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then 
    echo "Error: Please run as root (use 'su' before executing)."
    exit 1
fi

OS_TYPE="$(uname -s)"

# -----------------------------------------------------------------------------
# CPU POWER MANAGEMENT (SETUP)
# On Linux: Uses cpupower to set performance governor.
# On macOS: Uses pmset to disable frequency throttling and background tasks.
# -----------------------------------------------------------------------------
echo "--- Optimizing CPU for Performance ---"
if [[ "$OS_TYPE" == "Linux"* ]]; then
    # Set governor to performance for all cores
    cpupower frequency-set -g performance
elif [[ "$OS_TYPE" == "Darwin"* ]]; then
    # Disable sleep, background disk tasks, and prioritize performance
    # Note: macOS doesn't have a direct 'governor' equivalent like Linux, 
    # but we can prevent the system from entering low power states.
    pmset -a throttlestop 1 2>/dev/null || echo "Info: throttlestop not supported on this Mac."
    pmset -a sleep 0 displaysleep 0 disksleep 0
fi

# -----------------------------------------------------------------------------
# WRAPPER SELECTION
# -----------------------------------------------------------------------------
get_wrapper() {
    case "$OS_TYPE" in
        Linux*)
            echo "nice -n -20 chrt -f 1 taskset -c 0"
            ;;
        Darwin*)
            echo "caffeinate -is nice -n -20 taskpolicy -c utility"
            ;;
        *)
            echo ""
            ;;
    esac
}

WRAPPER=$(get_wrapper)

echo "--- Starting benchmark session as Root on $OS_TYPE ---"

# -----------------------------------------------------------------------------
# EXECUTION LOOP
# -----------------------------------------------------------------------------
for i in "${!BENCHMARKS[@]}"; do
    BENC="${BENCHMARKS[$i]}"
    LOG_FILE="./${BENC}.log"
    
    echo "[$(($i + 1))/${#BENCHMARKS[@]}] Running: $BENC"
    
    # Run the orchestrator script
    eval $WRAPPER ./scripts/x.sh --run-id "$BENC" --benchmarks "$BENC" 2>&1 | tee "$LOG_FILE"
    
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        echo "      ✅ Successfully finished: $BENC"
    else
        echo "      ❌ Error: $BENC failed."
    fi
    echo "----------------------------------------------------"
done

# -----------------------------------------------------------------------------
# CPU POWER MANAGEMENT (RESTORE)
# -----------------------------------------------------------------------------
echo "--- Restoring CPU Default Settings ---"
if [[ "$OS_TYPE" == "Linux"* ]]; then
    cpupower frequency-set -g powersave # Change to 'ondemand' if preferred
elif [[ "$OS_TYPE" == "Darwin"* ]]; then
    pmset -a sleep 10 displaysleep 10 disksleep 10
fi

echo "--- All benchmarks finished ---"