from pathlib import Path

import pytest

from disk_space_manager.protection.repository import ProtectionRepository
from disk_space_manager.protection.service import (
    ProtectionError,
    ProtectionService,
    _atomic_place_no_overwrite,
)

from .protection_fakes import FakeRestic, FakeVolumes, MemorySecretStore, fake_volume


def _service(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.bin").write_bytes(b"first version")
    (source / "nested").mkdir()
    (source / "nested" / "two.bin").write_bytes(b"second file")
    volume1 = fake_volume(tmp_path / "HDD1", "volume-1", "HDD1")
    volume2 = fake_volume(tmp_path / "HDD2", "volume-2", "HDD2")
    volumes = FakeVolumes([volume1, volume2])
    restic = FakeRestic()
    secrets = MemorySecretStore()
    repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    service = ProtectionService(repository, restic, secrets, volumes)
    return service, source, volumes, restic


def _create_and_enroll(service, source, volumes):
    created = service.create_folder("Large files", source, required_copies=2)
    service.confirm_recovery_key(created["id"], created["recovery_key"])
    volumes.mount_only("volume-1")
    first = service.enroll_drive(created["id"], Path(volumes.volumes["volume-1"].mount_path))
    volumes.mount_only("volume-2")
    second = service.enroll_drive(created["id"], Path(volumes.volumes["volume-2"].mount_path))
    return created, first, second


def test_sequential_disks_changed_file_gate_evict_and_restore_from_either(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "disk_space_manager.executor.ACTION_LOG_FILE", tmp_path / "actions.log"
    )
    service, source, volumes, _ = _service(tmp_path)
    folder, first, second = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])

    volumes.mount_only("volume-1")
    service.backup(folder["id"], first["id"], operation_id="backup-1")
    assert service.repository.folder_summary(folder["id"])["partial_files"] == 2

    # A change between one-disk sessions cannot accidentally receive replica credit.
    (source / "one.bin").write_bytes(b"changed between sessions")
    volumes.mount_only("volume-2")
    with pytest.raises(ProtectionError, match="changed"):
        service.backup(folder["id"], second["id"], operation_id="stale-backup")

    service.index_folder(folder["id"])
    service.backup(folder["id"], second["id"], operation_id="backup-2-new")
    preview = service.eviction_preview(folder["id"], all_files=True)
    assert preview["eligible_count"] == 1
    assert len(preview["blocked"]) == 1

    volumes.mount_only("volume-1")
    service.backup(folder["id"], first["id"], operation_id="backup-1-new")
    preview = service.eviction_preview(folder["id"], all_files=True)
    assert preview["eligible_count"] == 2
    assert not preview["blocked"]

    result = service.evict(
        folder["id"],
        all_files=True,
        dry_run=False,
        confirmation_phrase=preview["confirmation_phrase"],
        operation_id="evict",
    )
    assert result["evicted"] == 2
    assert not (source / "one.bin").exists()
    assert not (source / "one.bin").is_symlink()
    assert not (source / ".disk-space-manager").exists()
    assert len(result["evidence"][0]["receipts"]) == 2

    files = service.repository.list_all_files(folder["id"])
    first_file_id = next(item["id"] for item in files if item["relative_path"] == "one.bin")
    volumes.mount_only("volume-2")
    service.restore(
        folder["id"],
        file_ids=[first_file_id],
        drive_id=second["id"],
        operation_id="restore-2",
    )
    assert (source / "one.bin").read_bytes() == b"changed between sessions"

    preview = service.eviction_preview(folder["id"], file_ids=[first_file_id])
    service.evict(
        folder["id"],
        file_ids=[first_file_id],
        dry_run=False,
        confirmation_phrase=preview["confirmation_phrase"],
        operation_id="evict-again",
    )
    volumes.mount_only("volume-1")
    service.restore(
        folder["id"],
        file_ids=[first_file_id],
        drive_id=first["id"],
        operation_id="restore-1",
    )
    assert (source / "one.bin").read_bytes() == b"changed between sessions"


def test_structural_and_targeted_full_verification_are_distinct(tmp_path):
    service, source, volumes, restic = _service(tmp_path)
    folder, first, _ = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])
    volumes.mount_only("volume-1")
    service.backup(folder["id"], first["id"], operation_id="backup")
    file_id = service.repository.list_all_files(folder["id"])[0]["id"]

    service.verify(
        folder["id"],
        first["id"],
        full=True,
        file_ids=[file_id],
        operation_id="verify",
    )
    replica = service.repository.get_file_replicas(file_id)[0]
    assert replica["status"] == "full_checked"
    assert replica["full_verified_at"]
    assert restic.check_calls[-1][1] is False
    drive = service.repository.get_drive(first["id"])
    assert drive["last_full_check_at"] is None


def test_restore_never_overwrites_existing_path(tmp_path):
    service, source, volumes, _ = _service(tmp_path)
    folder, first, second = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])
    volumes.mount_only("volume-1")
    service.backup(folder["id"], first["id"], operation_id="one")
    volumes.mount_only("volume-2")
    service.backup(folder["id"], second["id"], operation_id="two")
    file_id = service.repository.list_all_files(folder["id"])[0]["id"]

    with pytest.raises(ProtectionError, match="never overwrites"):
        service.restore(folder["id"], file_ids=[file_id], operation_id="restore")


def test_atomic_restore_placement_refuses_a_racing_destination(tmp_path):
    staged = tmp_path / "staged.bin"
    target = tmp_path / "target.bin"
    staged.write_bytes(b"restored data")
    target.write_bytes(b"existing data")

    with pytest.raises(FileExistsError):
        _atomic_place_no_overwrite(staged, target)

    assert staged.read_bytes() == b"restored data"
    assert target.read_bytes() == b"existing data"


def test_disconnect_repository_replacement_and_drive_rename_fail_safely(tmp_path):
    service, source, volumes, restic = _service(tmp_path)
    folder, first, _ = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])

    volumes.mount_only()
    with pytest.raises(ProtectionError, match="not connected"):
        service.backup(folder["id"], first["id"], operation_id="offline")

    volumes.mount_only("volume-1")
    volume = volumes.volumes["volume-1"]
    renamed = type(volume)(**{**volume.to_dict(), "name": "Renamed HDD1"})
    volumes.volumes["volume-1"] = renamed
    detail = service.folder_detail(folder["id"])
    renamed_drive = next(item for item in detail["drives"] if item["id"] == first["id"])
    assert renamed_drive["volume_name"] == "Renamed HDD1"

    repository_path = (
        Path(renamed.mount_path) / first["repo_relative_path"]
    )
    restic.repository_ids[str(repository_path)] = "replacement-repository"
    with pytest.raises(ProtectionError, match="identity"):
        service.backup(folder["id"], first["id"], operation_id="replacement")


def test_reconciles_file_atomically_staged_by_interrupted_eviction(tmp_path):
    service, source, volumes, _ = _service(tmp_path)
    folder, _, _ = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])
    file_info = next(
        item
        for item in service.repository.list_all_files(folder["id"])
        if item["relative_path"] == "one.bin"
    )
    original = source / "one.bin"
    staged = source / ".disk-space-manager" / "eviction-staging" / "crash" / "one.bin"
    staged.parent.mkdir(parents=True)
    service.repository.set_file_state(file_info["id"], "evicting", staged)
    original.replace(staged)

    service.reconcile_interrupted_file_operations()
    assert original.read_bytes() == b"first version"
    assert not (source / ".disk-space-manager").exists()
    assert service.repository.get_file_versions(folder["id"], [file_info["id"]])[0][
        "local_state"
    ] == "local"


def test_enrollment_resumes_repository_initialized_before_catalog_commit(tmp_path):
    service, source, volumes, restic = _service(tmp_path)
    folder = service.create_folder("Large files", source, required_copies=2)
    service.confirm_recovery_key(folder["id"], folder["recovery_key"])
    volumes.mount_only("volume-1")
    volume = volumes.volumes["volume-1"]
    repository_path = (
        Path(volume.mount_path)
        / "DiskSpaceManager"
        / "protected"
        / folder["id"]
        / "restic"
    )
    initialized_id = restic.initialize_repository(repository_path, folder["id"])
    (repository_path / "config").write_text("initialized")

    drive = service.enroll_drive(folder["id"], Path(volume.mount_path))

    assert drive["repository_id"] == initialized_id
    marker = repository_path.parent / "repository.json"
    assert marker.exists()
    assert folder["id"] in marker.read_text()


def test_enrollment_marker_failure_rolls_back_and_is_retryable(tmp_path, monkeypatch):
    service, source, volumes, _ = _service(tmp_path)
    folder = service.create_folder("Large files", source, required_copies=2)
    service.confirm_recovery_key(folder["id"], folder["recovery_key"])
    volumes.mount_only("volume-1")
    volume = volumes.volumes["volume-1"]

    from disk_space_manager.protection import service as service_module

    real_writer = service_module._write_recovery_marker

    def fail_marker_write(*args):
        raise OSError("disconnected")

    monkeypatch.setattr(service_module, "_write_recovery_marker", fail_marker_write)
    with pytest.raises(ProtectionError, match="retry enrollment"):
        service.enroll_drive(folder["id"], Path(volume.mount_path))
    assert service.repository.list_drives(folder["id"]) == []

    monkeypatch.setattr(service_module, "_write_recovery_marker", real_writer)
    drive = service.enroll_drive(folder["id"], Path(volume.mount_path))
    assert drive["volume_uuid"] == "volume-1"
