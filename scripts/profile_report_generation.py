#!/usr/bin/env python3
"""Profile full-report generation against a generated benchmark tree."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_DIR = Path("downloads/benchmark")
DEFAULT_FILE_COUNT = 1_000_000
DEFAULT_MAX_BYTES = 2_000_000_000
DEFAULT_AGE_MONTHS = 6
DEFAULT_TOP_DIRS = 100
DEFAULT_SUBDIRS_PER_TOP = 100
DEFAULT_OLD_FILE_COUNT = 1_000
DEFAULT_RECENT_SPARSE_FILE_COUNT = 1_000
OLD_FILE_SIZE = 1024 * 1024 + 1


@dataclass(frozen=True)
class BenchmarkStats:
    """Summary of the generated benchmark dataset."""

    file_count: int
    directory_count: int
    logical_bytes: int
    old_file_count: int
    recent_sparse_file_count: int
    cache_file_count: int


@dataclass(frozen=True)
class ProfileResult:
    """Timing and dataset summary for one profiling run."""

    benchmark_dir: Path
    stats: BenchmarkStats
    initial_cleanup_seconds: float
    setup_seconds: float
    report_seconds: float
    final_cleanup_seconds: float
    kept_benchmark: bool


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a benchmark tree, profile full-report, and clean up."
    )
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        default=DEFAULT_BENCHMARK_DIR,
        help="Benchmark directory to own and delete/recreate (default: downloads/benchmark)",
    )
    parser.add_argument(
        "--file-count",
        type=int,
        default=DEFAULT_FILE_COUNT,
        help="Number of files to generate (default: 1000000)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="Maximum logical bytes across generated files (default: 2000000000)",
    )
    parser.add_argument(
        "--age-months",
        type=int,
        default=DEFAULT_AGE_MONTHS,
        help="Age threshold passed to full-report (default: 6)",
    )
    parser.add_argument(
        "--keep-benchmark",
        action="store_true",
        help="Keep the generated benchmark directory after profiling.",
    )
    return parser.parse_args(argv)


def resolve_benchmark_dir(benchmark_dir: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve and validate the benchmark directory before deletion."""
    repo_root = repo_root.resolve()
    candidate = benchmark_dir
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if candidate.exists() and candidate.is_symlink():
        raise ValueError(f"Refusing to use symlink benchmark path: {candidate}")
    resolved = candidate.resolve()

    filesystem_root = Path(resolved.anchor).resolve()
    forbidden_paths = {repo_root, Path.home().resolve(), filesystem_root}
    if resolved in forbidden_paths:
        raise ValueError(f"Refusing to use unsafe benchmark path: {resolved}")

    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to use benchmark path outside repository: {resolved}"
        ) from exc

    return resolved


def validate_inputs(
    file_count: int,
    max_bytes: int,
    age_months: int,
    top_dirs: int,
    subdirs_per_top: int,
    old_file_count: int,
    recent_sparse_file_count: int,
) -> None:
    if file_count < 0:
        raise ValueError("--file-count must be non-negative")
    if max_bytes < 0:
        raise ValueError("--max-bytes must be non-negative")
    if age_months < 0:
        raise ValueError("--age-months must be non-negative")
    if top_dirs <= 0:
        raise ValueError("top_dirs must be positive")
    if subdirs_per_top <= 0:
        raise ValueError("subdirs_per_top must be positive")
    if old_file_count < 0:
        raise ValueError("old_file_count must be non-negative")
    if recent_sparse_file_count < 0:
        raise ValueError("recent_sparse_file_count must be non-negative")


def remove_benchmark_dir(benchmark_dir: Path) -> float:
    """Remove the owned benchmark directory and return elapsed seconds."""
    start = time.perf_counter()
    if benchmark_dir.exists():
        if benchmark_dir.is_symlink():
            raise ValueError(f"Refusing to remove symlink: {benchmark_dir}")
        if not benchmark_dir.is_dir():
            raise ValueError(f"Benchmark path exists but is not a directory: {benchmark_dir}")
        shutil.rmtree(benchmark_dir)
    return time.perf_counter() - start


def create_sparse_file(path: Path, size: int) -> None:
    """Create a file with the requested logical size without writing payload bytes."""
    with path.open("wb") as handle:
        if size > 0:
            handle.seek(size - 1)
            handle.write(b"\0")


def compute_generation_plan(
    file_count: int,
    max_bytes: int,
    old_file_count: int = DEFAULT_OLD_FILE_COUNT,
    recent_sparse_file_count: int = DEFAULT_RECENT_SPARSE_FILE_COUNT,
) -> tuple[int, int, int, int, int]:
    """Return old/recent sparse counts and sizing details."""
    actual_old_count = min(old_file_count, file_count, max_bytes // OLD_FILE_SIZE)
    old_bytes = actual_old_count * OLD_FILE_SIZE
    remaining_count = file_count - actual_old_count
    remaining_budget = max_bytes - old_bytes

    actual_recent_sparse_count = min(
        recent_sparse_file_count,
        remaining_count,
        remaining_budget,
    )
    if actual_recent_sparse_count:
        recent_base_size = remaining_budget // actual_recent_sparse_count
        recent_extra_files = remaining_budget % actual_recent_sparse_count
    else:
        recent_base_size = 0
        recent_extra_files = 0

    return (
        actual_old_count,
        actual_recent_sparse_count,
        recent_base_size,
        recent_extra_files,
        old_bytes,
    )


def generate_benchmark_tree(
    benchmark_dir: Path,
    file_count: int = DEFAULT_FILE_COUNT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    age_months: int = DEFAULT_AGE_MONTHS,
    top_dirs: int = DEFAULT_TOP_DIRS,
    subdirs_per_top: int = DEFAULT_SUBDIRS_PER_TOP,
    old_file_count: int = DEFAULT_OLD_FILE_COUNT,
    recent_sparse_file_count: int = DEFAULT_RECENT_SPARSE_FILE_COUNT,
    progress_every: int = 50_000,
) -> BenchmarkStats:
    """Create the benchmark directory tree and return generation stats."""
    validate_inputs(
        file_count,
        max_bytes,
        age_months,
        top_dirs,
        subdirs_per_top,
        old_file_count,
        recent_sparse_file_count,
    )

    benchmark_dir.mkdir(parents=True, exist_ok=False)
    leaf_dirs: list[Path] = []
    for top_index in range(top_dirs):
        top_dir = benchmark_dir / f"dir_{top_index:03d}"
        for sub_index in range(subdirs_per_top):
            leaf_dir = top_dir / f"sub_{sub_index:03d}"
            leaf_dir.mkdir(parents=True, exist_ok=True)
            leaf_dirs.append(leaf_dir)

    (
        actual_old_count,
        actual_recent_sparse_count,
        recent_base_size,
        recent_extra_files,
        old_bytes,
    ) = compute_generation_plan(
        file_count,
        max_bytes,
        old_file_count=old_file_count,
        recent_sparse_file_count=recent_sparse_file_count,
    )

    old_timestamp = time.time() - ((age_months * 30) + 30) * 24 * 60 * 60
    logical_bytes = 0
    cache_file_count = 0
    leaf_count = len(leaf_dirs)

    for file_index in range(file_count):
        leaf_dir = leaf_dirs[file_index % leaf_count]

        if file_index < actual_old_count:
            file_path = leaf_dir / f"old_{file_index:07d}.dat"
            create_sparse_file(file_path, OLD_FILE_SIZE)
            os.utime(file_path, (old_timestamp, old_timestamp))
            logical_bytes += OLD_FILE_SIZE
        else:
            recent_index = file_index - actual_old_count
            if recent_index < actual_recent_sparse_count:
                file_size = recent_base_size
                if recent_index < recent_extra_files:
                    file_size += 1
                if recent_index % 10 == 0:
                    file_path = leaf_dir / f"cache_{file_index:07d}.log"
                    cache_file_count += 1
                else:
                    file_path = leaf_dir / f"payload_{file_index:07d}.bin"
                create_sparse_file(file_path, file_size)
                logical_bytes += file_size
            else:
                if file_index % 10_000 == 0:
                    file_path = leaf_dir / f"temp_{file_index:07d}.tmp"
                    cache_file_count += 1
                else:
                    file_path = leaf_dir / f"file_{file_index:07d}.empty"
                create_sparse_file(file_path, 0)

        if progress_every and (file_index + 1) % progress_every == 0:
            print(f"Generated {file_index + 1:,}/{file_count:,} files...", flush=True)

    directory_count = 1 + top_dirs + (top_dirs * subdirs_per_top)
    return BenchmarkStats(
        file_count=file_count,
        directory_count=directory_count,
        logical_bytes=logical_bytes,
        old_file_count=actual_old_count,
        recent_sparse_file_count=actual_recent_sparse_count,
        cache_file_count=cache_file_count,
    )


def run_report(benchmark_dir: Path, age_months: int, repo_root: Path = REPO_ROOT) -> None:
    command = [
        sys.executable,
        "-m",
        "disk_space_manager",
        "full-report",
        "--path",
        str(benchmark_dir),
        "--age-months",
        str(age_months),
    ]
    print("\nProfiling command:")
    print(" ".join(command), flush=True)
    subprocess.run(command, cwd=repo_root, check=True)


def run_profile(
    benchmark_dir: Path = DEFAULT_BENCHMARK_DIR,
    file_count: int = DEFAULT_FILE_COUNT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    age_months: int = DEFAULT_AGE_MONTHS,
    keep_benchmark: bool = False,
    repo_root: Path = REPO_ROOT,
    top_dirs: int = DEFAULT_TOP_DIRS,
    subdirs_per_top: int = DEFAULT_SUBDIRS_PER_TOP,
    old_file_count: int = DEFAULT_OLD_FILE_COUNT,
    recent_sparse_file_count: int = DEFAULT_RECENT_SPARSE_FILE_COUNT,
) -> ProfileResult:
    """Generate the benchmark tree, profile full-report, and clean up."""
    validate_inputs(
        file_count,
        max_bytes,
        age_months,
        top_dirs,
        subdirs_per_top,
        old_file_count,
        recent_sparse_file_count,
    )
    repo_root = repo_root.resolve()
    benchmark_dir = resolve_benchmark_dir(benchmark_dir, repo_root=repo_root)

    print(f"Benchmark directory: {benchmark_dir}")
    print("Deleting any existing benchmark directory...", flush=True)
    initial_cleanup_seconds = remove_benchmark_dir(benchmark_dir)

    final_cleanup_seconds = 0.0
    try:
        print(f"Generating {file_count:,} files with <= {max_bytes:,} logical bytes...")
        setup_start = time.perf_counter()
        stats = generate_benchmark_tree(
            benchmark_dir,
            file_count=file_count,
            max_bytes=max_bytes,
            age_months=age_months,
            top_dirs=top_dirs,
            subdirs_per_top=subdirs_per_top,
            old_file_count=old_file_count,
            recent_sparse_file_count=recent_sparse_file_count,
        )
        setup_seconds = time.perf_counter() - setup_start

        report_start = time.perf_counter()
        run_report(benchmark_dir, age_months, repo_root=repo_root)
        report_seconds = time.perf_counter() - report_start
    finally:
        if keep_benchmark:
            print(f"Keeping benchmark directory: {benchmark_dir}", flush=True)
        else:
            print("Cleaning up benchmark directory...", flush=True)
            final_cleanup_seconds = remove_benchmark_dir(benchmark_dir)

    return ProfileResult(
        benchmark_dir=benchmark_dir,
        stats=stats,
        initial_cleanup_seconds=initial_cleanup_seconds,
        setup_seconds=setup_seconds,
        report_seconds=report_seconds,
        final_cleanup_seconds=final_cleanup_seconds,
        kept_benchmark=keep_benchmark,
    )


def print_summary(result: ProfileResult) -> None:
    print("\nProfiling summary")
    print("=================")
    print(f"Files generated:        {result.stats.file_count:,}")
    print(f"Directories generated:  {result.stats.directory_count:,}")
    print(f"Logical bytes:          {result.stats.logical_bytes:,}")
    print(f"Old sparse files:       {result.stats.old_file_count:,}")
    print(f"Recent sparse files:    {result.stats.recent_sparse_file_count:,}")
    print(f"Cache-like files:       {result.stats.cache_file_count:,}")
    print(f"Initial cleanup time:   {result.initial_cleanup_seconds:.2f}s")
    print(f"Setup time:             {result.setup_seconds:.2f}s")
    print(f"Report generation time: {result.report_seconds:.2f}s")
    print(f"Final cleanup time:     {result.final_cleanup_seconds:.2f}s")
    print(f"Benchmark kept:         {'yes' if result.kept_benchmark else 'no'}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_profile(
            benchmark_dir=args.benchmark_dir,
            file_count=args.file_count,
            max_bytes=args.max_bytes,
            age_months=args.age_months,
            keep_benchmark=args.keep_benchmark,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Profiling failed: {exc}", file=sys.stderr)
        return 1

    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
