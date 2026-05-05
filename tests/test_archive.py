"""Tests for archive to local folder functionality."""

import os
import time
import pytest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from click.testing import CliRunner

from disk_space_manager.cli import cli
from disk_space_manager.scanner import DiskScanner
from disk_space_manager.executor import ActionExecutor


def _create_old_large_file(path, size_bytes=1024 * 1024 + 1, age_days=200):
    """Create a file with old access/modify time and specified size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * size_bytes)
    old_time = time.time() - (age_days * 24 * 60 * 60)
    os.utime(path, (old_time, old_time))


def _create_recent_file(path, size_bytes=1024 * 1024 + 1):
    """Create a file with current timestamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * size_bytes)


class TestArchiveToLocalFolder:
    """CLI integration tests for archive --target-path."""

    @pytest.fixture(autouse=True)
    def _clear_excluded_dirs(self, monkeypatch):
        monkeypatch.setattr("disk_space_manager.scanner.EXCLUDED_DIRECTORIES", [])
        monkeypatch.setattr("disk_space_manager.scanner.USER_EXCLUDED_DIRECTORIES", [])

    def test_dry_run_with_target_path(self, tmp_path):
        """Dry-run archive to local folder reports files but doesn't move them."""
        src = tmp_path / "source"
        target = tmp_path / "archive_dest"
        _create_old_large_file(src / "old_file.dat")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--target-path", str(target),
            "--age-months", "1",
        ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        assert (src / "old_file.dat").exists()

    def test_creates_target_directory(self, tmp_path):
        """Target directory is created automatically when it doesn't exist."""
        src = tmp_path / "source"
        target = tmp_path / "brand_new_dir"
        _create_old_large_file(src / "old_file.dat")
        assert not target.exists()

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--target-path", str(target),
            "--age-months", "1",
        ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        assert target.exists()

    def test_moves_files_to_local_folder(self, tmp_path):
        """Files are physically moved to the local archive folder."""
        src = tmp_path / "source"
        target = tmp_path / "archive_dest"
        _create_old_large_file(src / "old_report.dat", age_days=200)

        runner = CliRunner()
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            result = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        archived = target / "archived_files" / "old_report.dat"
        assert archived.exists()

    def test_no_old_files(self, tmp_path):
        """Command exits cleanly when no files meet the age threshold."""
        src = tmp_path / "source"
        target = tmp_path / "archive_dest"
        _create_recent_file(src / "recent.dat")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--target-path", str(target),
            "--age-months", "6",
        ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"

    def test_preserves_directory_structure(self, tmp_path):
        """Subdirectory structure is preserved in the archive."""
        src = tmp_path / "source"
        target = tmp_path / "archive_dest"
        _create_old_large_file(src / "sub" / "nested" / "data.dat", age_days=200)

        runner = CliRunner()
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            result = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        archived = target / "archived_files" / "sub" / "nested" / "data.dat"
        assert archived.exists()

    def test_target_path_takes_precedence_over_external_path(self, tmp_path):
        """--target-path is used when both archive target options are given."""
        src = tmp_path / "source"
        local_target = tmp_path / "local_dest"
        external_target = tmp_path / "external_dest"
        external_target.mkdir()
        _create_old_large_file(src / "old_file.dat")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--target-path", str(local_target),
            "--external-path", str(external_target),
            "--age-months", "1",
        ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        assert local_target.exists()

    def test_external_path_works(self, tmp_path):
        """--external-path can be used as the archive destination."""
        src = tmp_path / "source"
        external_target = tmp_path / "external_dest"
        external_target.mkdir()
        _create_old_large_file(src / "old_file.dat")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--external-path", str(external_target),
            "--age-months", "1",
        ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"

    def test_external_path_must_be_writable(self, tmp_path, monkeypatch):
        """--external-path must point to a writable mounted destination."""
        src = tmp_path / "source"
        external_target = tmp_path / "external_dest"
        external_target.mkdir()
        _create_old_large_file(src / "old_file.dat")
        monkeypatch.setattr("disk_space_manager.archive_targets.is_writable_path", lambda path: False)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "--dry-run", "archive",
            "--path", str(src),
            "--external-path", str(external_target),
            "--age-months", "1",
        ])

        assert result.exit_code == 1
        assert "does not exist" in result.output
        assert "writable" in result.output

    def test_archive_help_uses_external_path_not_ssd_path(self):
        """The breaking CLI cleanup exposes only the generic external path flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["archive", "--help"])

        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        assert "--external-path" in result.output
        assert "--ssd-path" not in result.output

    def test_multiple_files_archived(self, tmp_path):
        """Multiple old files are all moved to the archive."""
        src = tmp_path / "source"
        target = tmp_path / "archive_dest"
        _create_old_large_file(src / "a.dat", age_days=200)
        _create_old_large_file(src / "b.dat", age_days=300)
        _create_old_large_file(src / "c.dat", age_days=250)

        runner = CliRunner()
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            result = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"
        for name in ("a.dat", "b.dat", "c.dat"):
            assert (target / "archived_files" / name).exists()


    def test_archive_inside_scan_path_excludes_already_archived(self, tmp_path):
        """Files in the archive folder are NOT re-archived when the archive
        target is a subdirectory of the scan path."""
        src = tmp_path / "source"
        target = src / "my_archive"  # archive is INSIDE scan path

        # Create a normal old file that should be archived
        _create_old_large_file(src / "should_move.dat", age_days=200)

        # Pre-populate the archive folder with a previously archived file
        prev = target / "archived_files" / "already_here.dat"
        _create_old_large_file(prev, age_days=400)

        runner = CliRunner()
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            result = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert result.exit_code == 0, f"Failed: {result.output}\n{result.exception}"

        # should_move.dat must have been archived
        assert (target / "archived_files" / "should_move.dat").exists()
        # already_here.dat must still be in place (not moved into a nested archive)
        assert (target / "archived_files" / "already_here.dat").exists()
        # No nested archive directory should have been created
        assert not (target / "archived_files" / "my_archive").exists()

    def test_repeated_archive_does_not_rearchive(self, tmp_path):
        """Running archive twice with target inside scan path doesn't move
        files that were already archived in the first run."""
        src = tmp_path / "source"
        target = src / "archive"  # archive is INSIDE scan path
        _create_old_large_file(src / "data.dat", age_days=200)

        runner = CliRunner()

        # First archive run
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            r1 = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert r1.exit_code == 0, f"Run 1 failed: {r1.output}\n{r1.exception}"
        assert (target / "archived_files" / "data.dat").exists()

        archived_after_first = set(
            p.name for p in (target / "archived_files").rglob("*") if p.is_file()
        )

        # Second archive run — nothing new should be archived
        with patch("disk_space_manager.ui.Confirm.ask", return_value=True):
            r2 = runner.invoke(cli, [
                "archive",
                "--path", str(src),
                "--target-path", str(target),
                "--age-months", "1",
            ])
        assert r2.exit_code == 0, f"Run 2 failed: {r2.output}\n{r2.exception}"

        archived_after_second = set(
            p.name for p in (target / "archived_files").rglob("*") if p.is_file()
        )
        assert archived_after_first == archived_after_second


class TestScannerExcludePaths:
    """Tests for DiskScanner exclude_paths feature."""

    @pytest.fixture(autouse=True)
    def _clear_excluded_dirs(self, monkeypatch):
        monkeypatch.setattr("disk_space_manager.scanner.EXCLUDED_DIRECTORIES", [])
        monkeypatch.setattr("disk_space_manager.scanner.USER_EXCLUDED_DIRECTORIES", [])

    def test_exclude_paths_skips_directory(self, tmp_path):
        """Files in excluded paths are not scanned."""
        (tmp_path / "keep.txt").write_text("keep")
        excluded = tmp_path / "skip_me"
        excluded.mkdir()
        (excluded / "hidden.txt").write_text("hidden")

        scanner = DiskScanner(tmp_path, exclude_paths=[excluded])
        result = scanner.scan()

        scanned_names = {os.path.basename(f["path"]) for f in result["files"]}
        assert "keep.txt" in scanned_names
        assert "hidden.txt" not in scanned_names

    def test_exclude_paths_skips_nested(self, tmp_path):
        """Nested directories under excluded paths are also skipped."""
        (tmp_path / "top.txt").write_text("top")
        deep = tmp_path / "archive" / "sub" / "deep"
        deep.mkdir(parents=True)
        (deep / "buried.txt").write_text("buried")

        scanner = DiskScanner(tmp_path, exclude_paths=[tmp_path / "archive"])
        result = scanner.scan()

        scanned_names = {os.path.basename(f["path"]) for f in result["files"]}
        assert "top.txt" in scanned_names
        assert "buried.txt" not in scanned_names

    def test_exclude_paths_no_effect_when_empty(self, tmp_path):
        """Scanner works normally when exclude_paths is empty."""
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        scanner = DiskScanner(tmp_path, exclude_paths=[])
        result = scanner.scan()
        assert len(result["files"]) == 2


class TestExecutorArchiveToLocalFolder:
    """Direct tests for ActionExecutor.archive_files with local folders."""

    def test_move_to_local_folder(self, tmp_path):
        src = tmp_path / "source"
        src.mkdir()
        target = tmp_path / "target" / "archived_files"

        test_file = src / "test.txt"
        test_file.write_text("test content")

        files = [{"path": test_file, "size": test_file.stat().st_size}]

        executor = ActionExecutor(dry_run=False)
        result = executor.archive_files(files, target, src, confirm=False)

        assert result["moved"] == 1
        assert result["failed"] == 0
        assert (target / "test.txt").exists()

    def test_preserves_file_content(self, tmp_path):
        src = tmp_path / "source"
        src.mkdir()
        target = tmp_path / "target" / "archived_files"

        test_file = src / "data.bin"
        content = b"important data content here"
        test_file.write_bytes(content)

        files = [{"path": test_file, "size": test_file.stat().st_size}]

        executor = ActionExecutor(dry_run=False)
        executor.archive_files(files, target, src, confirm=False)

        assert (target / "data.bin").read_bytes() == content

    def test_dry_run_does_not_move(self, tmp_path):
        src = tmp_path / "source"
        src.mkdir()
        target = tmp_path / "target" / "archived_files"

        test_file = src / "test.txt"
        test_file.write_text("test content")

        files = [{"path": test_file, "size": test_file.stat().st_size}]

        executor = ActionExecutor(dry_run=True)
        result = executor.archive_files(files, target, src, confirm=False)

        assert result["moved"] == 1
        assert test_file.exists()
        assert not target.exists()

    def test_preserves_subdirectory_structure(self, tmp_path):
        src = tmp_path / "source"
        target = tmp_path / "target" / "archived_files"

        test_file = src / "a" / "b" / "deep.txt"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("deep content")

        files = [{"path": test_file, "size": test_file.stat().st_size}]

        executor = ActionExecutor(dry_run=False)
        executor.archive_files(files, target, src, confirm=False)

        assert (target / "a" / "b" / "deep.txt").exists()
