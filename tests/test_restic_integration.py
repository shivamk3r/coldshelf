"""Optional real-Restic end-to-end test; all data stays in pytest temp paths."""

import os
import shutil
import uuid

import pytest

from disk_space_manager.protection.restic import ResticRunner
from disk_space_manager.protection.service import _validate_snapshot_manifest


RESTIC_BINARY = os.environ.get("RESTIC_TEST_BINARY") or shutil.which("restic")


class TemporaryPasswordRestic(ResticRunner):
    def _environment(self, repository, folder_id):
        environment = os.environ.copy()
        environment["RESTIC_PROGRESS_FPS"] = "2"
        environment["RESTIC_PASSWORD"] = "temporary-integration-test-password"
        if repository is not None:
            environment["RESTIC_REPOSITORY"] = str(repository)
        return environment


@pytest.mark.skipif(not RESTIC_BINARY, reason="Restic is not installed")
def test_real_restic_backup_check_catalog_and_restore(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "sample.bin").write_bytes(b"real restic round trip")
    (source / ".DS_Store").write_bytes(b"finder metadata")
    (source / "nested" / ".DS_Store").write_bytes(b"nested finder metadata")
    (source / ".project-config").write_text("keep this dotfile")
    repository = tmp_path / "repository"
    restore_target = tmp_path / "restore"
    folder_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    runner = TemporaryPasswordRestic(binary=str(RESTIC_BINARY))

    assert runner.preflight()["version"]
    initialized_id = runner.initialize_repository(repository, folder_id)
    assert runner.repository_id(repository, folder_id) == initialized_id
    snapshot_id = runner.backup(
        repository,
        folder_id,
        generation_id,
        source,
        operation_id="integration-backup",
    )
    entries = runner.list_snapshot(repository, folder_id, snapshot_id)
    assert [(item["relative_path"], item["size"]) for item in entries] == [
        (".project-config", len("keep this dotfile")),
        ("nested/sample.bin", len(b"real restic round trip")),
    ]
    source_stat = (source / "nested" / "sample.bin").stat()
    config_stat = (source / ".project-config").stat()
    _validate_snapshot_manifest(
        [
            {
                "relative_path": ".project-config",
                "size": config_stat.st_size,
                "mtime_ns": config_stat.st_mtime_ns,
            },
            {
                "relative_path": "nested/sample.bin",
                "size": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            }
        ],
        entries,
    )
    runner.check(repository, folder_id)
    runner.restore(
        repository,
        folder_id,
        snapshot_id,
        ["nested/sample.bin"],
        restore_target,
        operation_id="integration-restore",
    )
    assert (restore_target / "nested" / "sample.bin").read_bytes() == b"real restic round trip"
