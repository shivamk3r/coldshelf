"""Deterministic Restic, Keychain, and mounted-volume fakes for protection tests."""

import os
import uuid
from pathlib import Path

from disk_space_manager.protection.policy import is_ignored_backup_path
from disk_space_manager.protection.restic import ResticError
from disk_space_manager.protection.volumes import VolumeInfo


class MemorySecretStore:
    def __init__(self):
        self.secrets = {}

    def generate(self):
        return "test-recovery-key"

    def set(self, folder_id, secret):
        self.secrets[folder_id] = secret

    def get(self, folder_id):
        if folder_id not in self.secrets:
            raise RuntimeError("missing secret")
        return self.secrets[folder_id]

    def delete(self, folder_id):
        self.secrets.pop(folder_id, None)

    def matches(self, folder_id, candidate):
        return self.get(folder_id) == candidate


class FakeVolumes:
    def __init__(self, volumes):
        self.volumes = {volume.volume_uuid: volume for volume in volumes}
        self.mounted = set(self.volumes)

    def ensure_supported(self):
        return None

    def list_volumes(self):
        return [
            volume for volume_id, volume in self.volumes.items() if volume_id in self.mounted
        ]

    def resolve(self, mount_path):
        resolved = Path(mount_path).resolve()
        for volume in self.list_volumes():
            if Path(volume.mount_path).resolve() == resolved:
                return volume
        raise RuntimeError(f"not mounted: {mount_path}")

    def find_by_uuid(self, volume_uuid):
        if volume_uuid not in self.mounted:
            return None
        return self.volumes.get(volume_uuid)

    def mount_only(self, *volume_uuids):
        self.mounted = set(volume_uuids)


def fake_volume(path: Path, volume_uuid: str, name: str) -> VolumeInfo:
    path.mkdir(parents=True, exist_ok=True)
    return VolumeInfo(
        volume_uuid=volume_uuid,
        name=name,
        mount_path=str(path),
        filesystem="ExFAT",
        device_identifier=f"disk-{volume_uuid}",
        total_bytes=2 * 1024**4,
        free_bytes=1900 * 1024**3,
        writable=True,
        internal=False,
        removable=True,
    )


class FakeRestic:
    def __init__(self):
        self.repository_ids = {}
        self.snapshot_entries = {}
        self.snapshot_contents = {}
        self.snapshot_mtimes = {}
        self.snapshot_tags = {}
        self.snapshot_repositories = {}
        self.check_calls = []
        self.cancelled = []

    def preflight(self):
        return {"version": "0.19.1"}

    def initialize_repository(self, repository, folder_id, operation_id=None):
        repository = Path(repository)
        repository.mkdir(parents=True, exist_ok=True)
        repository_id = f"repo-{uuid.uuid4().hex}"
        (repository / "config").write_text(repository_id)
        self.repository_ids[str(repository)] = repository_id
        return repository_id

    def repository_id(self, repository, folder_id, operation_id=None):
        try:
            return self.repository_ids[str(Path(repository))]
        except KeyError as exc:
            raise ResticError("repository unavailable", 1) from exc

    def backup(
        self,
        repository,
        folder_id,
        generation_id,
        source_path,
        *,
        operation_id,
        progress=None,
    ):
        snapshot_id = f"snap-{uuid.uuid4().hex}"
        source_path = Path(source_path)
        entries = []
        contents = {}
        mtimes = {}
        for path in sorted(source_path.rglob("*")):
            if (
                not path.is_file()
                or ".disk-space-manager" in path.parts
                or is_ignored_backup_path(path.name)
            ):
                continue
            relative = str(path.relative_to(source_path))
            file_stat = path.stat()
            entries.append(
                {
                    "relative_path": relative,
                    "size": file_stat.st_size,
                    "mtime": None,
                    "mtime_ns": file_stat.st_mtime_ns,
                    "snapshot_id": snapshot_id,
                }
            )
            contents[relative] = path.read_bytes()
            mtimes[relative] = file_stat.st_mtime_ns
        self.snapshot_entries[snapshot_id] = entries
        self.snapshot_contents[snapshot_id] = contents
        self.snapshot_mtimes[snapshot_id] = mtimes
        self.snapshot_tags[snapshot_id] = [
            f"dsm-set-{folder_id}",
            f"dsm-generation-{generation_id}",
            "dsm-protected",
        ]
        self.snapshot_repositories[snapshot_id] = str(Path(repository))
        if progress:
            progress(1.0, "fake backup complete")
        return snapshot_id

    def list_snapshot(self, repository, folder_id, snapshot_id, operation_id=None):
        return [
            entry
            for entry in self.snapshot_entries[snapshot_id]
            if not is_ignored_backup_path(entry["relative_path"])
        ]

    def snapshots(self, repository, folder_id, required_tags=None, operation_id=None):
        required = set(required_tags or [])
        return [
            {"id": snapshot_id, "time": snapshot_id, "tags": tags}
            for snapshot_id, tags in self.snapshot_tags.items()
            if required.issubset(tags)
            and self.snapshot_repositories[snapshot_id] == str(Path(repository))
        ]

    def check(
        self,
        repository,
        folder_id,
        *,
        full=False,
        operation_id=None,
        progress=None,
    ):
        self.repository_id(repository, folder_id)
        self.check_calls.append((str(repository), full))
        if progress:
            progress(1.0, "fake check complete")

    def verify_snapshot_paths(
        self,
        repository,
        folder_id,
        snapshot_id,
        relative_paths,
        *,
        operation_id,
        progress=None,
    ):
        for index, path in enumerate(relative_paths, start=1):
            if path not in self.snapshot_contents[snapshot_id]:
                raise ResticError("missing data", 1)
            if progress:
                progress(index / len(relative_paths), "fake full check")

    def restore(
        self,
        repository,
        folder_id,
        snapshot_id,
        relative_paths,
        target,
        *,
        operation_id,
        progress=None,
    ):
        self.repository_id(repository, folder_id)
        target = Path(target)
        for index, relative in enumerate(relative_paths, start=1):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.snapshot_contents[snapshot_id][relative])
            mtime_ns = self.snapshot_mtimes[snapshot_id][relative]
            os.utime(destination, ns=(mtime_ns, mtime_ns))
            if progress:
                progress(index / len(relative_paths), "fake restore")

    def cancel(self, operation_id):
        self.cancelled.append(operation_id)
        return True
