"""Tests for progress bar and time remaining functionality."""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from click.testing import CliRunner

from scanner import DiskScanner
from analyzer import FileAnalyzer
from main import cli


def _make_test_files(tmp_path, count=200, prefix="file", ext=".txt", content="content"):
    """Create test files in a temporary directory."""
    for i in range(count):
        (tmp_path / f"{prefix}_{i}{ext}").write_text(content)


def _make_file_info_list(tmp_path, count=200, ext=".cache", age_days=400, size=100):
    """Create a list of file info dicts for testing analyzer methods."""
    now = datetime.now()
    ts = (now - timedelta(days=age_days)).timestamp()
    files = []
    for i in range(count):
        p = tmp_path / f"file_{i}{ext}"
        p.write_text("x" * size)
        files.append({
            "path": str(p),
            "size": size,
            "atime": ts,
            "mtime": ts,
            "ctime": ts,
        })
    return files


class TestScannerProgressCallback:
    """Tests for DiskScanner progress_callback."""

    @pytest.fixture(autouse=True)
    def _clear_excluded_dirs(self, monkeypatch):
        monkeypatch.setattr("scanner.EXCLUDED_DIRECTORIES", [])
        monkeypatch.setattr("scanner.USER_EXCLUDED_DIRECTORIES", [])

    def test_callback_is_invoked_during_scan(self, tmp_path):
        _make_test_files(tmp_path, count=250)
        callback = MagicMock()
        scanner = DiskScanner(tmp_path, progress_callback=callback)
        scanner.scan()
        assert callback.call_count > 0

    def test_callback_receives_increasing_counts(self, tmp_path):
        _make_test_files(tmp_path, count=350)
        counts = []
        scanner = DiskScanner(tmp_path, progress_callback=lambda c: counts.append(c))
        scanner.scan()
        assert len(counts) > 0, "Callback should have been called at least once"
        for i in range(1, len(counts)):
            assert counts[i] >= counts[i - 1], "Counts should be monotonically increasing"

    def test_scan_works_without_callback(self, tmp_path):
        _make_test_files(tmp_path, count=5)
        scanner = DiskScanner(tmp_path)
        result = scanner.scan()
        assert len(result["files"]) == 5

    def test_callback_not_called_for_empty_directory(self, tmp_path):
        callback = MagicMock()
        scanner = DiskScanner(tmp_path, progress_callback=callback)
        scanner.scan()
        assert callback.call_count == 0


class TestAnalyzerCacheProgressCallback:
    """Tests for FileAnalyzer.find_cache_files progress_callback."""

    def test_callback_is_invoked(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=200)
        callback = MagicMock()
        analyzer = FileAnalyzer()
        analyzer.find_cache_files(files, progress_callback=callback)
        assert callback.call_count > 0

    def test_callback_values_are_monotonically_increasing(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=500)
        values = []
        analyzer = FileAnalyzer()
        analyzer.find_cache_files(files, progress_callback=lambda n: values.append(n))
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1]

    def test_final_callback_equals_total_files(self, tmp_path):
        count = 350
        files = _make_file_info_list(tmp_path, count=count)
        values = []
        analyzer = FileAnalyzer()
        analyzer.find_cache_files(files, progress_callback=lambda n: values.append(n))
        assert values[-1] == count

    def test_works_without_callback(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=10)
        analyzer = FileAnalyzer()
        result = analyzer.find_cache_files(files)
        assert isinstance(result, list)

    def test_empty_file_list(self):
        callback = MagicMock()
        analyzer = FileAnalyzer()
        result = analyzer.find_cache_files([], progress_callback=callback)
        assert result == []
        assert callback.call_count == 0


class TestAnalyzerOldFilesProgressCallback:
    """Tests for FileAnalyzer.find_old_files progress_callback."""

    def test_callback_is_invoked(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=200, age_days=400)
        callback = MagicMock()
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))
        analyzer.find_old_files(files, min_size=0, progress_callback=callback)
        assert callback.call_count > 0

    def test_callback_values_are_monotonically_increasing(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=500, age_days=400)
        values = []
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))
        analyzer.find_old_files(files, min_size=0, progress_callback=lambda n: values.append(n))
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1]

    def test_final_callback_equals_total_files(self, tmp_path):
        count = 350
        files = _make_file_info_list(tmp_path, count=count, age_days=400)
        values = []
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))
        analyzer.find_old_files(files, min_size=0, progress_callback=lambda n: values.append(n))
        assert values[-1] == count

    def test_works_without_callback(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=10, age_days=400)
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))
        result = analyzer.find_old_files(files, min_size=0)
        assert isinstance(result, list)

    def test_empty_file_list(self):
        callback = MagicMock()
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))
        result = analyzer.find_old_files([], min_size=0, progress_callback=callback)
        assert result == []
        assert callback.call_count == 0


class TestProgressDoesNotAlterResults:
    """Verify that adding progress callbacks doesn't change analysis results."""

    def test_cache_results_same_with_and_without_callback(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=100)
        analyzer = FileAnalyzer()

        result_without = analyzer.find_cache_files(files)
        result_with = analyzer.find_cache_files(files, progress_callback=lambda n: None)

        assert len(result_without) == len(result_with)
        for a, b in zip(result_without, result_with):
            assert a["path"] == b["path"]
            assert a["size"] == b["size"]

    def test_old_files_results_same_with_and_without_callback(self, tmp_path):
        files = _make_file_info_list(tmp_path, count=100, age_days=400)
        analyzer = FileAnalyzer(age_threshold=timedelta(days=30))

        result_without = analyzer.find_old_files(files, min_size=0)
        result_with = analyzer.find_old_files(files, min_size=0, progress_callback=lambda n: None)

        assert len(result_without) == len(result_with)
        for a, b in zip(result_without, result_with):
            assert a["path"] == b["path"]
            assert a["size"] == b["size"]


class TestFullReportCommand:
    """Integration tests for the full-report command with progress."""

    @pytest.fixture(autouse=True)
    def _clear_excluded_dirs(self, monkeypatch):
        monkeypatch.setattr("scanner.EXCLUDED_DIRECTORIES", [])
        monkeypatch.setattr("scanner.USER_EXCLUDED_DIRECTORIES", [])

    def test_runs_successfully(self, tmp_path):
        _make_test_files(tmp_path, count=10)
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path)])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"

    def test_runs_with_age_months(self, tmp_path):
        _make_test_files(tmp_path, count=5)
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path), "--age-months", "1"])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"

    def test_runs_on_empty_directory(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path)])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"

    def test_runs_with_cache_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"temp_{i}.cache").write_text("cache content")
            (tmp_path / f"data_{i}.txt").write_text("real content")
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path)])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"

    def test_runs_with_nested_directories(self, tmp_path):
        sub = tmp_path / "subdir" / "nested"
        sub.mkdir(parents=True)
        for i in range(5):
            (sub / f"file_{i}.txt").write_text("content")
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path)])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"

    def test_runs_with_many_files(self, tmp_path):
        _make_test_files(tmp_path, count=250)
        runner = CliRunner()
        result = runner.invoke(cli, ["full-report", "--path", str(tmp_path)])
        assert result.exit_code == 0, f"Command failed: {result.output}\n{result.exception}"
