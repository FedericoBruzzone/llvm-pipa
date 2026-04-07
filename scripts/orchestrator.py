#!/usr/bin/env python3
"""
llvm-perf/scripts/orchestrator.py
==================================

Single-file orchestrator for **incremental LLVM pass performance analysis**.

Overview
--------
This script drives a complete experiment pipeline that measures the impact
of each LLVM optimization pass on runtime, compile cost, binary size, and
hardware performance counters.  The core idea is *incremental attribution*:
rather than comparing only O0 vs O1, the pipeline builds a series of
**prefix-truncated** optimization pipelines (O1_custom_0 … O1_custom_N)
and measures deltas between consecutive variants so that each pass's
contribution can be isolated.

Pipeline stages
---------------
1. **Emit IR** — compile the benchmark source to unoptimized LLVM IR
   (``-O0 -Xclang -disable-O0-optnone``).
2. **Discover passes** — run ``opt --print-pipeline-passes -O1`` to extract
   the full ordered pass list for the target LLVM version.
3. **Generate variants** — create ``O0`` (baseline), plus ``O1_custom_N``
   variants that apply only the first *N* passes from the discovered list.
4. **Compile** each variant — baseline via clang -O0; non-baseline via
   ``opt --passes=<truncated-pipeline>`` then clang from optimized IR.
5. **Measure runtime** — through ``hyperfine`` (preferred) or internal
   timing fallback, with configurable warmup and repetitions.
6. **Profile** — collect hardware performance counters via ``perf stat``
   on Linux or ``xctrace`` on macOS.
7. **Write outputs** — emit CSV tables (main, compile_metrics,
   runtime_metrics, profile_metrics, passes, errors, incremental_deltas),
   JSON summary, and markdown report.

Key concepts
------------
* **Variant** — a (benchmark, pass-prefix-length) pair. ``O0`` has no
  pipeline; ``O1_custom_N`` applies passes [0..N).
* **Delta** — difference for any tracked metric between two variants:
  ``delta_vs_O0`` and ``delta_prev`` (incremental step).
* **Profile backend** — abstracted behind ``_profile()``, auto-selects
  ``xctrace`` on macOS and ``perf stat`` on Linux.

Configuration
-------------
All experiment parameters come from a single TOML file (default
``configs/benchmarks.toml``), overridable via CLI flags.

Usage
-----
::

    python3 scripts/orchestrator.py --config configs/benchmarks.toml \\
        --runs 10 --warmup 2 --step 5 --benchmarks micro_sum_loop
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import importlib
import json
import math
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------- Robust bracket-aware split for LLVM pass pipelines --------------- #


def split_top_level(text: str) -> list[str]:
    """Split a comma-separated string respecting nested brackets and quotes.

    LLVM pass pipeline strings contain nested structures like
    ``function<eager-inv>(instcombine,simplifycfg)`` that must not be split on
    their internal commas.  This function tracks bracket depth for ``[]``,
    ``{}``, ``()``, and ``<>`` as well as double-quote escaping, splitting only
    at commas where all bracket depths are zero.

    Args:
        text: The raw pipeline string, e.g. from ``opt --print-pipeline-passes``.

    Returns:
        List of top-level comma-separated tokens with whitespace stripped.
    """
    parts = []
    cur = []
    in_str = False
    esc = False
    depth = {"[": 0, "]": 0, "{": 0, "}": 0, "(": 0, ")": 0, "<": 0, ">": 0}
    openers = {"[": "]", "{": "}", "(": ")", "<": ">"}
    closers = {"]": "[", "}": "{", ")": "(", ">": "<"}
    for ch in text:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
        elif ch in openers:
            depth[ch] += 1
            cur.append(ch)
        elif ch in closers:
            depth[closers[ch]] -= 1
            cur.append(ch)
        elif ch == "," and all(depth[k] == 0 for k in openers):
            part = "".join(cur).strip()
            if part:
                parts.append(part)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts

# --------------- TOML parsing (stdlib or fallback) --------------- #
# Python 3.11+ ships ``tomllib``; older versions can use the ``tomli``
# third-party package.  When neither is available we use a minimal
# hand-rolled TOML parser that covers only the subset used in this
# project's config files (string/int/float/bool/array/inline-table).
try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    print("[INFO] tomllib not found, using fallback parser. For best results, install tomli via pip.", file=sys.stderr)
    try:
        tomllib = importlib.import_module("tomli")
    except ModuleNotFoundError:
        print("[INFO] tomli not found, using fallback parser. Consider installing tomli for better TOML support.", file=sys.stderr)
        class _TomlFallback:
            """Minimal TOML parser for project config files.

            Supports: ``[tables]``, ``[[array-of-tables]]``, key = value,
            inline arrays ``[…]``, inline tables ``{…}``, strings, booleans,
            integers, floats, and ``#`` comments.  Does **not** support
            multi-line strings (``\"\"\"``), datetime literals, or dotted keys.
            """
            @staticmethod
            def _strip_comment(line: str) -> str:
                """Remove inline ``#`` comments while respecting quoted strings."""
                out: List[str] = []
                in_str = False
                esc = False
                for ch in line:
                    if in_str:
                        out.append(ch)
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                            out.append(ch)
                        elif ch == "#":
                            break
                        else:
                            out.append(ch)
                return "".join(out).rstrip()

            @staticmethod
            def _split_top_level(text: str) -> List[str]:
                """Split *text* on commas that are not nested inside brackets or quotes."""
                parts: List[str] = []
                cur: List[str] = []
                in_str = False
                esc = False
                depth_arr = 0
                depth_obj = 0
                depth_paren = 0
                depth_angle = 0

                for ch in text:
                    if in_str:
                        cur.append(ch)
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                        continue

                    if ch == '"':
                        in_str = True
                        cur.append(ch)
                    elif ch == "[":
                        depth_arr += 1
                        cur.append(ch)
                    elif ch == "]":
                        depth_arr -= 1
                        cur.append(ch)
                    elif ch == "{":
                        depth_obj += 1
                        cur.append(ch)
                    elif ch == "}":
                        depth_obj -= 1
                        cur.append(ch)
                    elif ch == "(":
                        depth_paren += 1
                        cur.append(ch)
                    elif ch == ")":
                        depth_paren -= 1
                        cur.append(ch)
                    elif ch == "<":
                        depth_angle += 1
                        cur.append(ch)
                    elif ch == ">":
                        depth_angle -= 1
                        cur.append(ch)
                    elif ch == "," and depth_arr == 0 and depth_obj == 0 and depth_paren == 0 and depth_angle == 0:
                        part = "".join(cur).strip()
                        if part:
                            parts.append(part)
                        cur = []
                    else:
                        cur.append(ch)

                tail = "".join(cur).strip()
                if tail:
                    parts.append(tail)
                return parts

            @staticmethod
            def _bracket_balance(text: str, open_ch: str, close_ch: str) -> int:
                """Count unmatched *open_ch* minus *close_ch* outside quoted strings."""
                bal = 0
                in_str = False
                esc = False
                for ch in text:
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == open_ch:
                        bal += 1
                    elif ch == close_ch:
                        bal -= 1
                return bal

            @staticmethod
            def _parse_string(s: str) -> str:
                """Strip surrounding quotes and decode escape sequences."""
                s = s.strip()
                if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
                    return bytes(s[1:-1], "utf-8").decode("unicode_escape")
                return s

            @classmethod
            def _parse_value(cls, v: str):
                """Recursively parse a TOML value (string, bool, int, float, array, inline table)."""
                v = v.strip()
                if not v:
                    return ""
                if v.startswith('"') and v.endswith('"'):
                    return cls._parse_string(v)
                if v.lower() == "true":
                    return True
                if v.lower() == "false":
                    return False
                if re.fullmatch(r"[-+]?\d+", v):
                    try:
                        return int(v)
                    except ValueError:
                        pass
                if re.fullmatch(r"[-+]?\d+\.\d*", v):
                    try:
                        return float(v)
                    except ValueError:
                        pass

                if v.startswith("[") and v.endswith("]"):
                    inner = v[1:-1].strip()
                    if not inner:
                        return []
                    return [cls._parse_value(x) for x in cls._split_top_level(inner)]

                if v.startswith("{") and v.endswith("}"):
                    inner = v[1:-1].strip()
                    obj: Dict[str, Any] = {}
                    if not inner:
                        return obj
                    for part in cls._split_top_level(inner):
                        if "=" not in part:
                            continue
                        k, vv = part.split("=", 1)
                        obj[k.strip()] = cls._parse_value(vv.strip())
                    return obj

                return v

            @staticmethod
            def _ensure_table(
                root: Dict[str, Any], path_parts: List[str]
            ) -> Dict[str, Any]:
                """Walk *path_parts* in *root*, creating nested dicts as needed."""
                node = root
                for part in path_parts:
                    if part not in node or not isinstance(node[part], dict):
                        node[part] = {}
                    node = node[part]
                return node

            @classmethod
            def loads(cls, text: str) -> Dict[str, Any]:
                """Parse a TOML document string and return a nested dict."""
                data: Dict[str, Any] = {}
                current: Dict[str, Any] = data

                lines = text.splitlines()
                i = 0
                while i < len(lines):
                    line = cls._strip_comment(lines[i]).strip()
                    i += 1

                    if not line:
                        continue

                    if line.startswith("[[") and line.endswith("]]"):
                        header = line[2:-2].strip()
                        parts = [p.strip() for p in header.split(".") if p.strip()]
                        if not parts:
                            continue
                        parent = cls._ensure_table(data, parts[:-1])
                        key = parts[-1]
                        arr = parent.get(key)
                        if not isinstance(arr, list):
                            arr = []
                            parent[key] = arr
                        obj: Dict[str, Any] = {}
                        arr.append(obj)
                        current = obj
                        continue

                    if line.startswith("[") and line.endswith("]"):
                        header = line[1:-1].strip()
                        if not header:
                            continue
                        parts = [p.strip() for p in header.split(".") if p.strip()]
                        current = cls._ensure_table(data, parts)
                        continue

                    if "=" not in line:
                        continue

                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()

                    if (
                        value.startswith("[")
                        and cls._bracket_balance(value, "[", "]") > 0
                    ):
                        collected = [value]
                        bal = cls._bracket_balance(value, "[", "]")
                        while i < len(lines) and bal > 0:
                            nxt = cls._strip_comment(lines[i]).strip()
                            i += 1
                            if not nxt:
                                continue
                            collected.append(nxt)
                            bal += cls._bracket_balance(nxt, "[", "]")
                        value = " ".join(collected)

                    current[key] = cls._parse_value(value)

                return data

        tomllib = _TomlFallback()


# ============================= Data models ============================= #


@dataclasses.dataclass
class Benchmark:
    """One benchmark entry loaded from the ``[[benchmarks]]`` TOML table.

    Attributes:
        bench_id:           Unique short identifier (e.g. ``micro_sum_loop``).
        source:             Absolute path to the primary C/C++ source file.
        language:           Source language (``c`` or ``cpp``).
        compiler:           Compiler binary to use (default ``clang``).
        include_paths:      Extra ``-I`` directories relative to project root.
        extra_sources:      Additional source files linked into the binary.
        compile_flags:      Benchmark-specific compiler flags.
        link_flags:         Benchmark-specific linker flags.
        run_args:           Arguments passed to the compiled binary at runtime.
        expected_exit_code: The exit code a correct run should return (usually 0).
        enabled:            Whether this benchmark participates in the experiment.
        tags:               Free-form labels for filtering / grouping.
    """
    bench_id: str
    source: Path
    language: str
    compiler: str
    include_paths: List[str]
    extra_sources: List[str]
    compile_flags: List[str]
    link_flags: List[str]
    run_args: List[str]
    expected_exit_code: int
    enabled: bool
    tags: List[str]


@dataclasses.dataclass
class Variant:
    """An experiment variant — one point on the optimization-prefix axis.

    Attributes:
        name:        Human-readable label (``O0`` or ``O1_custom_<N>``).
        pass_count:  Number of pipeline passes to apply (``None`` for O0).
        is_baseline: True only for the O0 (unoptimized) variant.
        is_full_o1:  True when this variant covers the entire discovered pipeline.
    """
    name: str
    pass_count: Optional[int]  # None for O0
    is_baseline: bool = False
    is_full_o1: bool = False


@dataclasses.dataclass
class CompileResult:
    """Compile-stage output for one (benchmark, variant) pair.

    Captures both success/failure status and all compile-time metrics that
    are later joined into the main analysis table.  Paths are stored
    relative to the project root so that CSVs are portable.

    Structural metrics (``ir_instruction_count``, ``ir_basic_block_count``,
    ``text_section_size``) are filled post-compilation by analysing the
    optimised IR and the resulting binary.

    -1 is used as a sentinel for "not available / not computed".
    """
    benchmark_id: str
    variant: str
    compile_ok: bool
    compile_wall_seconds: float     # Wall-clock time for the full compile stage
    binary_path: str                # Relative path to the compiled executable
    binary_size_bytes: int          # Total file size of the output binary
    stderr_path: str                # Relative path to captured compiler stderr
    stdout_path: str                # Relative path to captured compiler stdout
    time_passes_path: str           # Relative path to -time-passes log
    stats_path: str                 # Relative path to -stats log
    ir_path: str                    # Relative path to optimized .ll output
    ll_path: str                    # Relative path to source-emitted .ll
    bc_path: str                    # Relative path to bitcode (.bc) output
    error: str                      # Error message if compile failed, else ""
    # --- Derived metrics (post-compilation analysis) ---
    ir_instruction_count: int = -1  # LLVM IR instruction count in optimized .ll
    ir_basic_block_count: int = -1  # LLVM IR basic block count in optimized .ll
    text_section_size: int = -1     # .text / __text section size in bytes
    opt_wall_seconds: float = 0.0   # Opt-only wall time from -time-passes Total
    opt_passes_json: str = ""       # JSON list of {pass_name, wall_seconds} dicts


@dataclasses.dataclass
class RunResult:
    """Runtime measurement for one (benchmark, variant) pair.

    Produced by either *hyperfine* (external JSON) or the internal manual
    timing fallback.  ``raw_seconds`` preserves every individual timing
    sample so downstream statistics (CI, effect-size, p-value) can be
    recomputed.
    """
    benchmark_id: str
    variant: str
    runs: int                   # Number of measurement runs
    warmup_runs: int            # Number of discarded warmup runs
    mean_seconds: float         # Arithmetic mean of raw samples
    median_seconds: float       # Median of raw samples
    stddev_seconds: float       # Sample standard deviation (0 if runs < 2)
    min_seconds: float          # Fastest observed run
    max_seconds: float          # Slowest observed run
    return_codes_ok: bool       # True if every run exited with expected RC
    raw_seconds: List[float]    # Individual timing samples


@dataclasses.dataclass
class ProfileResult:
    """Hardware performance counter data for one (benchmark, variant) pair.

    Fields use a **unified metric schema** so that consumers do not need to
    know which profiling backend (``perf`` or ``xctrace``) produced the data.

    The mapping from backend-specific data to these fields is:

    +-----------------------+-------------------+------------------------------+
    | Field                 | perf stat         | xctrace CPU Counters         |
    +=======================+===================+==============================+
    | metric_ir             | instructions      | retiring (bucket 1)          |
    | metric_drefs          | cache-references  | delivery/frontend (bucket 0) |
    | metric_d1_misses      | cache-misses      | bad speculation (bucket 2)   |
    | metric_ll_misses      | —                 | memory stalls (bucket 3)     |
    | metric_instructions   | instructions      | retiring (bucket 1)          |
    | metric_cycles         | cycles            | sum of all 4 buckets         |
    | metric_branch_misses  | branch-misses     | bad speculation (bucket 2)   |
    | metric_cache_references | cache-references | frontend + memory buckets  |
    +-----------------------+-------------------+------------------------------+

    ``None`` means the metric was unavailable (e.g. PMC not supported).
    """
    benchmark_id: str
    variant: str
    tool: str                                     # "perf" or "xctrace"
    ok: bool                                      # True if profiling succeeded
    output_path: str                              # Relative path to raw output
    metric_ir: Optional[int] = None               # Instruction-work proxy
    metric_drefs: Optional[int] = None            # Data-reference / frontend proxy
    metric_d1_misses: Optional[int] = None        # L1D or speculation misses
    metric_ll_misses: Optional[int] = None        # Last-level cache / memory stalls
    metric_instructions: Optional[int] = None     # Retired instruction count
    metric_cycles: Optional[int] = None           # CPU cycle count
    metric_ipc: Optional[float] = None            # Instructions per cycle
    metric_cpi: Optional[float] = None            # Cycles per instruction
    metric_branch_misses: Optional[int] = None    # Branch misprediction count
    metric_branch_miss_rate: Optional[float] = None  # branch_misses / instructions
    metric_cache_references: Optional[int] = None # Cache reference count
    error: str = ""                               # Error message if profiling failed


@dataclasses.dataclass
class Overrides:
    """CLI-driven overrides applied on top of the TOML configuration.

    Every field defaults to ``None`` (= "do not override").  When set,
    ``Config.apply_overrides()`` replaces the corresponding config value.
    """
    runs: Optional[int] = None
    warmup: Optional[int] = None
    timeout_seconds: Optional[int] = None
    run_timeout_seconds: Optional[int] = None
    compile_timeout_seconds: Optional[int] = None
    step: Optional[int] = None
    max_limit: Optional[int] = None
    explicit_limits: Optional[List[int]] = None
    include_o0: Optional[bool] = None
    include_full_o1: Optional[bool] = None
    fail_fast: Optional[bool] = None
    randomize_execution_order: Optional[bool] = None
    seed: Optional[int] = None
    benchmarks: Optional[List[str]] = None
    disable_profiler: bool = False
    profiler: Optional[str] = None  # "auto", "perf", "xctrace", "none"


# ============================= Utility functions ============================= #


def now_iso() -> str:
    """Return the current local time as an ISO-8601 string with timezone."""
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def eprint(msg: str) -> None:
    """Print a message to stderr (used for log and error output)."""
    print(msg, file=sys.stderr)


def mkdirp(p: Path) -> None:
    """Create a directory and all parents, silently succeeding if it exists."""
    p.mkdir(parents=True, exist_ok=True)


def read_text(p: Path) -> str:
    """Read the entire contents of a UTF-8 text file."""
    return p.read_text(encoding="utf-8")


def write_text(p: Path, text: str) -> None:
    """Write *text* to a file, creating parent directories as needed."""
    mkdirp(p.parent)
    p.write_text(text, encoding="utf-8")


def _json_sanitize(obj: Any) -> Any:
    """Recursively replace NaN / Inf floats with ``None`` for JSON safety.

    ``json.dumps(allow_nan=False)`` raises on NaN/Inf; this pre-pass
    converts them to ``None`` so the output is always valid JSON.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_sanitize(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_json_sanitize(v) for v in obj)
    return obj


def json_write(p: Path, obj: Any) -> None:
    """Write a Python object as pretty-printed JSON, sanitizing NaN/Inf."""
    mkdirp(p.parent)
    sanitized = _json_sanitize(obj)
    p.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

# ----------------------------- Utility (continued) ----------------------------- #

def parse_limit_from_variant_name(variant: str) -> Optional[int]:
    """Extract the numeric pass-count from a variant name like ``O1_custom_25``.

    Returns ``None`` for ``O0`` or unrecognized formats.
    """
    if variant == "O0":
        return None
    m = re.match(r"O1_custom_(\d+)", variant)
    if m:
        return int(m.group(1))
    return None


def csv_write(p: Path, rows: List[Dict[str, Any]]) -> None:
    """Write a list of dictionaries to a CSV file.

    Column order is taken from the first row's keys.  Parent directories
    are created automatically.  An empty list produces an empty file.
    """
    mkdirp(p.parent)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def safe_float(v: Any, default: Optional[float] = math.nan) -> Optional[float]:
    """Parse *v* to float, returning *default* if conversion fails or result is non-finite."""
    try:
        out = float(v)
    except Exception:
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def safe_int(v: Any, default: int = -1) -> int:
    """Parse *v* to int, returning *default* if conversion fails."""
    try:
        return int(v)
    except Exception:
        return default


def shell_join(parts: List[str]) -> str:
    """Join command parts with spaces (simple, no shell-quoting)."""
    return " ".join(parts)


def tool_exists(name: str) -> bool:
    """Return True if *name* is found on ``$PATH`` (via ``shutil.which``)."""
    return shutil.which(name) is not None


def stat_size(path: Path) -> int:
    """Return file size in bytes, or -1 if the file does not exist."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return -1


def run_cmd(
    cmd: List[str],
    cwd: Path,
    timeout_s: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* as a subprocess, capturing stdout and stderr as text.

    Does **not** raise on non-zero exit codes (``check=False``).  The caller
    is responsible for inspecting ``returncode``.
    """
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        timeout=timeout_s,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )





# ============================= Parsing ============================= #


# Regex to extract individual pass entries from ``opt -opt-bisect-limit`` output.
# Each line has the format:  BISECT: running pass (N) PassName on TargetName
PASS_LINE_RE = re.compile(r"BISECT:\s+running pass\s+\((\d+)\)\s+([^\s]+)\s+on\s+(.+)")


def parse_opt_bisect_passes(text: str) -> List[Dict[str, Any]]:
    """Parse ``opt -opt-bisect-limit`` stderr into structured pass records.

    Each returned dict has keys ``index`` (int), ``pass_name`` (str),
    ``target`` (str), and ``raw`` (the full line).  Used as a legacy
    fallback; the primary discovery method is ``--print-pipeline-passes``.
    """
    out: List[Dict[str, Any]] = []
    # Initialize the regex for parsing pass lines
    PASS_LINE_RE = re.compile(r"BISECT:\s+running pass\s+\((\d+)\)\s+([^\s]+)\s+on\s+(.+)")
    
    for line in text.splitlines():
        m = PASS_LINE_RE.search(line)
        if not m:
            continue
        idx, pname, target = m.groups()
        out.append(
            {
                "index": int(idx),
                "pass_name": pname,
                "target": target.strip(),
                "raw": line.strip(),
            }
        )
    return out


# --------------- perf stat (Linux) output parsing --------------- #

# Matches lines like "  1,234,567      cycles" (comma-grouped count + event name).
# Lines with "<not supported>" or "<not counted>" will NOT match because they
# lack the leading numeric token, so they are silently skipped.
_PERF_STAT_LINE_RE = re.compile(
    r"^\s*([\d,]+)\s+(\S+)",
    re.MULTILINE,
)


def parse_perf_stat(
    stderr_text: str,
) -> Dict[str, int]:
    """Parse ``perf stat`` stderr output into {event_name: count}.

    Handles both normal and ``<not supported>`` / ``<not counted>`` lines.
    """
    result: Dict[str, int] = {}
    for m in _PERF_STAT_LINE_RE.finditer(stderr_text):
        count_str, event = m.group(1), m.group(2)
        try:
            result[event] = int(count_str.replace(",", ""))
        except ValueError:
            continue
    return result


# --------------- xctrace (macOS) PMC parsing --------------- #

# XPath to export the per-process aggregated CPU counter metrics from an
# xctrace trace bundle.  Targets the "CPU Counters" instrument's
# per-process aggregation table.
XCTRACE_XPATH = (
    '/trace-toc/run[@number="1"]/data'
    '/table[@schema="CounterMetricAggregatedForProcess"]'
)


def _resolve_uint64_array(elem: ET.Element) -> Optional[List[int]]:
    """Extract decimal values from a <uint64-array> element.

    The text content looks like ``4395 4875 488 229``.
    Elements with a ``ref`` attribute contain no text (they point to an earlier
    element with the same data); we skip those.
    """
    txt = (elem.text or "").strip()
    if not txt:
        return None
    try:
        return [int(x) for x in txt.split()]
    except ValueError:
        return None


def parse_xctrace_pmc_xml(
    xml_text: str,
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Parse xctrace export XML for the *CounterMetricAggregatedForProcess* table.

    Each ``<row>`` contains a ``<uint64-array>`` with 4 values produced by the
    CPU Counters *bottleneck* counting mode on Apple Silicon:
        [0] cycles / delivery   [1] useful / processing
        [2] discarded           [3] memory stalls (L1D misses)

    We sum the values across all sampling epochs and map them to the closest
    equivalents of the callgrind/cachegrind metrics:
        metric_ir   -> sum of index 1 ("useful" instruction work)
        metric_drefs -> sum of index 0 ("cycles" / delivery overhead)
        metric_d1_misses -> sum of index 2 (discarded)
        metric_ll_misses -> sum of index 3 (memory / L1D miss stalls)
    """
    totals: List[int] = [0, 0, 0, 0]
    found = False
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, None, None, None

    # Aggregate per-sample rows into one metric tuple per benchmark variant.
    for arr in root.iter("uint64-array"):
        vals = _resolve_uint64_array(arr)
        if vals is None:
            continue
        for i in range(min(len(vals), 4)):
            totals[i] += vals[i]
        found = True

    if not found:
        return None, None, None, None

    return totals[1], totals[0], totals[2], totals[3]


# ==================== IR / Binary structural metrics ==================== #


def count_ir_metrics(ll_path: Path) -> Tuple[int, int]:
    """Count LLVM IR instructions and basic blocks in a .ll file.

    Instructions: lines inside function bodies that are SSA assignments or
    void-returning instructions (store, br, ret, call, etc.).
    Basic blocks: entry blocks (one per define) plus explicit labels.
    """
    if not ll_path.exists():
        return -1, -1
    try:
        text = ll_path.read_text(encoding="utf-8")
    except Exception:
        return -1, -1

    instr_count = 0
    bb_count = 0
    in_function = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("define "):
            in_function = True
            bb_count += 1  # entry block
            continue

        if stripped == "}":
            in_function = False
            continue

        if not in_function:
            continue

        # Basic-block label (e.g. "foo:", "42:", ".lr.ph:")
        if re.match(r"^[a-zA-Z0-9_.]+:\s*", stripped) or re.match(
            r"^\d+:\s*", stripped
        ):
            bb_count += 1
            continue

        # Skip empty lines, comments, metadata
        if not stripped or stripped.startswith(";") or stripped.startswith("!"):
            continue

        # SSA assignment (e.g. "%x = add i32 %a, %b")
        if "=" in stripped and not stripped.startswith("!"):
            instr_count += 1
        # Void-returning instructions
        elif any(
            stripped.startswith(kw + " ") or stripped == kw
            for kw in (
                "store", "br", "ret", "switch", "unreachable", "fence",
                "call", "invoke", "resume", "indirectbr",
                "catchswitch", "catchret", "cleanupret",
            )
        ):
            instr_count += 1

    return instr_count, bb_count


def get_text_section_size(binary_path: Path) -> int:
    """Get the code (.text / __text) section size from a compiled binary."""
    try:
        # macOS (Mach-O): size -m
        cp = subprocess.run(
            ["size", "-m", str(binary_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            for line in (cp.stdout or "").splitlines():
                m = re.search(r"Section\s+__text:\s*(\d+)", line)
                if m:
                    return int(m.group(1))
        # Linux (ELF): size
        cp = subprocess.run(
            ["size", str(binary_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            lines = (cp.stdout or "").strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if parts:
                    return int(parts[0])
    except Exception:
        pass
    return -1


# ==================== Compiler time-passes parsing ==================== #


# Regex for one data line from ``opt -time-passes`` output.
# Columns: user_time (pct)  sys_time (pct)  user+sys (pct)  wall_time (pct)  [instr_count]  pass_name
# The optional ``(?:\d+\s+)?`` group handles the extra instruction-count
# column introduced in LLVM 18+.
_TIME_PASSES_LINE_RE = re.compile(
    r"\s*([\d.]+)\s+\(\s*[\d.]+%\)\s+"
    r"([\d.]+)\s+\(\s*[\d.]+%\)\s+"
    r"([\d.]+)\s+\(\s*[\d.]+%\)\s+"
    r"([\d.]+)\s+\(\s*[\d.]+%\)\s+"
    r"(?:\d+\s+)?"  # optional instruction count column (LLVM 18+)
    r"(.+)"
)


def parse_time_passes(text: str) -> Tuple[float, List[Dict[str, Any]]]:
    """Parse LLVM ``-time-passes`` output into structured timing data.

    Returns ``(total_wall_seconds, per_pass_timings)`` where each entry is
    ``{"pass_name": str, "wall_seconds": float}``.
    """
    total_wall = 0.0
    passes: List[Dict[str, Any]] = []
    for line in text.splitlines():
        m = _TIME_PASSES_LINE_RE.match(line)
        if m:
            wall = float(m.group(4))
            name = m.group(5).strip()
            if name == "Total":
                total_wall = wall
            else:
                passes.append({"pass_name": name, "wall_seconds": wall})
    return total_wall, passes


# ============================= Statistics ============================= #

# Look-up table of t-distribution critical values for a 95% two-tailed
# confidence interval (alpha = 0.05).  Keys are degrees-of-freedom;
# intermediate values are linearly interpolated by ``_t_critical_95()``.
_T_CRIT_95: Dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021,
    60: 2.000, 120: 1.980,
}


def _t_critical_95(df: int) -> float:
    """Look up or interpolate the t-critical value for a 95% CI."""
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    if df > 120:
        return 1.96
    keys = sorted(_T_CRIT_95.keys())
    for i in range(len(keys) - 1):
        if keys[i] <= df <= keys[i + 1]:
            lo, hi = keys[i], keys[i + 1]
            ratio = (df - lo) / (hi - lo)
            return _T_CRIT_95[lo] + ratio * (_T_CRIT_95[hi] - _T_CRIT_95[lo])
    return 1.96


def compute_ci_95(
    samples: List[float],
) -> Tuple[Optional[float], Optional[float]]:
    """Compute 95% confidence interval for the mean using the t-distribution."""
    n = len(samples)
    if n == 0:
        return None, None
    mean = statistics.fmean(samples)
    if n == 1:
        return mean, mean
    stderr = statistics.stdev(samples) / math.sqrt(n)
    t = _t_critical_95(n - 1)
    margin = t * stderr
    return mean - margin, mean + margin


def compute_cohens_d(a: List[float], b: List[float]) -> Optional[float]:
    """Compute Cohen's d effect size between groups *a* and *b*.

    Returns ``None`` when sample sizes are too small (need >= 2 each).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    var_a, var_b = statistics.variance(a), statistics.variance(b)
    n_a, n_b = len(a), len(b)
    denom = (n_a - 1) * var_a + (n_b - 1) * var_b
    if denom == 0:
        return 0.0
    pooled_std = math.sqrt(denom / (n_a + n_b - 2))
    if pooled_std == 0:
        return 0.0
    return (mean_a - mean_b) / pooled_std


def compute_welch_t_pvalue(a: List[float], b: List[float]) -> Optional[float]:
    """Two-tailed p-value from Welch's t-test.

    Uses scipy if available; otherwise falls back to a normal approximation.
    Returns ``None`` when sample sizes are too small (need >= 2 each).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        from scipy.stats import ttest_ind
        result = ttest_ind(a, b, equal_var=False)
        return float(result.pvalue)
    except ImportError:
        pass
    # Fallback: normal approximation of p-value
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    var_a, var_b = statistics.variance(a), statistics.variance(b)
    n_a, n_b = len(a), len(b)
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return 1.0
    t_stat = abs(mean_a - mean_b) / se
    return math.erfc(t_stat / math.sqrt(2))


# Metrics for which we compute deltas (vs O0, vs previous variant).
# Each entry must match a column name in the main output table.  Adding
# a new metric here automatically generates ``delta_vs_O0_<name>`` and
# ``delta_prev_<name>`` columns in the output.
_DELTA_METRICS = [
    "runtime_mean_seconds",
    "compile_time_wall_seconds",
    "binary_size_bytes",
    "ir_instruction_count",
    "text_section_size",
    "profile_ir",
    "profile_d1_misses",
    "profile_ll_misses",
    "profile_instructions",
    "profile_cycles",
    "profile_branch_misses",
    "profile_cache_references",
]


# ============================= Config ============================= #


class Config:
    """Typed accessor for the TOML experiment configuration.

    Reads all sections from the raw TOML dict and exposes them as typed
    Python attributes.  Paths are resolved relative to the project root
    (the parent of the ``configs/`` directory).

    Sections consumed:
        * ``[environment]`` — output directories
        * ``[experiment]`` — warmup, runs, timeout, ordering
        * ``[llvm]`` — compiler / optimizer binaries and flags
        * ``[compilation]`` — C flags, linker flags, binary extension
        * ``[passes]`` — pipeline discovery and variant generation policy
        * ``[tools.hyperfine]``, ``[tools.perf]``, ``[tools.xctrace]``,
          ``[tools.profiler]`` — tool toggles and parameters
        * ``[output]`` — CSV / JSON switches and schema paths
    """
    def __init__(self, root: Path, raw: Dict[str, Any]) -> None:
        """Populate every config attribute from *raw* TOML dict, applying defaults."""
        self.root = root
        self.raw = raw

        env = raw.get("environment", {})
        self.results_dir = root / env.get("results_dir", "results")
        self.artifacts_dir = root / env.get("artifacts_dir", "artifacts")

        exp = raw.get("experiment", {})
        self.warmup_runs = safe_int(exp.get("warmup_runs", 5), 5)
        self.measurement_runs = safe_int(exp.get("measurement_runs", 30), 30)
        self.timeout_seconds = safe_int(exp.get("timeout_seconds", 300), 300)
        self.fail_fast = bool(exp.get("fail_fast", False))
        self.randomize_execution_order = bool(
            exp.get("randomize_execution_order", True)
        )
        self.seed = safe_int(raw.get("project", {}).get("seed", 42), 42)

        llvm = raw.get("llvm", {})
        self.clang = llvm.get("clang", "clang")
        self.opt = llvm.get("opt", "opt")
        self.opt_pipeline_flag = llvm.get("opt_pipeline_flag", "-O1")
        self.emit_ir_flags = list(
            llvm.get(
                "emit_ir_flags", ["-S", "-emit-llvm", "-Xclang", "-disable-O0-optnone"]
            )
        )
        self.collect_time_passes = bool(llvm.get("collect_time_passes", True))
        self.collect_stats = bool(llvm.get("collect_stats", True))

        comp = raw.get("compilation", {})
        self.cflags_common = list(comp.get("cflags_common", []))
        self.cflags_o0 = list(comp.get("cflags_O0", ["-O0"]))
        self.cflags_emit_ir = list(
            comp.get("cflags_emit_ir", ["-O0", "-Xclang", "-disable-O0-optnone"])
        )
        self.ldflags = list(comp.get("ldflags", []))
        self.binary_extension = comp.get("binary_extension", "")
        self.compile_timeout_seconds = safe_int(
            comp.get("compile_timeout_seconds", 120), 120
        )
        self.run_timeout_seconds = safe_int(comp.get("run_timeout_seconds", 120), 120)

        passes = raw.get("passes", {})
        self.derive_from_print_pipeline = bool(passes.get("derive_from_print_pipeline", True))
        self.max_passes = safe_int(passes.get("max_passes", -1), -1)
        # Bisect logic removed
        self.store_discovered_passes = root / passes.get(
            "store_discovered_passes", "artifacts/discovered_passes_O1.txt"
        )

        variants = passes.get("variants", {})
        self.include_o0_baseline = bool(variants.get("include_O0_baseline", True))
        self.include_full_o1 = bool(variants.get("include_full_O1", True))
        self.start_limit = safe_int(variants.get("start_limit", 0), 0)
        self.step = max(1, safe_int(variants.get("step", 1), 1))
        self.explicit_limits = [
            safe_int(x) for x in variants.get("explicit_limits", [])
        ]

        tools = raw.get("tools", {})
        h = tools.get("hyperfine", {})
        self.hyperfine_enabled = bool(h.get("enabled", True))
        self.hyperfine_bin = h.get("binary", "hyperfine")
        self.hyperfine_runs = safe_int(
            h.get("runs", self.measurement_runs), self.measurement_runs
        )
        self.hyperfine_warmup = safe_int(h.get("warmup", 3), 3)

        pf = tools.get("perf", {})
        self.perf_enabled = bool(pf.get("enabled", False))
        self.perf_bin = pf.get("binary", "perf")
        self.perf_events = list(pf.get("events", ["cycles", "instructions", "cache-misses", "branch-misses"]))
        self.perf_stat_repetitions = safe_int(pf.get("stat_repetitions", 5), 5)
        self.perf_timeout_seconds = safe_int(pf.get("timeout_seconds", 600), 600)

        xt = tools.get("xctrace", {})
        self.xctrace_enabled = bool(xt.get("enabled", True))
        self.xctrace_template = xt.get("template", "CPU Counters")
        self.xctrace_timeout_seconds = safe_int(xt.get("timeout_seconds", 600), 600)

        profiler = tools.get("profiler", {})
        self.profiler_backend = profiler.get("backend", "auto")

        out = raw.get("output", {})
        self.write_csv = bool(out.get("write_csv", True))
        self.write_json = bool(out.get("write_json", True))
        schema = out.get("schema", {})
        self.main_table = root / schema.get("main_table", "results/main.csv")
        self.passes_table = root / schema.get("passes_table", "results/passes.csv")
        self.compile_table = root / schema.get(
            "compile_table", "results/compile_metrics.csv"
        )
        self.runtime_table = root / schema.get(
            "runtime_table", "results/runtime_metrics.csv"
        )
        self.profile_table = root / schema.get(
            "profile_table", "results/profile_metrics.csv"
        )
        self.errors_table = root / schema.get("errors_table", "results/errors.csv")

    def apply_overrides(self, ov: Overrides) -> None:
        """Merge CLI overrides into this configuration instance.

        Only fields explicitly set (non-None) in *ov* are applied;
        unset fields leave the TOML-parsed defaults untouched.
        """
        if ov.runs is not None:
            self.measurement_runs = max(1, ov.runs)
            self.hyperfine_runs = max(1, ov.runs)
        if ov.warmup is not None:
            self.warmup_runs = max(0, ov.warmup)
            self.hyperfine_warmup = max(0, ov.warmup)
        if ov.timeout_seconds is not None:
            self.timeout_seconds = max(1, ov.timeout_seconds)
        if ov.run_timeout_seconds is not None:
            self.run_timeout_seconds = max(1, ov.run_timeout_seconds)
        if ov.compile_timeout_seconds is not None:
            self.compile_timeout_seconds = max(1, ov.compile_timeout_seconds)
        if ov.step is not None:
            self.step = max(1, ov.step)
        # Bisect logic removed
        if ov.explicit_limits is not None:
            self.explicit_limits = sorted(set(x for x in ov.explicit_limits if x >= 0))
        if ov.include_o0 is not None:
            self.include_o0_baseline = ov.include_o0
        if ov.include_full_o1 is not None:
            self.include_full_o1 = ov.include_full_o1
        if ov.fail_fast is not None:
            self.fail_fast = ov.fail_fast
        if ov.randomize_execution_order is not None:
            self.randomize_execution_order = ov.randomize_execution_order
        if ov.seed is not None:
            self.seed = ov.seed
        if ov.profiler is not None:
            self.profiler_backend = ov.profiler
        if ov.disable_profiler:
            self.profiler_backend = "none"


def load_raw_config(path: Path) -> Dict[str, Any]:
    """Parse a TOML config file and return the raw dictionary tree."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_benchmarks(raw_cfg: Dict[str, Any], root: Path) -> List[Benchmark]:
    """Build ``Benchmark`` objects from the ``[[benchmarks]]`` TOML table.

    Only enabled benchmarks are returned.  Paths in the config are resolved
    relative to *root* (the project root directory).
    """
    out: List[Benchmark] = []
    for item in raw_cfg.get("benchmarks", []):
        out.append(
            Benchmark(
                bench_id=item["id"],
                source=root / item["source"],
                language=str(item.get("language", "c")),
                compiler=str(item.get("compiler", "clang")),
                include_paths=[str(x) for x in item.get("include_paths", [])],
                extra_sources=[str(x) for x in item.get("extra_sources", [])],
                compile_flags=list(item.get("compile_flags", [])),
                link_flags=list(item.get("link_flags", [])),
                run_args=[str(x) for x in item.get("run_args", [])],
                expected_exit_code=safe_int(item.get("expected_exit_code", 0), 0),
                enabled=bool(item.get("enabled", True)),
                tags=list(item.get("tags", [])),
            )
        )
    return [b for b in out if b.enabled]


# ============================= Orchestrator ============================= #


class Orchestrator:
    """Drives a single end-to-end benchmark experiment.

    Lifecycle: ``preflight`` -> ``run`` (which calls ``_discover_passes``,
    ``_build_variants``, ``_compile_variant``, ``_measure_runtime_*``,
    ``_profile``) -> ``_write_outputs``.
    """

    def __init__(self, config_path: Path, overrides: Overrides) -> None:
        """Initialize one experiment run with isolated artifact/result directories.

        Semantically, each Orchestrator instance is a single reproducible run:
        it captures configuration, selected benchmarks, and all outputs under a
        unique run_id so different executions never overwrite each other.
        """
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent.parent.resolve()
        self.raw_cfg = load_raw_config(self.config_path)
        self.config = Config(self.root, self.raw_cfg)
        self.config.apply_overrides(overrides)

        # Load all enabled benchmarks from the config
        all_benchmarks = load_benchmarks(self.raw_cfg, self.root)
        # If benchmark overrides are specified, filter them
        if overrides.benchmarks:
            self.benchmarks = [b for b in all_benchmarks if b.bench_id in overrides.benchmarks]
        else:
            self.benchmarks = all_benchmarks

        self.compile_results: List[CompileResult] = []
        self.run_results: List[RunResult] = []
        self.profile_results: List[ProfileResult] = []

        self.errors: List[Dict[str, Any]] = []

        self.run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = self.config.artifacts_dir / f"run_{self.run_id}"
        self.bin_dir = self.run_dir / "bin"
        self.ir_dir = self.run_dir / "ir"
        self.log_dir = self.run_dir / "logs"
        self.profile_dir = self.run_dir / "profiles"

        for d in [
            self.config.results_dir,
            self.config.artifacts_dir,
            self.run_dir,
            self.bin_dir,
            self.ir_dir,
            self.log_dir,
            self.profile_dir,
        ]:
            mkdirp(d)

    def _record_error(
        self, where: str, benchmark_id: str, variant: str, message: str
    ) -> None:
        """Append a structured error record and print a diagnostic to stderr."""
        self.errors.append(
            {
                "timestamp": now_iso(),
                "where": where,
                "benchmark_id": benchmark_id,
                "variant": variant,
                "message": message,
            }
        )
        eprint(f"[ERROR] [{where}] [{benchmark_id}] [{variant}] {message}")

    def preflight(self) -> None:
        """Validate that required tools exist and benchmarks are runnable.

        Raises ``RuntimeError`` if required tools are missing from ``$PATH``, no
        benchmarks are selected, or a benchmark source file does not exist.
        """
        if not self.config.hyperfine_enabled:
            raise RuntimeError(
                "Hyperfine must be enabled in config; internal timing fallback is removed."
            )
        required = [self.config.clang, self.config.opt, self.config.hyperfine_bin]
        missing = [x for x in required if not tool_exists(x)]
        if self.config.profiler_backend != "none":
            if self.config.profiler_backend == "perf":
                required.append(self.config.perf_bin)
            elif self.config.profiler_backend == "xctrace":
                required.append("xcrun")
            elif sys.platform == "darwin":
                required.append("xcrun")
            else:
                required.append(self.config.perf_bin)

        missing = [x for x in required if not tool_exists(x)]
        if missing:
            raise RuntimeError(f"Missing required tools in PATH: {missing}")
        if not self.benchmarks:
            raise RuntimeError("No enabled benchmarks selected.")
        for b in self.benchmarks:
            if not b.source.exists():
                raise RuntimeError(f"Benchmark source not found: {b.source}")

    def _emit_ir(self, b: Benchmark) -> Tuple[bool, Path, str]:
        """Compile benchmark C source to LLVM IR used as optimization input.

        This creates the canonical pre-optimization representation from which
        all non-baseline variants are derived, ensuring variants differ only by
        optimization pipeline truncation and not by front-end differences.
        """
        ll_path = self.ir_dir / f"{b.bench_id}.ll"
        include_args = []
        for inc in b.include_paths:
            include_args.extend(["-I", str((self.root / inc).resolve())])

        cmd = (
            [b.compiler]
            + self.config.emit_ir_flags
            + self.config.cflags_common
            + self.config.cflags_emit_ir
            + include_args
            + b.compile_flags
            + [str(b.source), "-o", str(ll_path)]
        )
        cp = run_cmd(cmd, cwd=self.root, timeout_s=self.config.compile_timeout_seconds)
        if cp.returncode != 0:
            return False, ll_path, cp.stderr or "emit-ir failed"
        return True, ll_path, ""

    def _discover_passes(self, ll_path: Path) -> List[Dict[str, Any]]:
        """Extract the effective O1 pipeline to define the variant search space.

        The discovered ordered pass list is the semantic backbone of the study:
        each variant corresponds to keeping only a prefix of this sequence.
        """
        # Use only --print-pipeline-passes for discovery
        cmd = [
            self.config.opt,
            "--print-pipeline-passes",
            self.config.opt_pipeline_flag,
            "-S",
            "/dev/null",
            "-o",
            "/dev/null",
        ]
        cp = run_cmd(cmd, cwd=self.root, timeout_s=self.config.timeout_seconds)
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
        # Find the first non-empty line that is not a warning/error
        pipeline_line = next((line for line in text.splitlines() if line.strip() and not line.strip().startswith("warning") and not line.strip().startswith("error")), "")
        if not pipeline_line:
            self._record_error(
                "discover_passes",
                "__global__",
                "O1",
                "No passes discovered via --print-pipeline-passes.",
            )
            return []
        # Use robust bracket-aware split for pipeline string
        pass_names = [p.strip() for p in split_top_level(pipeline_line) if p.strip()]
        passes = [
            {"index": i, "pass_name": pname, "target": "", "raw": pname}
            for i, pname in enumerate(pass_names)
        ]
        lines = [f"{p['index']:4d}\t{p['pass_name']}" for p in passes]
        write_text(self.config.store_discovered_passes, "\n".join(lines) + "\n")
        return passes

    def _build_variants(self) -> List[Variant]:
        """Create experiment variants as cumulative prefixes of discovered passes.

        O0 is the baseline (no O1 pipeline), while O1_custom_N means "apply the
        first N passes of the discovered pipeline". This supports incremental
        attribution of performance/size changes to optimization progress.
        """
        if not self.discovered_passes:
            return [Variant(name="O0", pass_count=None, is_baseline=True)]

        max_found = max(p["index"] for p in self.discovered_passes) if self.discovered_passes else 0
        if self.config.max_passes >= 0:
            max_found = min(max_found, self.config.max_passes)

        if self.config.explicit_limits:
            limits = sorted(
                set(x for x in self.config.explicit_limits if 0 <= x <= max_found)
            )
        else:
            limits = list(
                range(self.config.start_limit, max_found + 1, self.config.step)
            )

        out: List[Variant] = []
        if self.config.include_o0_baseline:
            out.append(Variant(name="O0", pass_count=None, is_baseline=True))

        for lim in limits:
            out.append(Variant(name=f"O1_custom_{lim}", pass_count=lim))

        if self.config.include_full_o1 and max_found not in limits:
            out.append(
                Variant(
                    name=f"O1_custom_{max_found}",
                    pass_count=max_found,
                    is_full_o1=True,
                )
            )

        return out

    def _compile_variant(
        self, b: Benchmark, ll_path: Path, v: Variant
    ) -> CompileResult:
        """Build one benchmark variant and persist stage-specific logs.

        Baseline variants compile directly from source with O0 flags.
        Non-baseline variants run a truncated `opt` pipeline on IR, then produce
        an executable. Returned metrics capture both correctness and compile cost.
        """
        stem = f"{b.bench_id}__{v.name}"
        out_bin = self.bin_dir / (stem + self.config.binary_extension)
        out_ir = self.ir_dir / (stem + ".opt.ll")
        out_bc = self.ir_dir / (stem + ".opt.bc")
        out_stdout = self.log_dir / (stem + ".compile.stdout.txt")
        out_stderr = self.log_dir / (stem + ".compile.stderr.txt")
        out_tpass = self.log_dir / (stem + ".time-passes.txt")
        out_stats = self.log_dir / (stem + ".stats.txt")

        t0 = time.perf_counter()
        ok = False
        err = ""
        try:
            if v.is_baseline:
                include_args = []
                for inc in b.include_paths:
                    include_args.extend(["-I", str((self.root / inc).resolve())])
                extra_src_args = [str((self.root / s).resolve()) for s in b.extra_sources]
                cmd = (
                    [b.compiler]
                    + self.config.cflags_common
                    + self.config.cflags_o0
                    + include_args
                    + b.compile_flags
                    + [str(b.source)]
                    + extra_src_args
                    + ["-o", str(out_bin)]
                    + self.config.ldflags
                    + b.link_flags
                )
                cp = run_cmd(cmd, cwd=self.root, timeout_s=self.config.compile_timeout_seconds)
                write_text(out_stdout, cp.stdout or "")
                write_text(out_stderr, cp.stderr or "")
                ok = cp.returncode == 0
                if not ok:
                    err = cp.stderr or "baseline compile failed"
            else:
                # Build the truncated pipeline
                if v.pass_count is not None and v.pass_count > 0:
                    # Use robust bracket-aware split for pipeline truncation
                    pipeline_line = ",".join([p["pass_name"] for p in self.discovered_passes])
                    top_level_passes = split_top_level(pipeline_line)
                    truncated = top_level_passes[:v.pass_count]
                    passes_arg = "--passes=" + ",".join(truncated)
                else:
                    passes_arg = self.config.opt_pipeline_flag
                opt_cmd = [
                    self.config.opt,
                    passes_arg,
                    str(ll_path),
                    "-S",
                    "-o",
                    str(out_ir),
                ]
                if self.config.collect_time_passes:
                    opt_cmd.append("-time-passes")
                if self.config.collect_stats:
                    opt_cmd.append("-stats")
                cp_opt = run_cmd(opt_cmd, cwd=self.root, timeout_s=self.config.timeout_seconds)
                write_text(out_stdout, cp_opt.stdout or "")
                write_text(out_stderr, cp_opt.stderr or "")
                if self.config.collect_time_passes:
                    write_text(out_tpass, cp_opt.stderr or "")
                if self.config.collect_stats:
                    write_text(out_stats, cp_opt.stderr or "")
                if cp_opt.returncode != 0:
                    ok = False
                    err = cp_opt.stderr or "opt stage failed"
                else:
                    as_cmd = [self.config.opt, str(out_ir), "-o", str(out_bc)]
                    cp_as = run_cmd(as_cmd, cwd=self.root, timeout_s=self.config.timeout_seconds)
                    if cp_as.returncode != 0:
                        ok = False
                        err = cp_as.stderr or "bc emit stage failed"
                        write_text(
                            out_stderr,
                            read_text(out_stderr)
                            + "\n\n[bc emit stage]\n"
                            + (cp_as.stderr or ""),
                        )
                    else:
                        cc_cmd = (
                            [b.compiler, str(out_ir)]
                            + [str((self.root / s).resolve()) for s in b.extra_sources]
                            + ["-o", str(out_bin)]
                            + self.config.ldflags
                            + b.link_flags
                        )
                        cp_cc = run_cmd(
                            cc_cmd,
                            cwd=self.root,
                            timeout_s=self.config.compile_timeout_seconds,
                        )
                        write_text(
                            out_stdout,
                            read_text(out_stdout)
                            + "\n\n[clang stdout]\n"
                            + (cp_cc.stdout or ""),
                        )
                        write_text(
                            out_stderr,
                            read_text(out_stderr)
                            + "\n\n[clang stderr]\n"
                            + (cp_cc.stderr or ""),
                        )
                        ok = cp_cc.returncode == 0
                        if not ok:
                            err = cp_cc.stderr or "final clang stage failed"
        except Exception as ex:
            ok = False
            err = str(ex)
        t1 = time.perf_counter()

        # Collect IR structural metrics and text section size
        ir_instr_count = -1
        ir_bb_count = -1
        txt_sect_size = -1
        opt_wall = 0.0
        opt_passes_data = ""

        if ok:
            # IR file: optimized IR for non-baseline, source-emitted for O0
            ir_file = out_ir if (not v.is_baseline and out_ir.exists()) else ll_path
            if ir_file.exists():
                ir_instr_count, ir_bb_count = count_ir_metrics(ir_file)
            if out_bin.exists():
                txt_sect_size = get_text_section_size(out_bin)
            # Parse -time-passes for opt-only wall time and per-pass breakdown
            if not v.is_baseline and out_tpass.exists():
                tpass_text = read_text(out_tpass)
                opt_wall, opt_passes_list = parse_time_passes(tpass_text)
                opt_passes_data = json.dumps(opt_passes_list) if opt_passes_list else ""

        return CompileResult(
            benchmark_id=b.bench_id,
            variant=v.name,
            compile_ok=ok,
            compile_wall_seconds=t1 - t0,
            binary_path=str(out_bin.relative_to(self.root)),
            binary_size_bytes=stat_size(out_bin) if ok else -1,
            stderr_path=str(out_stderr.relative_to(self.root)),
            stdout_path=str(out_stdout.relative_to(self.root)),
            time_passes_path=str(out_tpass.relative_to(self.root)),
            stats_path=str(out_stats.relative_to(self.root)),
            ir_path=str(out_ir.relative_to(self.root)),
            ll_path=str(ll_path.relative_to(self.root)),
            bc_path=str(out_bc.relative_to(self.root)),
            error=err,
            ir_instruction_count=ir_instr_count,
            ir_basic_block_count=ir_bb_count,
            text_section_size=txt_sect_size,
            opt_wall_seconds=opt_wall,
            opt_passes_json=opt_passes_data,
        )

    def _measure_runtime_hyperfine(
        self, b: Benchmark, v: Variant, exe: Path, args: List[str]
    ) -> Optional[RunResult]:
        """Measure runtime via hyperfine and normalize into RunResult schema.

        The goal is to keep downstream analysis backend-agnostic: whether timing
        comes from hyperfine or manual timing, consumers see the same fields.
        """
        if not (
            self.config.hyperfine_enabled and tool_exists(self.config.hyperfine_bin)
        ):
            return None

        out_json = self.log_dir / f"{b.bench_id}__{v.name}.hyperfine.json"
        cmd = [
            self.config.hyperfine_bin,
            "--warmup",
            str(self.config.hyperfine_warmup),
            "--runs",
            str(self.config.hyperfine_runs),
            "--export-json",
            str(out_json),
            shell_join([str(exe)] + args),
        ]
        cp = run_cmd(cmd, cwd=self.root, timeout_s=self.config.timeout_seconds)
        if cp.returncode != 0 or not out_json.exists():
            self._record_error(
                "hyperfine", b.bench_id, v.name, cp.stderr or "hyperfine failed"
            )
            return None

        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
            if not data.get("results"):
                self._record_error(
                    "hyperfine", b.bench_id, v.name, "No results in hyperfine JSON"
                )
                return None
            r0 = data["results"][0]
            times = [safe_float(x) for x in r0.get("times", [])]
            return RunResult(
                benchmark_id=b.bench_id,
                variant=v.name,
                runs=safe_int(r0.get("runs", len(times)), len(times)),
                warmup_runs=self.config.hyperfine_warmup,
                mean_seconds=safe_float(
                    r0.get("mean", statistics.fmean(times) if times else 0.0),
                    0.0,
                ),
                median_seconds=safe_float(
                    r0.get("median", statistics.median(times) if times else 0.0),
                    0.0,
                ),
                stddev_seconds=safe_float(
                    r0.get(
                        "stddev", statistics.stdev(times) if len(times) >= 2 else 0.0
                    ),
                    0.0,
                ),
                min_seconds=min(times) if times else 0.0,
                max_seconds=max(times) if times else 0.0,
                return_codes_ok=True,
                raw_seconds=times,
            )
        except Exception as ex:
            self._record_error("hyperfine-parse", b.bench_id, v.name, str(ex))
            return None

    def _profile_perf(
        self, b: Benchmark, v: Variant, exe: Path, args: List[str]
    ) -> List[ProfileResult]:
        """Collect profile metrics using Linux ``perf stat``.

        Runs ``perf stat -e <events> -r <reps> -- <exe> <args>`` and parses the
        aggregated counters from stderr into ProfileResult fields.
        """
        out: List[ProfileResult] = []
        if not (self.config.perf_enabled and tool_exists(self.config.perf_bin)):
            return out

        perf_out = self.profile_dir / f"{b.bench_id}__{v.name}.perf.txt"
        events_str = ",".join(self.config.perf_events)
        cmd = [
            self.config.perf_bin, "stat",
            "-e", events_str,
            "-r", str(self.config.perf_stat_repetitions),
            "--", str(exe),
        ] + args

        cp = run_cmd(
            cmd, cwd=self.root, timeout_s=self.config.perf_timeout_seconds
        )

        stderr = cp.stderr or ""
        perf_out.write_text(stderr, encoding="utf-8")

        ok = cp.returncode == b.expected_exit_code
        if not ok:
            err_msg = stderr[:500] or "perf stat failed"
            self._record_error("perf", b.bench_id, v.name, err_msg)
            out.append(
                ProfileResult(
                    benchmark_id=b.bench_id,
                    variant=v.name,
                    tool="perf",
                    ok=False,
                    output_path=str(perf_out.relative_to(self.root)),
                    error=err_msg,
                )
            )
            return out

        counters = parse_perf_stat(stderr)

        instructions = counters.get("instructions")
        cycles = counters.get("cycles")
        cache_misses = counters.get("cache-misses")
        branch_misses = counters.get("branch-misses")
        cache_refs = counters.get("cache-references")

        ipc = (
            (instructions / cycles)
            if (instructions and cycles and cycles > 0)
            else None
        )
        cpi = (
            (cycles / instructions)
            if (cycles and instructions and instructions > 0)
            else None
        )
        branch_miss_rate = None
        if branch_misses is not None and instructions and instructions > 0:
            branch_miss_rate = branch_misses / instructions

        out.append(
            ProfileResult(
                benchmark_id=b.bench_id,
                variant=v.name,
                tool="perf",
                ok=True,
                output_path=str(perf_out.relative_to(self.root)),
                metric_ir=instructions,
                metric_drefs=cache_refs,
                metric_d1_misses=cache_misses,
                metric_ll_misses=None,
                metric_instructions=instructions,
                metric_cycles=cycles,
                metric_ipc=ipc,
                metric_cpi=cpi,
                metric_branch_misses=branch_misses,
                metric_branch_miss_rate=branch_miss_rate,
                metric_cache_references=cache_refs,
            )
        )
        return out

    def _profile_xctrace(
        self, b: Benchmark, v: Variant, exe: Path, args: List[str]
    ) -> List[ProfileResult]:
        """Collect profile metrics using macOS-native xctrace CPU counters.

        Even though xctrace data is not callgrind-native, results are projected
        into the same logical metric slots so downstream tables remain stable.
        """
        out: List[ProfileResult] = []
        if not (self.config.xctrace_enabled and tool_exists("xcrun")):
            return out

        trace_path = self.profile_dir / f"{b.bench_id}__{v.name}.trace"

        # Remove previous trace bundle (xctrace refuses to overwrite)
        if trace_path.exists():
            shutil.rmtree(trace_path)

        cmd = [
            "xcrun", "xctrace", "record",
            "--template", self.config.xctrace_template,
            "--output", str(trace_path),
            "--launch", "--",
            str(exe),
        ] + args

        cp = run_cmd(
            cmd, cwd=self.root, timeout_s=self.config.xctrace_timeout_seconds
        )

        if cp.returncode != 0 or not trace_path.exists():
            err_msg = cp.stderr or "xctrace record failed"
            self._record_error("xctrace", b.bench_id, v.name, err_msg)
            out.append(
                ProfileResult(
                    benchmark_id=b.bench_id,
                    variant=v.name,
                    tool="xctrace",
                    ok=False,
                    output_path=str(trace_path),
                    error=err_msg,
                )
            )
            return out

        # Export per-process aggregated PMC data
        export_cmd = [
            "xcrun", "xctrace", "export",
            "--input", str(trace_path),
            "--xpath", XCTRACE_XPATH,
        ]
        cp2 = run_cmd(export_cmd, cwd=self.root, timeout_s=120)
        xml_text = cp2.stdout or ""

        ir, drefs, d1, ll = parse_xctrace_pmc_xml(xml_text)
        rel_path = str(trace_path.relative_to(self.root))

        # Derive CPU microarchitectural metrics from Apple bottleneck buckets:
        #   ir   (retiring)  = useful instruction work  (pipeline slots)
        #   drefs (frontend)  = instruction delivery stalls
        #   d1   (bad spec)   = branch misprediction waste
        #   ll   (backend)    = memory / execution-unit stalls
        all_vals = [x for x in [ir, drefs, d1, ll] if x is not None]
        total_slots = sum(all_vals) if all_vals else None
        instructions = ir          # retiring slots ≈ instruction count proxy
        cycles = total_slots       # total slots ≈ cycle count proxy
        ipc = (
            (instructions / cycles)
            if (instructions and cycles and cycles > 0)
            else None
        )
        cpi = (
            (cycles / instructions)
            if (cycles and instructions and instructions > 0)
            else None
        )
        branch_misses = d1         # bad speculation ≈ misprediction proxy
        branch_miss_rate = (
            (d1 / total_slots)
            if (d1 is not None and total_slots and total_slots > 0)
            else None
        )
        cache_refs = (
            ((drefs or 0) + (ll or 0))
            if (drefs is not None or ll is not None)
            else None
        )

        out.append(
            ProfileResult(
                benchmark_id=b.bench_id,
                variant=v.name,
                tool="xctrace",
                ok=True,
                output_path=rel_path,
                metric_ir=ir,
                metric_drefs=drefs,
                metric_d1_misses=d1,
                metric_ll_misses=ll,
                metric_instructions=instructions,
                metric_cycles=cycles,
                metric_ipc=ipc,
                metric_cpi=cpi,
                metric_branch_misses=branch_misses,
                metric_branch_miss_rate=branch_miss_rate,
                metric_cache_references=cache_refs,
            )
        )

        return out

    def _profile(
        self, b: Benchmark, v: Variant, exe: Path, args: List[str]
    ) -> List[ProfileResult]:
        """Dispatch to the configured profiling backend.

        ``auto`` mode selects ``xctrace`` on macOS and ``perf stat`` on Linux.
        ``none`` skips profiling entirely (returns empty list).
        """
        backend = self.config.profiler_backend

        if backend == "none":
            return []
        if backend == "perf":
            return self._profile_perf(b, v, exe, args)
        if backend == "xctrace":
            return self._profile_xctrace(b, v, exe, args)

        # "auto" — pick based on platform
        if sys.platform == "darwin":
            return self._profile_xctrace(b, v, exe, args)
        else:
            return self._profile_perf(b, v, exe, args)

    def run(self) -> int:
        """Execute the full benchmark pipeline and persist all result artifacts.

        High-level semantics:
        1) discover optimization space;
        2) compile each variant;
        3) measure runtime;
        4) collect profile metrics;
        5) write normalized outputs for later analysis.
        """
        self.preflight()

        first = self.benchmarks[0]
        ok_ir, ll_path, err = self._emit_ir(first)
        if not ok_ir:
            raise RuntimeError(
                f"Cannot emit IR for first benchmark '{first.bench_id}': {err}"
            )

        self.discovered_passes = self._discover_passes(ll_path)
        variants = self._build_variants()

        if self.config.randomize_execution_order:
            rnd = random.Random(self.config.seed)
            rnd.shuffle(self.benchmarks)

        print(f"[INFO] run_id={self.run_id}")
        print(f"[INFO] root={self.root}")
        print(f"[INFO] benchmarks={len(self.benchmarks)} variants={len(variants)}")
        print(
            f"[INFO] tools: hyperfine={tool_exists(self.config.hyperfine_bin)}"
            f" profiler_backend={self.config.profiler_backend}"
            f" perf={tool_exists(self.config.perf_bin)}"
            f" xctrace={tool_exists('xcrun')}"
        )

        for b in self.benchmarks:
            print(f"[INFO] benchmark={b.bench_id}")
            ok_ir, ll_path, err = self._emit_ir(b)
            if not ok_ir:
                self._record_error("emit_ir", b.bench_id, "ALL", err)
                if self.config.fail_fast:
                    break
                continue

            for v in variants:
                print(f"  [INFO] variant={v.name}")
                comp = self._compile_variant(b, ll_path, v)
                self.compile_results.append(comp)

                if not comp.compile_ok:
                    self._record_error("compile", b.bench_id, v.name, comp.error)
                    if self.config.fail_fast:
                        break
                    continue

                exe = self.root / comp.binary_path
                rr = self._measure_runtime_hyperfine(b, v, exe, b.run_args)
                if rr is None:
                    self._record_error(
                        "runtime",
                        b.bench_id,
                        v.name,
                        "hyperfine unavailable or failed",
                    )
                    if self.config.fail_fast:
                        break
                    continue
                rr.benchmark_id = b.bench_id
                rr.variant = v.name
                self.run_results.append(rr)

                self.profile_results.extend(
                    self._profile(b, v, exe, b.run_args)
                )

            if self.config.fail_fast and self.errors:
                break

        self._write_outputs(variants)
        return 0 if not self.errors else 2

    def _write_outputs(self, variants: List[Variant]) -> None:
        """Materialize raw records plus derived delta tables (CSV/JSON/Markdown).

        Output pipeline:
        1. Convert internal dataclass lists into row-dict lists for CSV.
        2. Build the **main table** by joining compile + runtime + profile
           data on the ``(benchmark_id, variant)`` key.
        3. Compute **deltas vs O0** and **incremental deltas** (vs previous
           variant) for every metric in ``_DELTA_METRICS``.
        4. Compute statistical indicators: speedup ratio, Cohen's d
           effect-size, and Welch's t-test p-value for both delta types.
        5. Write all CSV files, JSON summary, incremental-deltas CSV, and
           markdown report into ``results/run_<run_id>/``.
        """
        pass_rows = [
            {
                "index": p["index"],
                "pass_name": p["pass_name"],
                "target": p["target"],
                "raw": p["raw"],
            }
            for p in self.discovered_passes
        ]

        compile_rows = [dataclasses.asdict(c) for c in self.compile_results]

        runtime_rows: List[Dict[str, Any]] = []
        for r in self.run_results:
            runtime_rows.append(
                {
                    "benchmark_id": r.benchmark_id,
                    "variant": r.variant,
                    "runs": r.runs,
                    "warmup_runs": r.warmup_runs,
                    "mean_seconds": r.mean_seconds,
                    "median_seconds": r.median_seconds,
                    "stddev_seconds": r.stddev_seconds,
                    "min_seconds": r.min_seconds,
                    "max_seconds": r.max_seconds,
                    "return_codes_ok": r.return_codes_ok,
                    "raw_seconds_json": json.dumps(r.raw_seconds),
                }
            )

        profile_rows = [dataclasses.asdict(p) for p in self.profile_results]

        # --- Step 1: Build lookup maps for join ---
        # Index compile results by (benchmark, variant) for O(1) lookup.
        c_map = {(c.benchmark_id, c.variant): c for c in self.compile_results}

        # Data-driven mapping: ProfileResult field -> output column name.
        # This mapping decouples the internal dataclass field names from
        # the CSV column names, allowing either to change independently.
        _PROFILE_COLS = {
            "metric_ir": "profile_ir",
            "metric_drefs": "profile_drefs",
            "metric_d1_misses": "profile_d1_misses",
            "metric_ll_misses": "profile_ll_misses",
            "metric_instructions": "profile_instructions",
            "metric_cycles": "profile_cycles",
            "metric_ipc": "profile_ipc",
            "metric_cpi": "profile_cpi",
            "metric_branch_misses": "profile_branch_misses",
            "metric_branch_miss_rate": "profile_branch_miss_rate",
            "metric_cache_references": "profile_cache_references",
        }

        # Index profile metrics by (benchmark, variant).  When the profiling
        # backend produces no counters (e.g. Docker without PMC access), the
        # values remain None — which CSV renders as empty cells.
        p_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for p in self.profile_results:
            key = (p.benchmark_id, p.variant)
            p_map.setdefault(key, {col: None for col in _PROFILE_COLS.values()})
            for src, dst in _PROFILE_COLS.items():
                val = getattr(p, src, None)
                if val is not None:
                    p_map[key][dst] = val

        # Build raw_seconds lookup for statistical analysis
        # (needed for CI, Cohen's d, p-value computations later).
        raw_map: Dict[Tuple[str, str], List[float]] = {}
        for r in self.run_results:
            raw_map[(r.benchmark_id, r.variant)] = r.raw_seconds

        # --- Step 2: Build the main analysis table ---
        # Join runtime, compile, and profile data into one row per
        # (benchmark, variant) pair.  This is the primary output table.
        main_rows: List[Dict[str, Any]] = []
        for r in self.run_results:
            c = c_map.get((r.benchmark_id, r.variant))
            pm = p_map.get((r.benchmark_id, r.variant), {})
            ci_lo, ci_hi = compute_ci_95(r.raw_seconds)
            row: Dict[str, Any] = {
                "benchmark_id": r.benchmark_id,
                "variant": r.variant,
                "variant_limit": parse_limit_from_variant_name(r.variant),
                "runtime_mean_seconds": r.mean_seconds,
                "runtime_median_seconds": r.median_seconds,
                "runtime_stddev_seconds": r.stddev_seconds if r.runs >= 2 else "",
                "runtime_ci95_lower": ci_lo,
                "runtime_ci95_upper": ci_hi,
                "compile_time_wall_seconds": c.compile_wall_seconds if c else None,
                "binary_size_bytes": c.binary_size_bytes if c else None,
                # IR / codegen structural metrics
                "ir_instruction_count": c.ir_instruction_count if c else None,
                "ir_basic_block_count": c.ir_basic_block_count if c else None,
                "text_section_size": c.text_section_size if c else None,
                "opt_wall_seconds": c.opt_wall_seconds if c else None,
            }
            # Add all profile columns dynamically
            for col in _PROFILE_COLS.values():
                row[col] = pm.get(col)
            main_rows.append(row)

        # --- Step 3: Compute deltas ---
        # Two kinds of delta are produced for every metric in _DELTA_METRICS:
        #   delta_vs_O0_<metric>   — absolute difference from the O0 baseline
        #   delta_prev_<metric>    — incremental difference from the previous variant
        # Additionally: speedup ratios, Cohen's d, and Welch's t p-values.
        # Rows within each benchmark are sorted by variant limit so that
        # O0 comes first, then O1_custom_0, O1_custom_1, …
        by_bench: Dict[str, List[Dict[str, Any]]] = {}
        for row in main_rows:
            by_bench.setdefault(row["benchmark_id"], []).append(row)

        for bench_id, rows in by_bench.items():

            def sort_key(x: Dict[str, Any]) -> Tuple[int, int]:
                """Sort O0 first, then variants by ascending numeric pass limit."""
                if x["variant"] == "O0":
                    return (0, -1)
                lim = x.get("variant_limit")
                return (1, lim if isinstance(lim, int) else 10**9)

            rows.sort(key=sort_key)

            base = next((r for r in rows if r["variant"] == "O0"), None)
            if base is not None:
                # Precompute baseline values for delta_vs_O0 columns
                base_vals = {
                    m: safe_float(base.get(m), None) for m in _DELTA_METRICS
                }
                base_rt = base_vals["runtime_mean_seconds"]
                base_raw = raw_map.get((bench_id, "O0"), [])

                for row in rows:
                    cur_vals = {
                        m: safe_float(row.get(m), None) for m in _DELTA_METRICS
                    }
                    # Emit delta_vs_O0_<metric> for every tracked metric
                    for m in _DELTA_METRICS:
                        row[f"delta_vs_O0_{m}"] = (
                            (cur_vals[m] - base_vals[m])
                            if (cur_vals[m] is not None and base_vals[m] is not None)
                            else None
                        )
                    cur_rt = cur_vals["runtime_mean_seconds"]
                    row["speedup_vs_O0"] = (
                        (base_rt / cur_rt)
                        if (cur_rt is not None and cur_rt != 0.0 and base_rt is not None)
                        else None
                    )
                    # Statistical stability: effect size and p-value vs O0
                    cur_raw = raw_map.get((row["benchmark_id"], row["variant"]), [])
                    row["effect_size_vs_O0"] = compute_cohens_d(base_raw, cur_raw)
                    row["pvalue_vs_O0"] = compute_welch_t_pvalue(base_raw, cur_raw)
            else:
                for row in rows:
                    for m in _DELTA_METRICS:
                        row[f"delta_vs_O0_{m}"] = None
                    row["speedup_vs_O0"] = None
                    row["effect_size_vs_O0"] = None
                    row["pvalue_vs_O0"] = None

            prev: Optional[Dict[str, Any]] = None
            for row in rows:
                if prev is None:
                    # First ordered row has no predecessor for incremental deltas.
                    for m in _DELTA_METRICS:
                        row[f"delta_prev_{m}"] = None
                    row["speedup_vs_prev"] = None
                    row["effect_size_vs_prev"] = None
                    row["pvalue_vs_prev"] = None
                    row["prev_variant"] = ""
                else:
                    row["prev_variant"] = prev["variant"]
                    cur_vals = {
                        m: safe_float(row.get(m), None) for m in _DELTA_METRICS
                    }
                    prv_vals = {
                        m: safe_float(prev.get(m), None) for m in _DELTA_METRICS
                    }
                    for m in _DELTA_METRICS:
                        row[f"delta_prev_{m}"] = (
                            (cur_vals[m] - prv_vals[m])
                            if (cur_vals[m] is not None and prv_vals[m] is not None)
                            else None
                        )
                    cur_rt = cur_vals["runtime_mean_seconds"]
                    prv_rt = prv_vals["runtime_mean_seconds"]
                    row["speedup_vs_prev"] = (
                        (prv_rt / cur_rt)
                        if (cur_rt is not None and cur_rt != 0.0 and prv_rt is not None)
                        else None
                    )
                    # Statistical comparison with previous variant
                    cur_raw = raw_map.get((row["benchmark_id"], row["variant"]), [])
                    prv_raw = raw_map.get((row["benchmark_id"], prev["variant"]), [])
                    row["effect_size_vs_prev"] = compute_cohens_d(prv_raw, cur_raw)
                    row["pvalue_vs_prev"] = compute_welch_t_pvalue(prv_raw, cur_raw)
                prev = row

        # --- Step 4: Flatten and sort rows for deterministic output ---
        main_rows = [r for rows in by_bench.values() for r in rows]
        main_rows.sort(
            key=lambda x: (
                x["benchmark_id"],
                0 if x["variant"] == "O0" else 1,
                x["variant_limit"] if isinstance(x["variant_limit"], int) else 10**9,
            )
        )

        error_rows = list(self.errors)

        # --- Step 5: Write CSV files ---
        if self.config.write_csv:
            run_dir = self.config.results_dir / f"run_{self.run_id}"
            run_dir.mkdir(parents=True, exist_ok=True)
            # Embed the run_id in each filename for traceability.
            def with_runid(path):
                """Suffix the run_id into *path* for traceability."""
                return run_dir / f"{path.stem}_{self.run_id}{path.suffix}"
            csv_write(with_runid(self.config.passes_table), pass_rows)
            csv_write(with_runid(self.config.compile_table), compile_rows)
            csv_write(with_runid(self.config.runtime_table), runtime_rows)
            csv_write(with_runid(self.config.profile_table), profile_rows)
            csv_write(with_runid(self.config.main_table), main_rows)
            csv_write(with_runid(self.config.errors_table), error_rows)

        # --- Step 6: Write JSON summary ---
        if self.config.write_json:
            json_write(
                run_dir / f"run_{self.run_id}.json",
                {
                    "meta": {
                        "run_id": self.run_id,
                        "timestamp": now_iso(),
                        "config_path": str(self.config_path),
                        "root": str(self.root),
                        "python": sys.version,
                        "platform": platform.platform(),
                        "tools": {
                            "clang": self.config.clang,
                            "opt": self.config.opt,
                            "profiler_backend": self.config.profiler_backend,
                            "hyperfine_available": tool_exists(
                                self.config.hyperfine_bin
                            ),
                            "perf_available": tool_exists(self.config.perf_bin),
                            "xctrace_available": tool_exists("xcrun"),
                        },
                        "variants": [dataclasses.asdict(v) for v in variants],
                    },
                    "passes": pass_rows,
                    "compile": compile_rows,
                    "runtime": runtime_rows,
                    "profile": profile_rows,
                    "main": main_rows,
                    "errors": error_rows,
                },
            )

        # --- Step 7: Incremental delta-only CSV ---
        # A compact "step-by-step" view that only includes delta_prev
        # columns, making it easy to see each pass's marginal effect.
        incremental_rows = []
        for r in main_rows:
            # Compact "step-by-step" view highlighting prev-variant deltas.
            inc_row: Dict[str, Any] = {
                "benchmark_id": r["benchmark_id"],
                "variant": r["variant"],
                "prev_variant": r.get("prev_variant", ""),
                "variant_limit": r.get("variant_limit"),
            }
            for m in _DELTA_METRICS:
                inc_row[f"delta_prev_{m}"] = r.get(f"delta_prev_{m}")
            inc_row["speedup_vs_prev"] = r.get("speedup_vs_prev")
            inc_row["effect_size_vs_prev"] = r.get("effect_size_vs_prev")
            inc_row["pvalue_vs_prev"] = r.get("pvalue_vs_prev")
            incremental_rows.append(inc_row)
        run_dir = self.config.results_dir / f"run_{self.run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_write(run_dir / f"incremental_deltas_{self.run_id}.csv", incremental_rows)

        # --- Step 8: Summary markdown report ---
        md: List[str] = []
        md.append(f"# Run summary `{self.run_id}`")
        md.append("")
        md.append(f"- Timestamp: `{now_iso()}`")
        md.append(f"- Benchmarks: `{len(self.benchmarks)}`")
        md.append(f"- Discovered passes: `{len(self.discovered_passes)}`")
        md.append(f"- Compile records: `{len(self.compile_results)}`")
        md.append(f"- Runtime records: `{len(self.run_results)}`")
        md.append(f"- Profile records: `{len(self.profile_results)}`")
        md.append(f"- Errors: `{len(self.errors)}`")
        md.append("")
        md.append("## Top speedups vs O0")
        md.append("")
        scored = []
        for r in main_rows:
            sp = safe_float(r.get("speedup_vs_O0"), None)
            if sp is not None:
                scored.append((sp, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        for sp, row in scored[:15]:
            md.append(f"- `{row['benchmark_id']}` / `{row['variant']}`: `{sp:.4f}x`")
        write_text(
            run_dir / f"run_{self.run_id}.md", "\n".join(md) + "\n"
        )


# ============================= CLI ============================= #


def parse_int_list(s: str) -> List[int]:
    """Parse a comma-separated string of integers (e.g. ``"0,5,10,20"``)."""
    out = []
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        out.append(int(x))
    return out


def parse_str_list(s: str) -> List[str]:
    """Parse a comma-separated string of identifiers."""
    return [x.strip() for x in s.split(",") if x.strip()]


def parse_args() -> argparse.Namespace:
    """Define and parse all CLI arguments.

    Arguments are grouped into:
      * **Run controls** — measurement repetitions, warmup, timeouts.
      * **Variant controls** — step size, explicit limits, O0/full-O1 toggles.
      * **Selection / tools** — benchmark filter and tool toggles.
    """
    p = argparse.ArgumentParser(
        description="Incremental LLVM pass performance orchestrator"
    )
    p.add_argument(
        "--config", default="configs/benchmarks.toml", help="TOML config path"
    )

    # Run controls
    p.add_argument("--runs", type=int, default=None, help="Override measurement runs")
    p.add_argument("--warmup", type=int, default=None, help="Override warmup runs")
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Override global command timeout",
    )
    p.add_argument(
        "--run-timeout-seconds",
        type=int,
        default=None,
        help="Override per-binary runtime timeout",
    )
    p.add_argument(
        "--compile-timeout-seconds",
        type=int,
        default=None,
        help="Override compile timeout",
    )
    p.add_argument("--fail-fast", action="store_true", help="Stop on first error")
    p.add_argument(
        "--no-randomize",
        action="store_true",
        help="Disable benchmark order randomization",
    )
    p.add_argument("--seed", type=int, default=None, help="Override random seed")

    # Variant controls
    p.add_argument("--step", type=int, default=None, help="Bisect step size")
    p.add_argument(
        "--max-limit",
        type=int,
        default=None,
        help="Max bisect limit (clamps discovered max)",
    )
    p.add_argument(
        "--explicit-limits",
        type=str,
        default=None,
        help="Comma-separated explicit limits, e.g. 0,5,10,20",
    )
    p.add_argument(
        "--no-o0", action="store_true", help="Do not include O0 baseline variant"
    )
    p.add_argument(
        "--no-full-o1", action="store_true", help="Do not auto-include full O1 variant"
    )

    # Selection / tools
    p.add_argument(
        "--benchmarks",
        type=str,
        default=None,
        help="Comma-separated benchmark IDs to run",
    )
    p.add_argument(
        "--disable-profiler",
        action="store_true",
        help="Skip profiling entirely",
    )

    return p.parse_args()


def make_overrides(args: argparse.Namespace) -> Overrides:
    """Translate parsed CLI arguments into an ``Overrides`` dataclass."""
    explicit_limits = (
        parse_int_list(args.explicit_limits) if args.explicit_limits else None
    )
    benchmarks = parse_str_list(args.benchmarks) if args.benchmarks else None
    return Overrides(
        runs=args.runs,
        warmup=args.warmup,
        timeout_seconds=args.timeout_seconds,
        run_timeout_seconds=args.run_timeout_seconds,
        compile_timeout_seconds=args.compile_timeout_seconds,
        step=args.step,
        max_limit=args.max_limit,
        explicit_limits=explicit_limits,
        include_o0=False if args.no_o0 else None,
        include_full_o1=False if args.no_full_o1 else None,
        fail_fast=True if args.fail_fast else None,
        randomize_execution_order=False if args.no_randomize else None,
        seed=args.seed,
        benchmarks=benchmarks,
        disable_profiler=args.disable_profiler,
    )


def main() -> int:
    """Entry point: parse CLI, create orchestrator, execute pipeline.

    Returns 0 on success, 1 on fatal errors, or 2 if some
    benchmarks / variants encountered recoverable errors.
    """
    args = parse_args()
    cfg = Path(args.config)
    if not cfg.exists():
        eprint(f"[FATAL] config not found: {cfg}")
        return 1

    orch = Orchestrator(config_path=cfg, overrides=make_overrides(args))
    try:
        return orch.run()
    except Exception as ex:
        eprint(f"[FATAL] {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
