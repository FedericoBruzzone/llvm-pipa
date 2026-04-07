FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

# Base OS + build tooling + profiling/runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gnupg \
    wget \
    curl \
    lsb-release \
    software-properties-common \
    build-essential \
    cmake \
    ninja-build \
    git \
    jq \
    time \
    python3 \
    python3-pip \
    python3-venv \
    linux-tools-common \
    linux-tools-generic \
    && rm -rf /var/lib/apt/lists/*

# Ensure `perf` is in PATH even when the kernel-versioned binary name differs.
# In Ubuntu containers the perf wrapper may fail because the running kernel
# doesn't match the installed linux-tools package.  We fall back to the
# highest version found under /usr/lib/linux-tools-*.
RUN if ! command -v perf >/dev/null 2>&1 || ! perf --version >/dev/null 2>&1; then \
        p="$(ls -d /usr/lib/linux-tools-*/perf 2>/dev/null | sort -V | tail -1)"; \
        [ -n "$p" ] && ln -sf "$p" /usr/local/bin/perf || true; \
    fi

# Install LLVM 20 from official apt.llvm.org repository
RUN mkdir -p /etc/apt/keyrings \
    && wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/apt.llvm.org.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/apt.llvm.org.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-20 main" \
       > /etc/apt/sources.list.d/llvm20.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       llvm-20 \
       llvm-20-dev \
       llvm-20-tools \
       llvm-20-runtime \
       clang-20 \
       lld-20 \
    && rm -rf /var/lib/apt/lists/*

# Install hyperfine (runtime benchmarking)
RUN apt-get update && apt-get install -y --no-install-recommends hyperfine \
    && rm -rf /var/lib/apt/lists/*

# Make LLVM 20 tools default in PATH
RUN update-alternatives --install /usr/bin/clang clang /usr/bin/clang-20 200 \
    && update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-20 200 \
    && update-alternatives --install /usr/bin/opt opt /usr/bin/opt-20 200 \
    && update-alternatives --install /usr/bin/llc llc /usr/bin/llc-20 200 \
    && update-alternatives --install /usr/bin/llvm-as llvm-as /usr/bin/llvm-as-20 200 \
    && update-alternatives --install /usr/bin/llvm-dis llvm-dis /usr/bin/llvm-dis-20 200 \
    && update-alternatives --install /usr/bin/FileCheck FileCheck /usr/bin/FileCheck-20 200

WORKDIR /workspace

# Python deps used by orchestration scripts
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Copy project into container
COPY . /workspace

# Default command: print tool versions and keep an interactive shell-ready entrypoint
CMD ["bash", "-lc", "echo 'LLVM toolchain:' && clang --version && opt --version && echo && echo 'Profiler/benchmark tools:' && hyperfine --version && (perf --version 2>/dev/null || echo 'perf: not available') && echo && echo 'Container ready. Run: python3 scripts/orchestrator.py --config configs/benchmarks.toml'"]
