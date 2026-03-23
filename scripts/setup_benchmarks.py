#!/usr/bin/env python3
"""
llvm-perf/scripts/setup_benchmarks.py

Clone benchmark suites (PolyBench + LLVM test-suite subset) and generate TOML
entries ready to paste into configs/benchmarks.toml.

Resilience features:
- PolyBench fallback git repositories
- Automatic tarball download + extraction fallback when git clone fails
  (e.g., when git prompts for credentials or HTTPS auth is blocked)

Usage examples:
  python3 scripts/setup_benchmarks.py
  python3 scripts/setup_benchmarks.py --only polybench
  python3 scripts/setup_benchmarks.py --only llvm-test-suite
  python3 scripts/setup_benchmarks.py --output configs/generated_benchmarks.toml
  python3 scripts/setup_benchmarks.py --no-clone
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# ----------------------------- Defaults ----------------------------- #

# Primary + fallbacks for PolyBench
POLYBENCH_GIT_CANDIDATES = [
    "https://github.com/MatthiasJReisinger/PolyBenchC-4.2",
    "https://github.com/MatthiasJReisinger/PolyBenchC-4.2.1",
    "https://github.com/daisytuner/PolyBench-C",
]

# Tarball fallback candidates for PolyBench (repo, branch)
POLYBENCH_TARBALL_CANDIDATES = [
    ("MatthiasJReisinger/PolyBenchC-4.2.1", "master"),
    ("MatthiasJReisinger/PolyBenchC-4.2.1", "main"),
    ("daisytuner/PolyBench-C", "main"),
    ("daisytuner/PolyBench-C", "master"),
]

LLVM_TEST_SUITE_REPO = "https://github.com/llvm/llvm-test-suite"
LLVM_TEST_SUITE_TARBALL = ("llvm/llvm-test-suite", "main")

DEFAULT_POLYBENCH_ROOT = Path("benchmarks/polybench")
DEFAULT_LLVM_TS_ROOT = Path("benchmarks/llvm-test-suite")

DEFAULT_LLVM_TS_SUBSET = [
    "SingleSource/Benchmarks/Shootout-C++/matrix.cpp",
    "SingleSource/Benchmarks/Misc/flops-3.c",
    "SingleSource/Benchmarks/Misc/mandel.c",
    "SingleSource/Benchmarks/Misc/fftbench.c",
    "SingleSource/Benchmarks/Misc/perlin.c",
]


@dataclass
class BenchEntry:
    bench_id: str
    source: str
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


# ----------------------------- Helpers ----------------------------- #


def run(cmd: Sequence[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def fail(msg: str, code: int = 1) -> "None":
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(code)


def sanitize_id(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


def detect_repo_root(start: Path) -> Path:
    p = start.resolve()
    if p.is_file():
        p = p.parent
    for cur in [p, *p.parents]:
        if (cur / "scripts").exists() and (cur / "configs").exists():
            return cur
    return start.resolve()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _looks_like_auth_failure(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "authentication failed",
        "invalid username or token",
        "password authentication is not supported",
        "could not read username",
        "terminal prompts disabled",
        "fatal: could not",
    ]
    return any(p in t for p in patterns)


def _tarball_url(repo: str, branch: str) -> str:
    return f"https://github.com/{repo}/archive/refs/heads/{branch}.tar.gz"


def _download_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "llvm-perf-setup-benchmarks/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _extract_tarball_to_dest(tar_data: bytes, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="llvm-perf-bench-") as td:
        tmp = Path(td)
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:gz") as tf:
            tf.extractall(tmp)

        top_dirs = [p for p in tmp.iterdir() if p.is_dir()]
        if len(top_dirs) != 1:
            fail("Unexpected tarball layout (could not detect top-level folder).")

        shutil.move(str(top_dirs[0]), str(dest))


def _has_expected_polybench_layout(dest: Path) -> bool:
    return (dest / "utilities/polybench.c").exists()


def _has_expected_llvm_ts_layout(dest: Path) -> bool:
    return (dest / "SingleSource/Benchmarks").exists()


def _try_git_clone(repo_url: str, dest: Path) -> Tuple[bool, str]:
    cp = run(["git", "clone", "--depth", "1", repo_url, str(dest)])
    out = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return cp.returncode == 0, out


def clone_or_update_polybench(dest: Path, do_clone: bool) -> None:
    if dest.exists() and (dest / ".git").exists():
        info(f"PolyBench already present: {dest} (fetching updates)")
        cp = run(["git", "fetch", "--all", "--tags"], cwd=dest)
        if cp.returncode != 0:
            warn(f"git fetch failed in {dest}: {cp.stderr.strip()}")
        return

    if dest.exists() and not (dest / ".git").exists():
        if _has_expected_polybench_layout(dest):
            info(f"PolyBench directory already available (non-git): {dest}")
            return
        warn(f"Destination exists but invalid PolyBench layout: {dest} (replacing)")
        shutil.rmtree(dest)

    if not do_clone:
        warn(f"Skipping PolyBench clone for {dest} (--no-clone)")
        return

    # 1) try git clone candidates
    last_err = ""
    auth_related = False
    for repo in POLYBENCH_GIT_CANDIDATES:
        info(f"Trying PolyBench git clone: {repo}")
        ok, out = _try_git_clone(repo, dest)
        if ok and _has_expected_polybench_layout(dest):
            info(f"PolyBench cloned successfully from {repo}")
            return
        if dest.exists() and not _has_expected_polybench_layout(dest):
            shutil.rmtree(dest)
        last_err = out.strip()
        if _looks_like_auth_failure(out):
            auth_related = True
            warn("PolyBench git clone encountered authentication/credential issues.")

    # 2) fallback to tarball download
    reason = "auth-related clone failure" if auth_related else "git clone failure"
    warn(f"Falling back to tarball download for PolyBench ({reason}).")

    for repo, branch in POLYBENCH_TARBALL_CANDIDATES:
        url = _tarball_url(repo, branch)
        try:
            info(f"Trying PolyBench tarball: {url}")
            data = _download_bytes(url)
            _extract_tarball_to_dest(data, dest)
            if _has_expected_polybench_layout(dest):
                info(f"PolyBench restored from tarball: {repo}@{branch}")
                return
            warn(f"Tarball extracted but PolyBench layout missing: {repo}@{branch}")
            if dest.exists():
                shutil.rmtree(dest)
        except Exception as ex:
            warn(f"PolyBench tarball failed ({repo}@{branch}): {ex}")

    fail(
        "Unable to obtain PolyBench via git or tarball fallback.\n"
        f"Last git error:\n{last_err}"
    )


def clone_or_update_llvm_test_suite(dest: Path, do_clone: bool) -> None:
    if dest.exists() and (dest / ".git").exists():
        info(f"LLVM test-suite already present: {dest} (fetching updates)")
        cp = run(["git", "fetch", "--all", "--tags"], cwd=dest)
        if cp.returncode != 0:
            warn(f"git fetch failed in {dest}: {cp.stderr.strip()}")
        return

    if dest.exists() and not (dest / ".git").exists():
        if _has_expected_llvm_ts_layout(dest):
            info(f"LLVM test-suite directory already available (non-git): {dest}")
            return
        warn(
            f"Destination exists but invalid LLVM test-suite layout: {dest} (replacing)"
        )
        shutil.rmtree(dest)

    if not do_clone:
        warn(f"Skipping LLVM test-suite clone for {dest} (--no-clone)")
        return

    info(f"Cloning LLVM test-suite: {LLVM_TEST_SUITE_REPO}")
    ok, out = _try_git_clone(LLVM_TEST_SUITE_REPO, dest)
    if ok and _has_expected_llvm_ts_layout(dest):
        return

    if dest.exists():
        shutil.rmtree(dest)

    # fallback tarball
    repo, branch = LLVM_TEST_SUITE_TARBALL
    url = _tarball_url(repo, branch)
    warn("LLVM test-suite git clone failed; trying tarball fallback.")
    try:
        data = _download_bytes(url)
        _extract_tarball_to_dest(data, dest)
        if _has_expected_llvm_ts_layout(dest):
            info(f"LLVM test-suite restored from tarball: {repo}@{branch}")
            return
    except Exception as ex:
        warn(f"LLVM test-suite tarball fallback failed: {ex}")

    fail(f"Unable to obtain LLVM test-suite.\nGit output:\n{out.strip()}")


# ----------------------------- Discovery: PolyBench ----------------------------- #


def find_polybench_kernels(poly_root: Path) -> List[Path]:
    if not poly_root.exists():
        return []

    kernels: List[Path] = []
    for p in poly_root.rglob("*.c"):
        rel = p.relative_to(poly_root).as_posix()
        if rel.startswith("utilities/") or "/utilities/" in rel:
            continue
        kernels.append(p)

    kernels.sort(key=lambda x: x.as_posix())
    return kernels


def polybench_entry_from_kernel(
    repo_root: Path, poly_root: Path, kernel_c: Path
) -> BenchEntry:
    rel_from_repo = kernel_c.relative_to(repo_root).as_posix()
    rel_from_poly = kernel_c.relative_to(poly_root).as_posix()
    parts = rel_from_poly.split("/")

    stem = Path(rel_from_poly).stem
    family = parts[0] if len(parts) > 1 else "polybench"
    bench_id = f"polybench_{sanitize_id('_'.join(parts[:-1]))}"

    util_polybench_c = (
        (poly_root / "utilities/polybench.c").relative_to(repo_root).as_posix()
    )
    include_paths = [
        str((poly_root / "utilities").relative_to(repo_root)).replace("\\", "/")
    ]

    return BenchEntry(
        bench_id=bench_id,
        source=rel_from_repo,
        language="c",
        compiler="clang",
        include_paths=include_paths,
        extra_sources=[util_polybench_c],
        compile_flags=["-std=c11", "-DPOLYBENCH_TIME", "-O0"],
        link_flags=["-lm"],
        run_args=[],
        expected_exit_code=0,
        enabled=True,
        tags=["polybench", family, stem],
    )


# ----------------------------- Discovery: LLVM test-suite ----------------------------- #


def llvm_ts_entry_from_source(repo_root: Path, src: Path) -> BenchEntry:
    rel = src.relative_to(repo_root).as_posix()
    ext = src.suffix.lower()

    is_cpp = ext in {".cc", ".cpp", ".cxx", ".c++"}
    language = "c++" if is_cpp else "c"
    compiler = "clang++" if is_cpp else "clang"

    rel_no_ext = src.relative_to(repo_root).with_suffix("").as_posix()
    bench_id = f"llvmts_{sanitize_id(rel_no_ext)}"

    tags = ["llvm-test-suite"]
    for key in ("Shootout", "Misc", "McCat", "Stanford", "Benchmarks"):
        if key.lower() in rel.lower():
            tags.append(key.lower())

    return BenchEntry(
        bench_id=bench_id,
        source=rel,
        language=language,
        compiler=compiler,
        include_paths=[],
        extra_sources=[],
        compile_flags=["-O0"],
        link_flags=["-lm"] if not is_cpp else [],
        run_args=[],
        expected_exit_code=0,
        enabled=True,
        tags=tags,
    )


def resolve_llvm_subset(
    llvm_root: Path, subset: Iterable[str]
) -> Tuple[List[Path], List[str]]:
    found: List[Path] = []
    missing: List[str] = []
    for rel in subset:
        p = llvm_root / rel
        if p.exists():
            found.append(p)
        else:
            missing.append(rel)
    return found, missing


# ----------------------------- TOML rendering ----------------------------- #


def toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def toml_str(s: str) -> str:
    return f'"{toml_escape(s)}"'


def toml_list_str(values: Sequence[str]) -> str:
    return "[" + ", ".join(toml_str(v) for v in values) + "]"


def render_entry(e: BenchEntry) -> str:
    lines: List[str] = []
    lines.append("[[benchmarks]]")
    lines.append(f"id = {toml_str(e.bench_id)}")
    lines.append(
        f"set = {toml_str('polybench' if 'polybench' in e.tags else 'llvm_test_suite')}"
    )
    lines.append(f"source = {toml_str(e.source)}")
    lines.append(f"language = {toml_str(e.language)}")
    lines.append(f"compiler = {toml_str(e.compiler)}")
    lines.append(f"include_paths = {toml_list_str(e.include_paths)}")
    lines.append(f"extra_sources = {toml_list_str(e.extra_sources)}")
    lines.append(f"compile_flags = {toml_list_str(e.compile_flags)}")
    lines.append(f"link_flags = {toml_list_str(e.link_flags)}")
    lines.append(f"run_args = {toml_list_str(e.run_args)}")
    lines.append('stdin_file = ""')
    lines.append(f"expected_exit_code = {e.expected_exit_code}")
    lines.append(f"enabled = {'true' if e.enabled else 'false'}")
    lines.append(f"tags = {toml_list_str(e.tags)}")
    lines.append("")
    return "\n".join(lines)


def render_header(poly_count: int, llvm_count: int) -> str:
    return (
        "# ------------------------------------------------------------------\n"
        "# Generated benchmark entries\n"
        "# File: scripts/setup_benchmarks.py\n"
        "# Paste these [[benchmarks]] blocks into configs/benchmarks.toml\n"
        "# ------------------------------------------------------------------\n"
        f"# polybench_entries = {poly_count}\n"
        f"# llvm_test_suite_entries = {llvm_count}\n"
        "\n"
    )


# ----------------------------- Main ----------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Setup PolyBench and LLVM test-suite benchmark entries for llvm-perf."
    )
    p.add_argument(
        "--only",
        choices=["polybench", "llvm-test-suite", "all"],
        default="all",
        help="Select which suite to process.",
    )
    p.add_argument(
        "--no-clone",
        action="store_true",
        help="Do not clone/update repositories; only inspect local directories.",
    )
    p.add_argument(
        "--polybench-root",
        default=str(DEFAULT_POLYBENCH_ROOT),
        help=f"PolyBench local path (default: {DEFAULT_POLYBENCH_ROOT})",
    )
    p.add_argument(
        "--llvm-test-suite-root",
        default=str(DEFAULT_LLVM_TS_ROOT),
        help=f"LLVM test-suite local path (default: {DEFAULT_LLVM_TS_ROOT})",
    )
    p.add_argument(
        "--max-polybench",
        type=int,
        default=25,
        help="Max number of PolyBench kernels to emit (default: 25).",
    )
    p.add_argument(
        "--llvm-subset",
        default=",".join(DEFAULT_LLVM_TS_SUBSET),
        help="Comma-separated llvm-test-suite source files (relative to suite root).",
    )
    p.add_argument(
        "--output",
        default="",
        help="Output file path for generated TOML snippet. If empty, print to stdout.",
    )
    p.add_argument(
        "--sort",
        action="store_true",
        help="Sort emitted entries by benchmark id.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    repo_root = detect_repo_root(Path.cwd())
    os.chdir(repo_root)

    if not (repo_root / "configs/benchmarks.toml").exists():
        warn("configs/benchmarks.toml not found; proceeding anyway.")

    do_clone = not args.no_clone
    only = args.only

    poly_root = (repo_root / args.polybench_root).resolve()
    llvm_root = (repo_root / args.llvm_test_suite_root).resolve()

    entries: List[BenchEntry] = []
    poly_count = 0
    llvm_count = 0

    if only in ("all", "polybench"):
        clone_or_update_polybench(poly_root, do_clone)

        kernels = find_polybench_kernels(poly_root)
        if not kernels:
            warn(f"No PolyBench kernels found under {poly_root}")
        if args.max_polybench > 0:
            kernels = kernels[: args.max_polybench]

        for k in kernels:
            entries.append(polybench_entry_from_kernel(repo_root, poly_root, k))
        poly_count = len(kernels)
        info(f"Prepared PolyBench entries: {poly_count}")

    if only in ("all", "llvm-test-suite"):
        clone_or_update_llvm_test_suite(llvm_root, do_clone)

        subset = [x.strip() for x in args.llvm_subset.split(",") if x.strip()]
        if not subset:
            subset = list(DEFAULT_LLVM_TS_SUBSET)

        found, missing = resolve_llvm_subset(llvm_root, subset)
        for rel in missing:
            warn(f"LLVM test-suite subset file missing: {rel}")

        for src in found:
            entries.append(llvm_ts_entry_from_source(repo_root, src))
        llvm_count = len(found)
        info(f"Prepared LLVM test-suite entries: {llvm_count}")

    if args.sort:
        entries.sort(key=lambda e: e.bench_id)

    body = render_header(poly_count, llvm_count) + "".join(
        render_entry(e) for e in entries
    )

    if args.output:
        out_path = (repo_root / args.output).resolve()
        ensure_parent(out_path)
        out_path.write_text(body, encoding="utf-8")
        info(f"Wrote generated TOML entries to: {out_path}")
    else:
        print(body)

    info("Next steps:")
    info("1) Review generated [[benchmarks]] entries")
    info("2) Paste them into configs/benchmarks.toml")
    info("3) Run: ./scripts/run_in_docker.sh -- --runs 10 --warmup 2 --step 10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
