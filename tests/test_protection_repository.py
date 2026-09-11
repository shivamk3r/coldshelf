import sqlite3
import stat

import pytest

from coldshelf.protection.repository import ProtectionRepository
from coldshelf.protection.service import ProtectionError, _safe_source_path


def test_schema_generation_replica_status_and_operation_lifecycle(tmp_path):
    database_path = tmp_path / "protected.sqlite3"
    repository = ProtectionRepository(database_path)
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    source = tmp_path / "source"
    source.mkdir()
    folder = repository.create_folder("folder", "Large files", source, 2, True)
    generation = repository.save_generation(
        folder["id"],
        "digest",
        [
            {
                "relative_path": "nested/file.bin",
                "file_type": "file",
                "size": 12,
                "mtime_ns": 2,
                "ctime_ns": 3,
                "inode": 4,
                "device": 5,
            }
        ],
    )
    first = repository.add_drive(
        folder["id"],
        volume_uuid="uuid-1",
        volume_name="HDD1",
        filesystem="ExFAT",
        repo_relative_path="DiskSpaceManager/protected/folder/restic",
        repository_id="repo-1",
        total_bytes=100,
        free_bytes=90,
    )
    second = repository.add_drive(
        folder["id"],
        volume_uuid="uuid-2",
        volume_name="HDD2",
        filesystem="ExFAT",
        repo_relative_path="DiskSpaceManager/protected/folder/restic",
        repository_id="repo-2",
        total_bytes=100,
        free_bytes=90,
    )

    repository.mark_generation_replicated(
        generation["id"], first["id"], "snap-1", "structural_checked"
    )
    assert repository.folder_summary(folder["id"])["partial_files"] == 1
    repository.mark_generation_replicated(
        generation["id"], second["id"], "snap-2", "full_checked"
    )
    summary = repository.folder_summary(folder["id"])
    assert summary["protected_files"] == 1
    assert summary["pending_files"] == 0

    operation = repository.create_operation(folder["id"], "index", {})
    repository.start_operation(operation["id"])
    repository.update_operation_progress(operation["id"], 25, "working", 42)
    running = repository.get_operation(operation["id"])
    assert running["eta_seconds"] == 42
    repository.finish_operation(operation["id"], {"ok": True})
    completed = repository.get_operation(operation["id"])
    assert completed["status"] == "completed"
    assert completed["result"] == {"ok": True}
    assert completed["eta_seconds"] is None
    assert completed["diagnostics"]["last_progress_percent"] == 25.0
    assert completed["diagnostics"]["last_progress_message"] == "working"
    assert "duration_seconds" in completed["diagnostics"]


def test_unsupported_schema_version_fails_closed(tmp_path):
    db_path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(RuntimeError, match="Unsupported"):
        ProtectionRepository(db_path)


def test_schema_one_operation_history_migrates_in_place(tmp_path):
    db_path = tmp_path / "version-one.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE protection_operations (
            id TEXT PRIMARY KEY,
            protected_folder_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            drive_target_id TEXT,
            request_json TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            progress_percent REAL NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT 'Queued',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO protection_operations (
            id, protected_folder_id, kind, status, request_json,
            progress_percent, progress_message, created_at
        ) VALUES ('legacy-operation', 'folder', 'backup', 'queued', '{}', 0, 'Queued',
                  '2026-01-01T00:00:00Z');
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    repository = ProtectionRepository(db_path)
    operation = repository.get_operation("legacy-operation")

    assert operation["eta_seconds"] is None
    assert operation["diagnostics"] == {}
    with sqlite3.connect(db_path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2


def test_failed_cancelled_and_interrupted_operations_keep_diagnostics(tmp_path):
    repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    source = tmp_path / "source"
    source.mkdir()
    folder = repository.create_folder("folder", "Large files", source, 2, True)

    failed = repository.create_operation(folder["id"], "backup", {})
    repository.start_operation(failed["id"])
    repository.update_operation_progress(failed["id"], 55, "Processing files", 12)
    repository.fail_operation(
        failed["id"],
        "Restic failed",
        diagnostics={"exception_type": "ProtectionError", "restic_exit_code": 3},
    )
    failed_result = repository.get_operation(failed["id"])
    assert failed_result["status"] == "failed"
    assert failed_result["eta_seconds"] is None
    assert failed_result["diagnostics"]["last_progress_percent"] == 55.0
    assert failed_result["diagnostics"]["last_progress_message"] == "Processing files"
    assert failed_result["diagnostics"]["exception_type"] == "ProtectionError"
    assert failed_result["diagnostics"]["restic_exit_code"] == 3

    cancelled = repository.create_operation(folder["id"], "backup", {})
    repository.start_operation(cancelled["id"])
    repository.update_operation_progress(cancelled["id"], 10, "Starting transfer", 30)
    repository.request_cancel(cancelled["id"])
    repository.fail_operation(
        cancelled["id"],
        "Operation cancelled",
        cancelled=True,
        diagnostics={"exception_type": "OperationCancelled"},
    )
    cancelled_result = repository.get_operation(cancelled["id"])
    assert cancelled_result["status"] == "cancelled"
    assert cancelled_result["eta_seconds"] is None
    assert cancelled_result["diagnostics"]["exception_type"] == "OperationCancelled"
    assert cancelled_result["diagnostics"]["last_progress_message"] == "Starting transfer"

    interrupted = repository.create_operation(folder["id"], "backup", {})
    repository.start_operation(interrupted["id"])
    repository.update_operation_progress(interrupted["id"], 70, "Checking repository", 5)
    repository.fail_interrupted_operations()
    interrupted_result = repository.get_operation(interrupted["id"])
    assert interrupted_result["status"] == "failed"
    assert interrupted_result["eta_seconds"] is None
    assert interrupted_result["diagnostics"]["exception_type"] == "ProcessInterrupted"
    assert interrupted_result["diagnostics"]["last_progress_message"] == "Checking repository"


@pytest.mark.parametrize("relative", ["../escape", "/absolute", ""])
def test_catalog_paths_cannot_escape_source(tmp_path, relative):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ProtectionError):
        _safe_source_path(source, relative)


def test_protected_path_containment(tmp_path):
    repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    source = tmp_path / "source"
    source.mkdir()
    repository.create_folder("folder", "Large", source, 2)

    assert repository.is_protected_path(source / "nested" / "file.bin")
    assert not repository.is_protected_path(tmp_path / "source-other" / "file.bin")
