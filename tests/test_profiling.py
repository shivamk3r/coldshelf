"""Tests for report-generation profiling helpers."""

from pathlib import Path

import pytest

from scripts import profile_report_generation as profiler


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def _logical_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def test_generate_benchmark_tree_respects_count_and_max_bytes(tmp_path):
    benchmark_dir = tmp_path / "benchmark"
    max_bytes = (profiler.OLD_FILE_SIZE * 2) + 1_234

    stats = profiler.generate_benchmark_tree(
        benchmark_dir,
        file_count=25,
        max_bytes=max_bytes,
        age_months=1,
        top_dirs=2,
        subdirs_per_top=2,
        old_file_count=2,
        recent_sparse_file_count=3,
        progress_every=0,
    )

    actual_bytes = _logical_bytes(benchmark_dir)
    assert _file_count(benchmark_dir) == 25
    assert actual_bytes <= max_bytes
    assert stats.file_count == 25
    assert stats.logical_bytes == actual_bytes
    assert stats.old_file_count == 2
    assert stats.directory_count == 7


def test_run_profile_cleans_up_after_success(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    benchmark_dir = repo_root / "downloads" / "benchmark"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "stale.txt").write_text("old data")

    called = {}

    def fake_run_report(path, age_months, repo_root):
        called["path"] = path
        called["age_months"] = age_months
        called["repo_root"] = repo_root

    monkeypatch.setattr(profiler, "run_report", fake_run_report)

    result = profiler.run_profile(
        benchmark_dir=Path("downloads/benchmark"),
        file_count=5,
        max_bytes=100,
        age_months=1,
        keep_benchmark=False,
        repo_root=repo_root,
        top_dirs=1,
        subdirs_per_top=1,
        old_file_count=0,
        recent_sparse_file_count=2,
    )

    assert called == {
        "path": benchmark_dir.resolve(),
        "age_months": 1,
        "repo_root": repo_root.resolve(),
    }
    assert result.stats.file_count == 5
    assert not benchmark_dir.exists()


def test_run_profile_keep_benchmark_preserves_generated_tree(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    benchmark_dir = repo_root / "downloads" / "benchmark"
    monkeypatch.setattr(profiler, "run_report", lambda path, age_months, repo_root: None)

    result = profiler.run_profile(
        benchmark_dir=Path("downloads/benchmark"),
        file_count=6,
        max_bytes=120,
        age_months=1,
        keep_benchmark=True,
        repo_root=repo_root,
        top_dirs=1,
        subdirs_per_top=2,
        old_file_count=0,
        recent_sparse_file_count=2,
    )

    assert result.kept_benchmark is True
    assert benchmark_dir.exists()
    assert _file_count(benchmark_dir) == 6


def test_run_profile_cleans_up_when_report_fails(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    benchmark_dir = repo_root / "downloads" / "benchmark"

    def fail_report(path, age_months, repo_root):
        raise RuntimeError("report failed")

    monkeypatch.setattr(profiler, "run_report", fail_report)

    with pytest.raises(RuntimeError, match="report failed"):
        profiler.run_profile(
            benchmark_dir=Path("downloads/benchmark"),
            file_count=4,
            max_bytes=80,
            age_months=1,
            keep_benchmark=False,
            repo_root=repo_root,
            top_dirs=1,
            subdirs_per_top=1,
            old_file_count=0,
            recent_sparse_file_count=1,
        )

    assert not benchmark_dir.exists()


def test_resolve_benchmark_dir_rejects_unsafe_paths(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError, match="unsafe benchmark path"):
        profiler.resolve_benchmark_dir(repo_root, repo_root=repo_root)

    with pytest.raises(ValueError, match="outside repository"):
        profiler.resolve_benchmark_dir(tmp_path / "outside", repo_root=repo_root)


def test_resolve_benchmark_dir_rejects_symlink(tmp_path):
    repo_root = tmp_path / "repo"
    target_dir = repo_root / "real_benchmark"
    link_path = repo_root / "downloads" / "benchmark"
    target_dir.mkdir(parents=True)
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to(target_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink benchmark path"):
        profiler.resolve_benchmark_dir(Path("downloads/benchmark"), repo_root=repo_root)
