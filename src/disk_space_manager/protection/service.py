"""Protected-folder orchestration and destructive-operation safety gates."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat as stat_module
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from disk_space_manager.executor import ActionExecutor

from .keychain import KeychainSecretStore, SecretStoreError
from .repository import ProtectionRepository, VERIFIED_REPLICA_STATES
from .restic import ResticError, ResticRunner, generation_id_from_tags
from .volumes import MacOSVolumeProvider, VolumeError, VolumeInfo


CONTROL_DIRECTORY = ".disk-space-manager"
REPOSITORY_ROOT = Path("DiskSpaceManager") / "protected"


class ProtectionError(RuntimeError):
    """Raised for a user-correctable protected-folder failure."""


ProgressCallback = Optional[Callable[[float, str], None]]


class ProtectionService:
    """Coordinate catalog state, mounted drives, Restic, and local eviction."""

    def __init__(
        self,
        repository: Optional[ProtectionRepository] = None,
        restic: Optional[ResticRunner] = None,
        secret_store: Optional[KeychainSecretStore] = None,
        volumes: Optional[MacOSVolumeProvider] = None,
    ):
        self.repository = repository or ProtectionRepository()
        self.restic = restic or ResticRunner()
        self.secret_store = secret_store or KeychainSecretStore()
        self.volumes = volumes or MacOSVolumeProvider()

    def preflight(self) -> Dict[str, Any]:
        try:
            self.volumes.ensure_supported()
            return self.restic.preflight()
        except (VolumeError, ResticError) as exc:
            raise ProtectionError(str(exc)) from exc

    def create_folder(
        self,
        name: str,
        source_path: Path,
        required_copies: int = 2,
    ) -> Dict[str, Any]:
        self.preflight()
        name = name.strip()
        if not name:
            raise ProtectionError("Protected folder name cannot be empty")
        if required_copies < 1 or required_copies > 10:
            raise ProtectionError("Required copies must be between one and ten")
        source_path = Path(source_path).expanduser().resolve(strict=True)
        if not source_path.is_dir():
            raise ProtectionError(f"Source is not a directory: {source_path}")
        try:
            source_path.relative_to(Path("/Volumes"))
        except ValueError:
            pass
        else:
            raise ProtectionError("The protected source must be on the Mac, not an external volume")
        reserved_path = source_path / CONTROL_DIRECTORY
        if reserved_path.exists() or reserved_path.is_symlink():
            raise ProtectionError(
                f"The source already contains the reserved control path {reserved_path}"
            )
        for existing in self.repository.list_folders():
            if existing["name"].casefold() == name.casefold():
                raise ProtectionError(f"A protected folder named '{name}' already exists")
            existing_path = Path(existing["source_path"]).resolve(strict=False)
            if _paths_overlap(source_path, existing_path):
                raise ProtectionError(
                    f"Protected folders cannot overlap: {existing['name']} uses {existing_path}"
                )

        folder_id = str(uuid.uuid4())
        secret = self.secret_store.generate()
        try:
            self.secret_store.set(folder_id, secret)
        except SecretStoreError as exc:
            raise ProtectionError(str(exc)) from exc
        try:
            folder = self.repository.create_folder(
                folder_id,
                name,
                source_path,
                required_copies,
            )
        except Exception:
            try:
                self.secret_store.delete(folder_id)
            except SecretStoreError:
                pass
            raise
        folder["recovery_key"] = secret
        folder["recovery_instructions"] = self.recovery_instructions(folder)
        return folder

    def update_folder(
        self,
        folder_id_or_name: str,
        *,
        name: str,
        required_copies: int,
        confirmation_phrase: Optional[str] = None,
    ) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        name = name.strip()
        if not name:
            raise ProtectionError("Protected folder name cannot be empty")
        if required_copies < 1 or required_copies > 10:
            raise ProtectionError("Required copies must be between one and ten")
        for existing in self.repository.list_folders():
            if existing["id"] != folder["id"] and existing["name"].casefold() == name.casefold():
                raise ProtectionError(f"A protected folder named '{name}' already exists")
        if required_copies < folder["required_copies"]:
            expected = f"UPDATE COPIES {folder['name']} {required_copies}"
            if confirmation_phrase != expected:
                raise ProtectionError(f"Confirmation phrase must match '{expected}'")
        return self.repository.update_folder(
            folder["id"], name=name, required_copies=required_copies
        )

    def delete_folder_catalog(
        self, folder_id_or_name: str, confirmation_phrase: str
    ) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        expected = f"DELETE PROTECTED {folder['name']}"
        if confirmation_phrase != expected:
            raise ProtectionError(f"Confirmation phrase must match '{expected}'")
        unavailable_count = self.repository.count_evicted_files(folder["id"])
        if unavailable_count:
            raise ProtectionError(
                f"Cannot delete a catalog while {unavailable_count} files are not locally present"
            )
        result = {
            "deleted": folder["id"],
            "repositories_left_intact": len(
                self.repository.list_drives(folder["id"])
            ),
        }
        self.repository.delete_folder(folder["id"])
        try:
            self.secret_store.delete(folder["id"])
        except SecretStoreError as exc:
            result["keychain_warning"] = str(exc)
        ActionExecutor().log_action(
            "DELETE_PROTECTED_CATALOG",
            Path(folder["source_path"]),
            success=True,
            details=result,
        )
        return result

    def confirm_recovery_key(self, folder_id_or_name: str, recovery_key: str) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        try:
            matches = self.secret_store.matches(folder["id"], recovery_key)
        except SecretStoreError as exc:
            raise ProtectionError(str(exc)) from exc
        if not matches:
            raise ProtectionError("Recovery key does not match the Keychain secret")
        self.repository.set_recovery_confirmed(folder["id"])
        return self.repository.get_folder(folder["id"])

    def recovery_instructions(self, folder: Dict[str, Any]) -> str:
        relative = REPOSITORY_ROOT / folder["id"] / "restic"
        return (
            f"Protected folder: {folder['name']}\n"
            f"Protected folder ID: {folder['id']}\n"
            f"Repository path on each disk: <volume>/{relative}\n"
            "Install Restic >= 0.19.1, set RESTIC_REPOSITORY to that path and "
            "RESTIC_PASSWORD to the saved recovery key, then run 'restic snapshots' "
            "or 'restic restore latest --target <destination>'."
        )

    def list_mounted_volumes(self) -> List[Dict[str, Any]]:
        try:
            volumes = self.volumes.list_volumes()
        except VolumeError as exc:
            raise ProtectionError(str(exc)) from exc
        enrolled_by_uuid = {
            drive["volume_uuid"]: drive for drive in self.repository.list_drives()
        }
        result = []
        for volume in volumes:
            item = volume.to_dict()
            drive = enrolled_by_uuid.get(volume.volume_uuid)
            item["enrolled"] = drive is not None
            item["drive_target_id"] = drive["id"] if drive else None
            result.append(item)
        return result

    def refresh_drive_status(self) -> Dict[str, VolumeInfo]:
        self.repository.set_all_drives_offline()
        try:
            mounted = {volume.volume_uuid: volume for volume in self.volumes.list_volumes()}
        except VolumeError as exc:
            raise ProtectionError(str(exc)) from exc
        for drive in self.repository.list_drives():
            volume = mounted.get(drive["volume_uuid"])
            if volume:
                self.repository.update_drive_seen(
                    drive["id"],
                    volume_name=volume.name,
                    total_bytes=volume.total_bytes,
                    free_bytes=volume.free_bytes,
                )
        return mounted

    def enroll_drive(self, folder_id_or_name: str, mount_path: Path) -> Dict[str, Any]:
        self.preflight()
        folder = self.repository.get_folder(folder_id_or_name)
        if not folder["recovery_confirmed"]:
            raise ProtectionError("Confirm the exported recovery key before enrolling a drive")
        try:
            volume = self.volumes.resolve(Path(mount_path))
        except VolumeError as exc:
            raise ProtectionError(str(exc)) from exc
        if volume.internal:
            raise ProtectionError("Protected repositories must use an external volume")
        if any(
            drive["volume_uuid"] == volume.volume_uuid
            for drive in self.repository.list_drives(folder["id"])
        ):
            raise ProtectionError(f"{volume.name} is already enrolled for this protected folder")

        relative_repo = REPOSITORY_ROOT / folder["id"] / "restic"
        repo_path = _safe_volume_path(Path(volume.mount_path), relative_repo)
        set_root = repo_path.parent
        set_root.mkdir(parents=True, exist_ok=True)
        repo_path.mkdir(parents=True, exist_ok=True)
        try:
            if any(repo_path.iterdir()):
                # A prior enrollment may have been interrupted after Restic init but
                # before the catalog row/marker was committed. The key and repository
                # identity make resuming this exact location safe.
                repository_id = self.restic.repository_id(repo_path, folder["id"])
            else:
                repository_id = self.restic.initialize_repository(repo_path, folder["id"])
        except ResticError as exc:
            raise ProtectionError(
                f"The protected repository cannot be initialized or resumed: {exc}"
            ) from exc
        try:
            drive = self.repository.add_drive(
                folder["id"],
                volume_uuid=volume.volume_uuid,
                volume_name=volume.name,
                filesystem=volume.filesystem,
                repo_relative_path=str(relative_repo),
                repository_id=repository_id,
                total_bytes=volume.total_bytes,
                free_bytes=volume.free_bytes,
            )
        except sqlite3.IntegrityError as exc:
            raise ProtectionError(
                "This volume or Restic repository identity is already enrolled"
            ) from exc
        try:
            _write_recovery_marker(set_root, folder, drive)
        except OSError as exc:
            self.repository.delete_drive(drive["id"])
            raise ProtectionError(
                "The repository was initialized, but its recovery marker could not be "
                "written. Reconnect the same disk and retry enrollment."
            ) from exc
        ActionExecutor().log_action(
            "ENROLL",
            Path(folder["source_path"]),
            repo_path,
            success=True,
        )
        drive["mount_path"] = volume.mount_path
        return drive

    def index_folder(
        self, folder_id_or_name: str, progress: ProgressCallback = None
    ) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        source = Path(folder["source_path"])
        if not source.exists() or not source.is_dir():
            raise ProtectionError(f"Protected source is unavailable: {source}")
        reserved_path = source / CONTROL_DIRECTORY
        if reserved_path.exists() or reserved_path.is_symlink():
            raise ProtectionError(
                f"The reserved control path must be reconciled or removed before indexing: {reserved_path}"
            )
        if progress:
            progress(0.05, "Indexing file metadata")
        entries = _scan_metadata(source, progress)
        manifest_digest = _manifest_digest(entries)
        generation = self.repository.save_generation(folder["id"], manifest_digest, entries)
        if progress:
            progress(1.0, f"Indexed {len(entries)} files")
        return {
            "generation": generation,
            "file_count": len(entries),
            "total_bytes": sum(entry["size"] for entry in entries),
            "manifest_digest": manifest_digest,
        }

    def backup(
        self,
        folder_id_or_name: str,
        drive_id: str,
        *,
        operation_id: str,
        progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        self.preflight()
        folder = self.repository.get_folder(folder_id_or_name)
        if not folder["recovery_confirmed"]:
            raise ProtectionError("Recovery key confirmation is required before backup")
        drive, volume, repo_path = self._mounted_drive(folder, drive_id)
        generation_id = folder.get("current_generation_id")
        if not generation_id:
            indexed = self.index_folder(folder["id"], _scaled_progress(progress, 0, 0.1))
            generation_id = indexed["generation"]["id"]
            folder = self.repository.get_folder(folder["id"])
        entries = self.repository.list_generation_entries(generation_id)
        if not entries:
            raise ProtectionError("There are no local files in the indexed generation")
        source = Path(folder["source_path"])
        self._validate_entries(source, entries)
        required_free = sum(int(entry["size"]) for entry in entries)
        if not drive["last_backup_at"] and volume.free_bytes < required_free + min(
            max(required_free // 20, 1024**3), 10 * 1024**3
        ):
            raise ProtectionError(
                f"{volume.name} does not have enough free space for the initial backup"
            )
        self._validate_repository_identity(
            drive, repo_path, folder["id"], operation_id=operation_id
        )
        if progress:
            progress(0.1, f"Backing up to {volume.name}")
        try:
            snapshot_id = self.restic.backup(
                repo_path,
                folder["id"],
                generation_id,
                source,
                operation_id=operation_id,
                progress=_scaled_progress(progress, 0.1, 0.72),
            )
            self._validate_entries(source, entries)
            snapshot_entries = self.restic.list_snapshot(
                repo_path,
                folder["id"],
                snapshot_id,
                operation_id=operation_id,
            )
            _validate_snapshot_manifest(entries, snapshot_entries)
            self.repository.mark_generation_replicated(
                generation_id,
                drive["id"],
                snapshot_id,
                "backed_up",
            )
            if progress:
                progress(0.82, "Checking repository structure")
            self.restic.check(
                repo_path,
                folder["id"],
                operation_id=operation_id,
                progress=_scaled_progress(progress, 0.82, 0.17),
            )
            self.repository.mark_generation_replicated(
                generation_id,
                drive["id"],
                snapshot_id,
                "structural_checked",
            )
        except ResticError as exc:
            if exc.exit_code not in {130, None}:
                self.repository.mark_replicas_suspect(drive["id"], str(exc))
            raise ProtectionError(str(exc)) from exc
        ActionExecutor().log_action(
            "BACKUP",
            source,
            repo_path,
            size=sum(int(entry["size"]) for entry in entries),
            success=True,
        )
        return {
            "drive_target_id": drive["id"],
            "generation_id": generation_id,
            "snapshot_id": snapshot_id,
            "file_count": len(entries),
            "verification": "structural_checked",
        }

    def verify(
        self,
        folder_id_or_name: str,
        drive_id: str,
        *,
        full: bool = False,
        file_ids: Sequence[str] = (),
        operation_id: str,
        progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        self.preflight()
        folder = self.repository.get_folder(folder_id_or_name)
        drive, volume, repo_path = self._mounted_drive(folder, drive_id)
        self._validate_repository_identity(
            drive, repo_path, folder["id"], operation_id=operation_id
        )
        try:
            if full and file_ids:
                files = self._selected_files(folder, file_ids, all_files=False)
                grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for file_info in files:
                    replica = next(
                        (
                            item
                            for item in self.repository.get_file_replicas(file_info["id"])
                            if item["drive_target_id"] == drive["id"]
                        ),
                        None,
                    )
                    if replica is None:
                        raise ProtectionError(
                            f"{file_info['relative_path']} has no replica on {volume.name}"
                        )
                    grouped[replica["snapshot_id"]].append(file_info)
                completed = 0
                total = len(files)
                self.restic.check(
                    repo_path,
                    folder["id"],
                    operation_id=operation_id,
                    progress=_scaled_progress(progress, 0, 0.2),
                )
                self.repository.mark_drive_verified(drive["id"], full=False)
                for snapshot_id, snapshot_files in grouped.items():
                    group_start = completed
                    group_size = len(snapshot_files)
                    group_progress = (
                        lambda ratio, message, start=group_start, size=group_size: progress(
                            0.2 + 0.8 * (start + ratio * size) / max(total, 1), message
                        )
                        if progress
                        else None
                    )
                    self.restic.verify_snapshot_paths(
                        repo_path,
                        folder["id"],
                        snapshot_id,
                        [item["relative_path"] for item in snapshot_files],
                        operation_id=operation_id,
                        progress=group_progress,
                    )
                    completed += len(snapshot_files)
                self.repository.mark_file_replicas_full(
                    drive["id"], [item["id"] for item in files]
                )
                verification = "targeted_full_checked"
            else:
                self.restic.check(
                    repo_path,
                    folder["id"],
                    full=full,
                    operation_id=operation_id,
                    progress=progress,
                )
                self.repository.mark_drive_verified(drive["id"], full=full)
                verification = "full_checked" if full else "structural_checked"
        except ResticError as exc:
            if exc.exit_code not in {130, None}:
                self.repository.mark_replicas_suspect(drive["id"], str(exc))
            raise ProtectionError(str(exc)) from exc
        ActionExecutor().log_action(
            "VERIFY_FULL" if full else "VERIFY_STRUCTURAL",
            repo_path,
            size=0,
            success=True,
        )
        return {
            "drive_target_id": drive["id"],
            "volume_name": volume.name,
            "verification": verification,
            "file_count": len(file_ids) if file_ids else None,
        }

    def eviction_preview(
        self,
        folder_id_or_name: str,
        file_ids: Sequence[str] = (),
        all_files: bool = False,
    ) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        files = self._selected_files(folder, file_ids, all_files)
        eligible = []
        blocked = []
        source = Path(folder["source_path"])
        for file_info in files:
            reasons = []
            if file_info["local_state"] not in {"local", "restored"}:
                reasons.append(f"local state is {file_info['local_state']}")
            replicas = self.repository.get_file_replicas(file_info["id"])
            verified = {
                replica["drive_target_id"]
                for replica in replicas
                if replica["status"] in VERIFIED_REPLICA_STATES
            }
            if len(verified) < folder["required_copies"]:
                reasons.append(
                    f"has {len(verified)}/{folder['required_copies']} verified replicas"
                )
            try:
                self._validate_entries(source, [file_info])
            except ProtectionError as exc:
                reasons.append(str(exc))
            item = {
                "id": file_info["id"],
                "relative_path": file_info["relative_path"],
                "size": file_info["size"],
                "verified_replicas": len(verified),
                "reasons": reasons,
            }
            (blocked if reasons else eligible).append(item)
        phrase = f"EVICT {folder['name']} {len(eligible)}"
        return {
            "eligible": eligible,
            "blocked": blocked,
            "eligible_count": len(eligible),
            "eligible_bytes": sum(item["size"] for item in eligible),
            "confirmation_phrase": phrase,
            "verification_warning": (
                "Eligibility uses structural repository checks. Stored data blocks may not "
                "have been read back bit-for-bit."
            ),
        }

    def evict(
        self,
        folder_id_or_name: str,
        *,
        file_ids: Sequence[str] = (),
        all_files: bool = False,
        dry_run: bool = True,
        confirmation_phrase: Optional[str] = None,
        operation_id: Optional[str] = None,
        progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        if not dry_run:
            self.preflight()
        folder = self.repository.get_folder(folder_id_or_name)
        if not folder["recovery_confirmed"]:
            raise ProtectionError("Recovery key confirmation is required before eviction")
        if not dry_run:
            try:
                self.secret_store.get(folder["id"])
            except SecretStoreError as exc:
                raise ProtectionError(str(exc)) from exc
        preview = self.eviction_preview(folder["id"], file_ids, all_files)
        if dry_run:
            preview["dry_run"] = True
            return preview
        if preview["blocked"]:
            raise ProtectionError("One or more selected files are not safe to evict")
        if not preview["eligible"]:
            raise ProtectionError("No eligible files were selected")
        if confirmation_phrase != preview["confirmation_phrase"]:
            raise ProtectionError(
                f"Confirmation phrase must match '{preview['confirmation_phrase']}'"
            )
        selected = self.repository.get_file_versions(
            folder["id"], [item["id"] for item in preview["eligible"]]
        )
        source = Path(folder["source_path"])
        operation_id = operation_id or str(uuid.uuid4())
        staging_root = source / CONTROL_DIRECTORY / "eviction-staging" / operation_id
        executor = ActionExecutor(dry_run=False)
        evicted = 0
        evicted_bytes = 0
        failures = []
        evidence = []
        for index, file_info in enumerate(selected, start=1):
            original = _safe_source_path(source, file_info["relative_path"])
            staged = _safe_source_path(staging_root, file_info["relative_path"])
            try:
                self._validate_entries(source, [file_info])
                receipts = [
                    {
                        "drive_target_id": replica["drive_target_id"],
                        "repository_id": replica["repository_id"],
                        "snapshot_id": replica["snapshot_id"],
                        "status": replica["status"],
                    }
                    for replica in self.repository.get_file_replicas(file_info["id"])
                    if replica["status"] in VERIFIED_REPLICA_STATES
                ]
                if len({receipt["repository_id"] for receipt in receipts}) < folder[
                    "required_copies"
                ]:
                    raise ProtectionError(
                        "Verified replica coverage changed after the eviction preview"
                    )
                staged.parent.mkdir(parents=True, exist_ok=True)
                self.repository.set_file_state(file_info["id"], "evicting", staged)
                os.replace(original, staged)
                staged.unlink()
                self.repository.set_file_state(file_info["id"], "evicted")
                file_evidence = {"file_id": file_info["id"], "receipts": receipts}
                executor.log_action(
                    "EVICT",
                    original,
                    size=file_info["size"],
                    success=True,
                    details=file_evidence,
                )
                evidence.append(file_evidence)
                evicted += 1
                evicted_bytes += int(file_info["size"])
            except Exception as exc:
                if staged.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, original)
                self.repository.set_file_state(
                    file_info["id"], "local" if original.exists() else "evicted"
                )
                executor.log_action(
                    "EVICT",
                    original,
                    size=file_info["size"],
                    success=False,
                    error=str(exc),
                )
                failures.append({"id": file_info["id"], "error": str(exc)})
            _remove_empty_control_tree(staged.parent, source / CONTROL_DIRECTORY)
            if progress:
                progress(index / len(selected), f"Evicted {index}/{len(selected)} files")
        _remove_empty_control_tree(staging_root, source / CONTROL_DIRECTORY)
        return {
            "dry_run": False,
            "evicted": evicted,
            "failed": len(failures),
            "total_size": evicted_bytes,
            "failures": failures,
            "evidence": evidence,
        }

    def restore(
        self,
        folder_id_or_name: str,
        *,
        file_ids: Sequence[str],
        drive_id: Optional[str] = None,
        destination: Optional[Path] = None,
        operation_id: str,
        progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        self.preflight()
        folder = self.repository.get_folder(folder_id_or_name)
        files = self._selected_files(folder, file_ids, all_files=False)
        if not files:
            raise ProtectionError("Select at least one file to restore")
        mounted = self.refresh_drive_status()
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        drive_cache: Dict[str, Tuple[Dict[str, Any], VolumeInfo, Path]] = {}
        for file_info in files:
            replicas = [
                replica
                for replica in self.repository.get_file_replicas(file_info["id"])
                if replica["status"] in VERIFIED_REPLICA_STATES
                and replica["volume_uuid"] in mounted
                and (not drive_id or replica["drive_target_id"] == drive_id)
            ]
            if not replicas:
                raise ProtectionError(
                    f"No connected verified replica can restore {file_info['relative_path']}"
                )
            replica = sorted(
                replicas,
                key=lambda item: item.get("full_verified_at") or item.get("verified_at") or "",
                reverse=True,
            )[0]
            groups[(replica["drive_target_id"], replica["snapshot_id"])].append(file_info)

        source = Path(folder["source_path"])
        destination_root = Path(destination).expanduser().resolve(strict=False) if destination else source
        try:
            destination_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProtectionError(
                f"Cannot create the restore destination {destination_root}: {exc}"
            ) from exc
        target_paths = {
            file_info["id"]: _safe_source_path(destination_root, file_info["relative_path"])
            for file_info in files
        }
        conflicts = [str(path) for path in target_paths.values() if path.exists() or path.is_symlink()]
        if conflicts:
            raise ProtectionError(
                "Restore never overwrites existing paths: " + ", ".join(conflicts[:5])
            )
        staging_control = destination_root / CONTROL_DIRECTORY
        staging_root = staging_control / "restore-staging" / operation_id
        restored = 0
        total_bytes = 0
        completed = 0
        try:
            for (selected_drive_id, snapshot_id), snapshot_files in groups.items():
                if selected_drive_id not in drive_cache:
                    selected_drive = self.repository.get_drive(selected_drive_id)
                    volume = mounted[selected_drive["volume_uuid"]]
                    repo_path = _safe_volume_path(
                        Path(volume.mount_path),
                        Path(selected_drive["repo_relative_path"]),
                    )
                    self._validate_repository_identity(
                        selected_drive,
                        repo_path,
                        folder["id"],
                        operation_id=operation_id,
                    )
                    drive_cache[selected_drive_id] = (selected_drive, volume, repo_path)
                _, _, repo_path = drive_cache[selected_drive_id]
                group_stage = staging_root / snapshot_id
                group_stage.mkdir(parents=True, exist_ok=True)
                group_start = completed
                group_size = len(snapshot_files)
                group_progress = (
                    lambda ratio, message, start=group_start, size=group_size: progress(
                        (start + ratio * size) / len(files), message
                    )
                    if progress
                    else None
                )
                try:
                    self.restic.restore(
                        repo_path,
                        folder["id"],
                        snapshot_id,
                        [item["relative_path"] for item in snapshot_files],
                        group_stage,
                        operation_id=operation_id,
                        progress=group_progress,
                    )
                except ResticError as exc:
                    raise ProtectionError(str(exc)) from exc
                for file_info in snapshot_files:
                    staged = _safe_source_path(group_stage, file_info["relative_path"])
                    try:
                        restored_fingerprint = _stat_fingerprint(staged)
                    except OSError as exc:
                        raise ProtectionError(
                            f"Restic did not restore the expected file: {file_info['relative_path']}"
                        ) from exc
                    if (
                        restored_fingerprint["size"] != int(file_info["size"])
                        or restored_fingerprint["mtime_ns"] != int(file_info["mtime_ns"])
                    ):
                        raise ProtectionError(
                            "Restored file metadata does not match the catalog version: "
                            f"{file_info['relative_path']}"
                        )
                    target = target_paths[file_info["id"]]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    placed = False
                    try:
                        if destination is None:
                            self.repository.set_file_state(
                                file_info["id"], "restoring", staged
                            )
                        _atomic_place_no_overwrite(staged, target)
                        placed = True
                        if destination is None:
                            self.repository.set_file_state(
                                file_info["id"],
                                "restored",
                                fingerprint=_stat_fingerprint(target),
                            )
                    except Exception:
                        if destination is None:
                            if placed:
                                self.repository.set_file_state(
                                    file_info["id"],
                                    "restored",
                                    fingerprint=_stat_fingerprint(target),
                                )
                            else:
                                self.repository.set_file_state(
                                    file_info["id"], "evicted"
                                )
                        raise
                    ActionExecutor().log_action(
                        "RESTORE",
                        repo_path,
                        target,
                        size=file_info["size"],
                        success=True,
                    )
                    restored += 1
                    total_bytes += int(file_info["size"])
                    completed += 1
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)
                _remove_empty_control_tree(staging_root.parent, staging_control)
        return {
            "restored": restored,
            "total_size": total_bytes,
            "destination": str(destination_root),
        }

    def folder_detail(
        self, folder_id_or_name: str, query: str = "", limit: int = 500, offset: int = 0
    ) -> Dict[str, Any]:
        folder = self.repository.get_folder(folder_id_or_name)
        try:
            mounted = self.refresh_drive_status()
        except ProtectionError:
            mounted = {}
        drives = []
        for drive in self.repository.list_drives(folder["id"]):
            item = dict(drive)
            volume = mounted.get(drive["volume_uuid"])
            item["mounted"] = volume is not None
            item["mount_path"] = volume.mount_path if volume else None
            if volume:
                item["free_bytes"] = volume.free_bytes
                item["total_bytes"] = volume.total_bytes
            drives.append(item)
        return {
            "folder": folder,
            "summary": self.repository.folder_summary(folder["id"]),
            "drives": drives,
            "files": self.repository.list_files(
                folder["id"], query=query, limit=limit, offset=offset
            ),
            "operations": self.repository.list_operations(folder["id"], limit=20),
        }

    def reconcile_interrupted_file_operations(self) -> None:
        for file_info in self.repository.list_incomplete_file_operations():
            try:
                folder = self.repository.get_folder(file_info["protected_folder_id"])
            except KeyError:
                continue
            original = _safe_source_path(
                Path(folder["source_path"]), file_info["relative_path"]
            )
            staged = Path(file_info["staged_path"]) if file_info.get("staged_path") else None
            if original.exists():
                fingerprint = _stat_fingerprint(original)
                if file_info["local_state"] == "restoring":
                    state = (
                        "restored"
                        if _fingerprint_matches(
                            fingerprint, file_info, ("size", "mtime_ns")
                        )
                        else "evicted"
                    )
                else:
                    state = (
                        "local"
                        if _fingerprint_matches(
                            fingerprint,
                            file_info,
                            ("size", "mtime_ns", "ctime_ns", "inode", "device"),
                        )
                        else "missing"
                    )
                self.repository.set_file_state(
                    file_info["id"],
                    state,
                    staged_path=staged if state in {"missing", "evicted"} and staged else None,
                    fingerprint=fingerprint if state in {"local", "restored"} else None,
                )
            elif staged and staged.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                _atomic_place_no_overwrite(staged, original)
                state = "restored" if file_info["local_state"] == "restoring" else "local"
                self.repository.set_file_state(
                    file_info["id"], state, fingerprint=_stat_fingerprint(original)
                )
            else:
                self.repository.set_file_state(file_info["id"], "evicted")
            if staged:
                control_root = next(
                    (
                        parent
                        for parent in staged.parents
                        if parent.name == CONTROL_DIRECTORY
                    ),
                    None,
                )
                if control_root:
                    _remove_empty_control_tree(staged.parent, control_root)

    def import_from_drive(
        self,
        *,
        name: str,
        source_path: Path,
        mount_path: Path,
        recovery_key: str,
        required_copies: int = 2,
        protected_folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.preflight()
        try:
            volume = self.volumes.resolve(mount_path)
        except VolumeError as exc:
            raise ProtectionError(str(exc)) from exc
        if volume.internal:
            raise ProtectionError("Recovery import requires an external volume")
        markers = list((Path(volume.mount_path) / REPOSITORY_ROOT).glob("*/repository.json"))
        if protected_folder_id:
            markers = [
                marker for marker in markers if marker.parent.name == protected_folder_id
            ]
        if not markers:
            suffix = f" for set {protected_folder_id!r}" if protected_folder_id else ""
            raise ProtectionError(
                f"No protected repository marker was found on the selected volume{suffix}"
            )
        if len(markers) != 1:
            raise ProtectionError(
                "This volume contains multiple protected repositories; provide the protected set ID"
            )
        try:
            marker = json.loads(markers[0].read_text())
            folder_id = str(marker["protected_folder_id"])
            relative_repo = Path(marker["repo_relative_path"])
            expected_repo_id = str(marker["repository_id"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProtectionError("The protected repository marker is invalid") from exc
        if markers[0].parent.name != folder_id:
            raise ProtectionError("The recovery marker is stored under the wrong protected set ID")
        expected_relative_repo = REPOSITORY_ROOT / folder_id / "restic"
        if relative_repo != expected_relative_repo:
            raise ProtectionError("The recovery marker contains an unsafe repository path")
        try:
            self.repository.get_folder(folder_id)
        except KeyError:
            pass
        else:
            raise ProtectionError("This protected folder is already present in the local catalog")

        try:
            self.secret_store.set(folder_id, recovery_key)
        except SecretStoreError as exc:
            raise ProtectionError(str(exc)) from exc
        repo_path = _safe_volume_path(Path(volume.mount_path), relative_repo)
        try:
            actual_repo_id = self.restic.repository_id(repo_path, folder_id)
            if actual_repo_id != expected_repo_id:
                raise ProtectionError("Repository identity does not match its recovery marker")
            snapshots = self.restic.snapshots(
                repo_path, folder_id, required_tags=[f"dsm-set-{folder_id}"]
            )
            if not snapshots:
                raise ProtectionError("The selected repository has no protected snapshots")
            recovered_source = Path(source_path).expanduser().resolve(strict=False)
            try:
                recovered_source.relative_to(Path("/Volumes"))
            except ValueError:
                pass
            else:
                raise ProtectionError(
                    "The recovered source path must be on the Mac, not an external volume"
                )
            reserved_path = recovered_source / CONTROL_DIRECTORY
            if reserved_path.exists() or reserved_path.is_symlink():
                raise ProtectionError(
                    f"The recovered source contains the reserved control path {reserved_path}"
                )
            recovered_name = name.strip() or "Recovered protected folder"
            for existing in self.repository.list_folders():
                if existing["name"].casefold() == recovered_name.casefold():
                    raise ProtectionError(
                        f"A protected folder named '{recovered_name}' already exists"
                    )
                if _paths_overlap(
                    recovered_source,
                    Path(existing["source_path"]).resolve(strict=False),
                ):
                    raise ProtectionError("Recovered protected folders cannot overlap")
            folder = self.repository.create_folder(
                folder_id,
                recovered_name,
                recovered_source,
                required_copies,
                recovery_confirmed=True,
            )
            drive = self.repository.add_drive(
                folder_id,
                volume_uuid=volume.volume_uuid,
                volume_name=volume.name,
                filesystem=volume.filesystem,
                repo_relative_path=str(relative_repo),
                repository_id=actual_repo_id,
                total_bytes=volume.total_bytes,
                free_bytes=volume.free_bytes,
            )
            for snapshot in snapshots:
                entries = self.restic.list_snapshot(repo_path, folder_id, snapshot["id"])
                metadata_entries = [
                    {
                        "relative_path": entry["relative_path"],
                        "file_type": "file",
                        "size": entry["size"],
                        "mtime_ns": int(
                            entry.get("mtime_ns") or _mtime_to_ns(entry.get("mtime"))
                        ),
                        "ctime_ns": 0,
                        "inode": 0,
                        "device": 0,
                    }
                    for entry in entries
                ]
                for entry in metadata_entries:
                    _safe_source_path(recovered_source, entry["relative_path"])
                generation_id = generation_id_from_tags(snapshot.get("tags") or [])
                digest = generation_id or _manifest_digest(metadata_entries)
                generation = self.repository.save_generation(folder_id, digest, metadata_entries)
                self.repository.mark_generation_replicated(
                    generation["id"], drive["id"], snapshot["id"], "backed_up"
                )
                for file_info in self.repository.list_generation_entries(generation["id"]):
                    self.repository.set_file_state(file_info["id"], "evicted")
            self.restic.check(repo_path, folder_id)
            self.repository.mark_drive_verified(drive["id"], full=False)
        except Exception:
            self.repository.delete_folder(folder_id)
            try:
                self.secret_store.delete(folder_id)
            except SecretStoreError:
                pass
            raise
        return self.folder_detail(folder["id"])

    def _mounted_drive(
        self, folder: Dict[str, Any], drive_id: str
    ) -> Tuple[Dict[str, Any], VolumeInfo, Path]:
        try:
            drive = self.repository.get_drive(drive_id)
        except KeyError as exc:
            raise ProtectionError("Unknown drive target") from exc
        if drive["protected_folder_id"] != folder["id"]:
            raise ProtectionError("Drive target does not belong to this protected folder")
        try:
            volume = self.volumes.find_by_uuid(drive["volume_uuid"])
        except VolumeError as exc:
            raise ProtectionError(str(exc)) from exc
        if not volume:
            raise ProtectionError(f"{drive['volume_name']} is not connected")
        self.repository.update_drive_seen(
            drive["id"],
            volume_name=volume.name,
            total_bytes=volume.total_bytes,
            free_bytes=volume.free_bytes,
        )
        return drive, volume, _safe_volume_path(
            Path(volume.mount_path), Path(drive["repo_relative_path"])
        )

    def _validate_repository_identity(
        self,
        drive: Dict[str, Any],
        repo_path: Path,
        folder_id: str,
        operation_id: Optional[str] = None,
    ) -> None:
        if not repo_path.exists() or not repo_path.is_dir():
            raise ProtectionError(f"Restic repository is unavailable: {repo_path}")
        try:
            repository_id = self.restic.repository_id(
                repo_path, folder_id, operation_id=operation_id
            )
        except ResticError as exc:
            raise ProtectionError(str(exc)) from exc
        if repository_id != drive["repository_id"]:
            raise ProtectionError(
                "Mounted repository identity does not match the enrolled drive"
            )

    def _validate_entries(
        self, source: Path, entries: Iterable[Dict[str, Any]]
    ) -> None:
        for entry in entries:
            path = _safe_source_path(source, entry["relative_path"])
            try:
                fingerprint = _stat_fingerprint(path)
            except OSError as exc:
                raise ProtectionError(f"Source changed or disappeared: {entry['relative_path']}") from exc
            for field in ("size", "mtime_ns", "ctime_ns", "inode", "device"):
                if int(fingerprint[field]) != int(entry[field]):
                    raise ProtectionError(
                        f"Source changed after indexing: {entry['relative_path']}"
                    )

    def _selected_files(
        self,
        folder: Dict[str, Any],
        file_ids: Sequence[str],
        all_files: bool,
    ) -> List[Dict[str, Any]]:
        if all_files:
            files = [
                item
                for item in self.repository.list_all_files(folder["id"])
                if item["local_state"] in {"local", "restored"}
            ]
        else:
            if not file_ids:
                raise ProtectionError("Select at least one file")
            files = self.repository.get_file_versions(folder["id"], list(file_ids))
            if len(files) != len(set(file_ids)):
                raise ProtectionError("One or more selected file IDs are invalid")
        return files


def _scan_metadata(source: Path, progress: ProgressCallback = None) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    pending = [source]
    directories = 0
    while pending:
        directory = pending.pop()
        directories += 1
        try:
            iterator = os.scandir(directory)
        except (OSError, PermissionError) as exc:
            raise ProtectionError(f"Cannot index {directory}: {exc}") from exc
        with iterator:
            for item in iterator:
                if item.name == CONTROL_DIRECTORY and Path(item.path).parent == source:
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        pending.append(Path(item.path))
                    elif item.is_file(follow_symlinks=False):
                        stat = item.stat(follow_symlinks=False)
                        entries.append(
                            {
                                "relative_path": str(Path(item.path).relative_to(source)),
                                "file_type": "file",
                                "size": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns,
                                "ctime_ns": stat.st_ctime_ns,
                                "inode": stat.st_ino,
                                "device": stat.st_dev,
                            }
                        )
                except (OSError, PermissionError) as exc:
                    raise ProtectionError(f"Cannot index {item.path}: {exc}") from exc
        if progress and directories % 25 == 0:
            progress(min(0.9, 0.1 + directories / (directories + len(pending) + 100)), f"Indexed {len(entries)} files")
    return sorted(entries, key=lambda entry: entry["relative_path"])


def _manifest_digest(entries: Iterable[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item["relative_path"]):
        canonical = {
            key: entry[key]
            for key in (
                "relative_path",
                "file_type",
                "size",
                "mtime_ns",
                "ctime_ns",
                "inode",
                "device",
            )
        }
        digest.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_snapshot_manifest(
    expected: Iterable[Dict[str, Any]], actual: Iterable[Dict[str, Any]]
) -> None:
    expected_map = {
        entry["relative_path"]: (int(entry["size"]), int(entry["mtime_ns"]))
        for entry in expected
    }
    actual_map = {
        entry["relative_path"]: (
            int(entry["size"]),
            int(entry.get("mtime_ns") or _mtime_to_ns(entry.get("mtime"))),
        )
        for entry in actual
    }
    if expected_map != actual_map:
        missing = sorted(set(expected_map) - set(actual_map))
        unexpected = sorted(set(actual_map) - set(expected_map))
        changed = sorted(
            path
            for path in set(expected_map) & set(actual_map)
            if expected_map[path] != actual_map[path]
        )
        details = []
        if missing:
            details.append(f"missing {missing[:3]}")
        if unexpected:
            details.append(f"unexpected {unexpected[:3]}")
        if changed:
            details.append(f"metadata changed {changed[:3]}")
        raise ProtectionError("Restic snapshot does not match the indexed generation: " + "; ".join(details))


def _stat_fingerprint(path: Path) -> Dict[str, int]:
    file_stat = path.lstat()
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise OSError(f"Not a regular file: {path}")
    return {
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "ctime_ns": file_stat.st_ctime_ns,
        "inode": file_stat.st_ino,
        "device": file_stat.st_dev,
    }


def _fingerprint_matches(
    fingerprint: Dict[str, int], file_info: Dict[str, Any], fields: Sequence[str]
) -> bool:
    return all(int(fingerprint[field]) == int(file_info[field]) for field in fields)


def _safe_source_path(root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ProtectionError("Catalog contains an unsafe absolute or empty path")
    candidate = (root / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ProtectionError(f"Catalog path escapes the protected folder: {relative_path}") from exc
    return candidate


def _safe_volume_path(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ProtectionError("Repository path must remain inside the enrolled volume")
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ProtectionError("Repository path escapes the enrolled volume") from exc
    return candidate


def _atomic_place_no_overwrite(staged: Path, target: Path) -> None:
    """Atomically move a staged file while failing if the target already exists."""
    if sys.platform == "darwin":
        import ctypes

        at_fdcwd = -2
        rename_exclusive = 0x00000004
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx = libc.renameatx_np
        renameatx.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx.restype = ctypes.c_int
        if renameatx(
            at_fdcwd,
            os.fsencode(staged),
            at_fdcwd,
            os.fsencode(target),
            rename_exclusive,
        ) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), str(target))
        return

    # Protection operations are macOS-only, but this safe fallback keeps unit
    # tests and future Unix support race-free on filesystems with hard links.
    os.link(staged, target, follow_symlinks=False)
    staged.unlink()


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _scaled_progress(
    callback: ProgressCallback, start: float, span: float
) -> ProgressCallback:
    if not callback:
        return None
    return lambda ratio, message: callback(start + max(0.0, min(ratio, 1.0)) * span, message)


def _write_recovery_marker(
    set_root: Path, folder: Dict[str, Any], drive: Dict[str, Any]
) -> None:
    marker = {
        "schema_version": 1,
        "protected_folder_id": folder["id"],
        "repo_relative_path": drive["repo_relative_path"],
        "repository_id": drive["repository_id"],
    }
    _atomic_write_text(
        set_root / "repository.json",
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write_text(
        set_root / "RECOVERY.txt",
        "Disk Space Manager protected repository\n\n"
        f"Protected folder ID: {folder['id']}\n"
        f"Restic repository: {drive['repo_relative_path']}\n"
        "The recovery key is intentionally not stored on this disk. Retrieve it from "
        "your password manager, install Restic >= 0.19.1, and follow the recovery "
        "instructions saved with that key.\n"
    )


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The target may have disconnected; the enrollment rollback reports
            # the primary marker-write error and a later retry reuses the repo.
            pass


def _remove_empty_control_tree(path: Path, stop: Path) -> None:
    current = path
    while current != stop.parent:
        try:
            current.rmdir()
        except OSError:
            break
        if current == stop:
            break
        current = current.parent


def _mtime_to_ns(value: Any) -> int:
    if not value:
        return 0
    try:
        import re
        from datetime import datetime

        match = re.fullmatch(
            r"(?P<head>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
            r"(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:\d{2})?",
            str(value),
        )
        if not match:
            return 0
        zone = match.group("zone") or ""
        if zone == "Z":
            zone = "+00:00"
        base = datetime.fromisoformat(match.group("head") + zone)
        fraction = (match.group("fraction") or "").ljust(9, "0")[:9]
        return int(base.timestamp()) * 1_000_000_000 + int(fraction or 0)
    except (ValueError, TypeError):
        return 0
