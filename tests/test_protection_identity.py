"""Persistent local identity, legacy repair, and eviction regression coverage."""

import os
import sqlite3

import pytest

from coldshelf.protection.repository import ProtectionRepository
from coldshelf.protection.restic import ResticError
from coldshelf.protection.service import ProtectionError
from coldshelf.protection.volumes import VolumeError
from .test_protection_service import _service, _create_and_enroll


@pytest.fixture
def protected(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    service, source, volumes, restic = _service(tmp_path)
    folder, first, second = _create_and_enroll(service, source, volumes)
    service.index_folder(folder["id"])
    for volume, drive in [("volume-1", first), ("volume-2", second)]:
        volumes.mount_only(volume)
        service.backup(folder["id"], drive["id"], operation_id=f"backup-{volume}")
    service.refresh_drive_status()
    entry = next(e for e in service.repository.list_all_files(folder["id"])
                 if e["relative_path"] == "one.bin")
    return service, source, volumes, restic, folder, entry


def _legacy(service, entry):
    with sqlite3.connect(service.repository.db_path) as conn:
        conn.execute("UPDATE file_versions SET source_volume_uuid=NULL, device=device+1 WHERE id=?", (entry["id"],))


def test_device_renumbering_preserves_version_receipts_and_generation(protected):
    service, source, volumes, restic, folder, entry = protected
    original_generation = service.repository.get_folder(folder["id"])["current_generation_id"]
    receipts = service.repository.get_file_replicas(entry["id"])
    with sqlite3.connect(service.repository.db_path) as conn:
        conn.execute("UPDATE file_versions SET device=device+1")
    assert service.eviction_preview(folder["id"], [entry["id"]])["eligible_count"] == 1
    indexed = service.index_folder(folder["id"])
    assert indexed["generation"]["id"] == original_generation
    assert service.repository.get_file_replicas(entry["id"]) == receipts


def test_different_volume_blocks_even_with_identical_other_metadata(protected):
    service, source, volumes, restic, folder, entry = protected
    volumes.source_uuid = "replacement-volume"
    preview = service.eviction_preview(folder["id"], [entry["id"]])
    assert preview["eligible_count"] == 0
    assert "Source volume changed" in preview["blocked"][0]["reasons"][0]


def test_legacy_migration_and_verified_repair_preserve_receipts(protected, tmp_path):
    service, source, volumes, restic, folder, entry = protected
    receipts = service.repository.get_file_replicas(entry["id"])
    _legacy(service, entry)
    # Recreate the actual schema-2 shape, then migrate without trusting this disk.
    with sqlite3.connect(service.repository.db_path) as conn:
        conn.execute("ALTER TABLE file_versions DROP COLUMN source_volume_uuid")
        conn.execute("PRAGMA user_version=2")
    service.repository = ProtectionRepository(service.repository.db_path)
    migrated = service.repository.get_file_versions(folder["id"], [entry["id"]])[0]
    assert migrated["source_volume_uuid"] is None
    assert service.repository.get_file_replicas(entry["id"]) == receipts
    preview = service.eviction_preview(folder["id"], [entry["id"]])
    assert "Filesystem device number changed" in preview["blocked"][0]["reasons"][0]
    with pytest.raises(ProtectionError, match="Repair local identity"):
        service.index_folder(folder["id"])
    result = service.repair_local_identity(folder["id"], file_ids=[entry["id"]], operation_id="repair")
    assert result["repaired"] == 1
    assert result["files_deleted"] == 0
    assert (source / "one.bin").read_bytes() == b"first version"
    assert service.repository.get_file_replicas(entry["id"]) == receipts
    assert service.eviction_preview(folder["id"], [entry["id"]])["eligible_count"] == 1
    assert "REPAIR_IDENTITY" in (tmp_path / "actions.log").read_text()


@pytest.mark.parametrize("failure", ["offline", "corrupt", "changed", "race", "cancel", "unknown-volume"])
def test_failed_repair_never_refreshes_identity(protected, monkeypatch, failure):
    service, source, volumes, restic, folder, entry = protected
    _legacy(service, entry)
    original = service.repository.get_file_versions(folder["id"], [entry["id"]])[0]
    if failure == "offline":
        volumes.mount_only()
    elif failure == "corrupt":
        def corrupt(*args, **kwargs):
            raise ResticError("corrupt repository", 1)
        monkeypatch.setattr(restic, "hash_snapshot_file", corrupt)
    elif failure == "changed":
        # Same size and mtime must not be sufficient proof of identical bytes.
        (source / "one.bin").write_bytes(b"other version")
        os.utime(source / "one.bin", ns=(entry["mtime_ns"], entry["mtime_ns"]))
    elif failure == "race":
        real_hash = restic.hash_snapshot_file
        def race(*args, **kwargs):
            result = real_hash(*args, **kwargs)
            (source / "one.bin").write_bytes(b"racing writer")
            return result
        monkeypatch.setattr(restic, "hash_snapshot_file", race)
    elif failure == "unknown-volume":
        def unknown(*args):
            raise VolumeError("UUID unavailable")
        monkeypatch.setattr(volumes, "source_volume_uuid", unknown)
    def progress(*args):
        if failure == "cancel":
            raise ProtectionError("Operation cancelled")
    with pytest.raises(ProtectionError):
        service.repair_local_identity(folder["id"], file_ids=[entry["id"]],
                                      operation_id="repair", progress=progress)
    assert service.repository.get_file_versions(folder["id"], [entry["id"]])[0] == original
    assert (source / "one.bin").exists()


def test_restore_rebinds_local_instance_and_keeps_content_receipts(protected):
    service, source, volumes, restic, folder, entry = protected
    receipts = service.repository.get_file_replicas(entry["id"])
    preview = service.eviction_preview(folder["id"], [entry["id"]])
    service.evict(folder["id"], file_ids=[entry["id"]], dry_run=False,
                  confirmation_phrase=preview["confirmation_phrase"], operation_id="evict")
    volumes.source_uuid = "new-mac-volume"
    service.restore(folder["id"], file_ids=[entry["id"]], operation_id="restore")
    restored = service.repository.get_file_versions(folder["id"], [entry["id"]])[0]
    assert restored["source_volume_uuid"] == "new-mac-volume"
    assert restored["local_state"] == "restored"
    assert service.repository.get_file_replicas(entry["id"]) == receipts
    assert service.eviction_preview(folder["id"], [entry["id"]])["eligible_count"] == 1


def test_changed_file_gets_new_version_without_old_receipts(protected):
    service, source, volumes, restic, folder, entry = protected
    (source / "one.bin").write_bytes(b"a new version")
    preview = service.eviction_preview(folder["id"], [entry["id"]])
    assert preview["eligible_count"] == 0
    assert "mtime_ns" in preview["blocked"][0]["reasons"][0]
    service.index_folder(folder["id"])
    current = next(e for e in service.repository.list_all_files(folder["id"])
                   if e["relative_path"] == "one.bin")
    assert current["id"] != entry["id"]
    assert not service.repository.get_file_replicas(current["id"])


def test_repair_refuses_symlink_to_matching_file(protected):
    service, source, volumes, restic, folder, entry = protected
    path = source / "one.bin"
    path.rename(source / "original.bin")
    path.symlink_to(source / "original.bin")
    with pytest.raises(ProtectionError, match="symbolic links"):
        service.repair_local_identity(folder["id"], file_ids=[entry["id"]], operation_id="repair")


def test_cli_repairs_legacy_identity_and_records_operation(protected, monkeypatch):
    from click.testing import CliRunner
    from coldshelf.cli import cli
    service, source, volumes, restic, folder, entry = protected
    _legacy(service, entry)
    monkeypatch.setattr("coldshelf.protection.cli._service", lambda: service)
    result = CliRunner().invoke(cli, ["protect", "repair-identity", folder["id"], "--file-id", entry["id"]])
    assert result.exit_code == 0, result.output
    operation = service.repository.list_operations(folder["id"])[0]
    assert operation["kind"] == "repair_identity"
    assert operation["status"] == "completed"
    assert operation["result"]["repaired"] == 1
    assert (source / "one.bin").read_bytes() == b"first version"


def test_eviction_staging_checks_live_device_and_restores_on_mismatch(protected, monkeypatch):
    service, source, volumes, restic, folder, entry = protected
    import coldshelf.protection.service as module
    real_stat = module._stat_fingerprint
    def changed_device(path):
        result = real_stat(path)
        if "eviction-staging" in path.parts:
            result["device"] += 1
        return result
    monkeypatch.setattr(module, "_stat_fingerprint", changed_device)
    preview = service.eviction_preview(folder["id"], [entry["id"]])
    result = service.evict(folder["id"], file_ids=[entry["id"]], dry_run=False,
                           confirmation_phrase=preview["confirmation_phrase"], operation_id="evict")
    assert result["evicted"] == 0
    assert result["failed"] == 1
    assert (source / "one.bin").read_bytes() == b"first version"
