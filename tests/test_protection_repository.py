import sqlite3
import stat

import pytest

from disk_space_manager.protection.repository import ProtectionRepository
from disk_space_manager.protection.service import ProtectionError, _safe_source_path


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
    repository.update_operation_progress(operation["id"], 25, "working")
    repository.finish_operation(operation["id"], {"ok": True})
    completed = repository.get_operation(operation["id"])
    assert completed["status"] == "completed"
    assert completed["result"] == {"ok": True}


def test_unsupported_schema_version_fails_closed(tmp_path):
    db_path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(RuntimeError, match="Unsupported"):
        ProtectionRepository(db_path)


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
