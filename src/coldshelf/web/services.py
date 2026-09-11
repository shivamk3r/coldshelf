"""Web-facing services that reuse the core coldshelf modules."""

import hashlib
import os
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from coldshelf.analyzer import FileAnalyzer
from coldshelf.archive_targets import (
    ArchiveTarget,
    ArchiveTargetError,
    resolve_archive_target,
)
from coldshelf.duplicates import DuplicateDetector, empty_duplicate_report
from coldshelf.drive_detector import detect_external_drives
from coldshelf.executor import ActionExecutor
from coldshelf.scanner import DiskScanner, ScanProgress
from coldshelf.protection.repository import ProtectionRepository
from coldshelf.utils import format_size

from .repository import WebRepository, confirmation_phrase
from .schemas import ActionRequest, JobCreateRequest


class ServiceError(RuntimeError):
    """Raised for user-correctable service errors."""


def browse_directories(path: Optional[str], show_hidden: bool = False) -> Dict[str, Any]:
    """List directories for the authenticated server-side folder picker."""
    requested = Path(path).expanduser() if path else Path.home()
    try:
        current = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ServiceError(f"Folder does not exist or cannot be opened: {requested}") from exc
    if not current.is_dir():
        raise ServiceError(f"Path is not a folder: {current}")
    if not os.access(current, os.R_OK | os.X_OK):
        raise ServiceError(f"Folder is not readable: {current}")

    directories: List[Dict[str, Any]] = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    readable = os.access(entry.path, os.R_OK | os.X_OK)
                    directories.append(
                        {
                            "name": entry.name,
                            "path": str(Path(entry.path)),
                            "kind": "folder",
                            "readable": readable,
                            "symlink": entry.is_symlink(),
                        }
                    )
                except OSError:
                    continue
    except (OSError, PermissionError) as exc:
        raise ServiceError(f"Folder cannot be listed: {current}") from exc
    directories.sort(key=lambda item: (not item["readable"], item["name"].casefold()))

    parent = current.parent if current.parent != current else None
    return {
        "current_path": str(current),
        "parent_path": str(parent) if parent else None,
        "roots": filesystem_roots(),
        "directories": directories,
    }


def filesystem_roots() -> List[Dict[str, Any]]:
    """Return shared Home, Computer, and detected-drive browse shortcuts."""
    home = Path.home().resolve()
    roots: List[Dict[str, Any]] = [
        {
            "name": "Home",
            "path": str(home),
            "kind": "home",
            "readable": os.access(home, os.R_OK | os.X_OK),
            "symlink": False,
        },
        {
            "name": "Computer",
            "path": str(Path("/")),
            "kind": "root",
            "readable": os.access("/", os.R_OK | os.X_OK),
            "symlink": False,
        },
    ]
    for drive in _detected_archive_drives():
        roots.append(
            {
                "name": str(drive.get("name") or Path(str(drive["path"])).name),
                "path": str(drive["path"]),
                "kind": "drive",
                "readable": os.access(str(drive["path"]), os.R_OK | os.X_OK),
                "symlink": False,
            }
        )
    return list({item["path"]: item for item in roots}.values())


def available_archive_targets() -> Dict[str, Any]:
    """Return safe display metadata for writable external archive targets."""
    drives = [
        {
            "name": str(item.get("name") or Path(str(item["path"])).name),
            "path": str(item["path"]),
            "available_space": int(item.get("available_space") or 0),
            "device": str(item.get("device") or ""),
            "filesystem": str(item.get("fs_type") or item.get("filesystem") or ""),
        }
        for item in _detected_archive_drives()
    ]
    return {
        "drives": drives,
        "recommended_path": drives[0]["path"] if drives else None,
    }


def primary_storage_summary() -> Dict[str, int]:
    """Return capacity details for the filesystem containing the user's home."""
    try:
        usage = shutil.disk_usage(Path.home())
    except OSError as exc:
        raise ServiceError("Storage information is temporarily unavailable.") from exc
    return {
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "available_bytes": int(usage.free),
    }


def _detected_archive_drives() -> List[Dict[str, Any]]:
    try:
        return list(detect_external_drives())
    except Exception:
        # Drive discovery is advisory in the dashboard. A platform-tool or
        # permission failure must not prevent users from choosing a local path.
        return []


class ReportService:
    """Build and persist report jobs for the web API."""

    def __init__(self, repository: WebRepository):
        self.repository = repository

    def generate_report(self, job_id: str, request: JobCreateRequest) -> None:
        """Run a complete report and persist summaries plus candidates."""
        self.repository.mark_job_running(job_id)
        scan_path = _resolve_scan_path(request.path)
        analyzer = FileAnalyzer(age_threshold=timedelta(days=request.age_months * 30))

        def scan_progress(progress: ScanProgress) -> None:
            if progress.is_finished:
                percent = 55.0
            elif progress.directories_discovered:
                ratio = progress.directories_completed / progress.directories_discovered
                percent = min(50.0, max(1.0, ratio * 50.0))
            else:
                percent = 1.0
            self.repository.update_job_progress(
                job_id,
                "scan",
                percent,
                f"Scanned {progress.files_scanned} files",
            )

        scanner = DiskScanner(
            scan_path,
            detailed_progress_callback=scan_progress,
        )
        scan_results = scanner.scan()

        self.repository.update_job_progress(
            job_id,
            "analyze",
            62.0,
            "Analyzing cache candidates",
        )
        cache_files = analyzer.find_cache_files(scan_results["files"])

        self.repository.update_job_progress(
            job_id,
            "analyze",
            70.0,
            "Analyzing old files",
        )
        old_source_files = [
            file_info
            for file_info in scan_results["files"]
            if not os.path.islink(file_info["path"])
        ]
        old_files = analyzer.find_old_files(old_source_files)

        duplicate_report = empty_duplicate_report()
        if request.include_duplicates:
            self.repository.update_job_progress(
                job_id,
                "duplicates",
                78.0,
                "Finding duplicate files",
            )
            duplicate_report = DuplicateDetector().build_report(
                scan_results["files"],
                include_near_duplicates=request.include_near_duplicates,
            )

        exact = duplicate_report.get("exact", {})
        savings = analyzer.calculate_potential_savings(
            cache_files,
            old_files,
            exact_duplicate_reclaimable_size=int(exact.get("reclaimable_size") or 0),
            exact_duplicate_file_count=int(exact.get("duplicate_file_count") or 0),
        )
        summary = _format_usage_summary(
            analyzer.analyze_disk_usage(scan_results["files"], scan_results["directories"])
        )
        report = {
            "job_id": job_id,
            "path": str(scan_path),
            "age_months": request.age_months,
            "scan": {
                "total_scanned": scan_results["total_scanned"],
                "error_count": len(scan_results["errors"]),
                "errors": scan_results["errors"][:100],
            },
            "summary": summary,
            "largest_files": [_format_file(file_info) for file_info in scanner.get_largest_files(20)],
            "largest_directories": [
                {
                    "path": path,
                    "size": size,
                    "size_formatted": format_size(size),
                }
                for path, size in scanner.get_largest_directories(20)
            ],
            "savings": savings,
            "duplicates": _format_duplicate_report(duplicate_report),
            "candidate_counts": {
                "cache": len(cache_files),
                "old": len(old_files),
            },
        }

        candidates = list(_build_candidates(job_id, "cache", cache_files))
        candidates.extend(_build_candidates(job_id, "old", old_files))

        self.repository.update_job_progress(job_id, "persist", 92.0, "Saving report")
        self.repository.save_report(job_id, report, candidates)
        self.repository.mark_job_completed(job_id)


class ActionService:
    """Execute web actions against persisted report candidates."""

    def __init__(
        self,
        repository: WebRepository,
        protection_repository: Optional[ProtectionRepository] = None,
    ):
        self.repository = repository
        self.protection_repository = protection_repository or ProtectionRepository()

    def clean(self, job_id: str, request: ActionRequest) -> Dict[str, Any]:
        self._validate_action_request(job_id, "clean", request)
        candidates = self._load_candidates(job_id, "cache", request.candidate_ids)
        selected_files = [_candidate_to_file(candidate) for candidate in candidates]
        files, skipped_count = self._exclude_protected(selected_files)
        if not files:
            raise ServiceError(
                "Selected files are registered as protected; use protected-folder eviction"
            )
        executor = ActionExecutor(dry_run=request.dry_run)
        result = executor.delete_files(files, confirm=False)
        return self.repository.save_action(
            action_id=str(uuid.uuid4()),
            job_id=job_id,
            action_type="clean",
            dry_run=request.dry_run,
            target_path=None,
            selected_count=len(selected_files),
            skipped_count=skipped_count,
            result=result,
        )

    def archive(self, job_id: str, request: ActionRequest) -> Dict[str, Any]:
        self._validate_action_request(job_id, "archive", request)
        candidates = self._load_candidates(job_id, "old", request.candidate_ids)
        try:
            target = _resolve_archive_target_for_action(request)
        except ArchiveTargetError as exc:
            raise ServiceError(str(exc)) from exc

        selected_files = [_candidate_to_file(candidate) for candidate in candidates]
        unprotected_files, protected_skipped = self._exclude_protected(selected_files)
        files = [
            file_info
            for file_info in unprotected_files
            if not _is_under_path(Path(file_info["path"]), target.root)
            and not os.path.islink(file_info["path"])
        ]
        skipped_count = protected_skipped + len(unprotected_files) - len(files)
        if not files:
            raise ServiceError("No selected archive candidates are safe to archive")

        job = self.repository.get_job(job_id)
        executor = ActionExecutor(dry_run=request.dry_run)
        result = executor.archive_files(
            files,
            target.archive_base,
            Path(job["path"]),
            confirm=False,
        )
        return self.repository.save_action(
            action_id=str(uuid.uuid4()),
            job_id=job_id,
            action_type="archive",
            dry_run=request.dry_run,
            target_path=str(target.root),
            selected_count=len(selected_files),
            skipped_count=skipped_count,
            result=result,
        )

    def _validate_action_request(
        self,
        job_id: str,
        action_type: str,
        request: ActionRequest,
    ) -> None:
        if not request.candidate_ids:
            raise ServiceError("Select at least one candidate")
        job = self.repository.get_job(job_id)
        if job["status"] != "completed":
            raise ServiceError("Actions require a completed report job")
        if not request.dry_run:
            expected = confirmation_phrase(action_type, job_id)
            if request.confirmation_phrase != expected:
                raise ServiceError(f"Confirmation phrase must match '{expected}'")

    def _load_candidates(
        self,
        job_id: str,
        kind: str,
        candidate_ids: List[str],
    ) -> List[Dict[str, Any]]:
        candidates = self.repository.list_candidates_by_ids(job_id, candidate_ids, kind)
        if len(candidates) != len(set(candidate_ids)):
            raise ServiceError("One or more selected candidates are invalid")
        return candidates

    def _exclude_protected(
        self, files: List[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], int]:
        safe = [
            file_info
            for file_info in files
            if not self.protection_repository.is_protected_path(Path(file_info["path"]))
        ]
        return safe, len(files) - len(safe)


def _resolve_scan_path(path: Optional[str]) -> Path:
    scan_path = Path(path).expanduser() if path else Path.home()
    if not scan_path.exists() or not scan_path.is_dir():
        raise ServiceError(f"Scan path does not exist or is not a directory: {scan_path}")
    return scan_path


def _resolve_archive_target_for_action(request: ActionRequest) -> ArchiveTarget:
    target_path = Path(request.target_path).expanduser() if request.target_path else None
    external_path = Path(request.external_path).expanduser() if request.external_path else None
    if request.dry_run and target_path:
        return ArchiveTarget(
            root=target_path,
            archive_base=target_path / "archived_files",
            label="local folder",
            source="target_path",
        )
    return resolve_archive_target(target_path=target_path, external_path=external_path)


def _build_candidates(
    job_id: str,
    kind: str,
    files: Iterable[Dict[str, Any]],
) -> Iterable[Dict[str, Any]]:
    for file_info in files:
        path = str(file_info["path"])
        yield {
            "id": _candidate_id(job_id, kind, path),
            "kind": kind,
            "path": path,
            "size": int(file_info.get("size") or 0),
            "metadata": _candidate_metadata(file_info),
        }


def _candidate_id(job_id: str, kind: str, path: str) -> str:
    digest = hashlib.sha256(f"{job_id}:{kind}:{path}".encode("utf-8")).hexdigest()
    return digest[:24]


def _candidate_metadata(file_info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in file_info.items()
        if key not in {"path", "size"}
    }


def _candidate_to_file(candidate: Dict[str, Any]) -> Dict[str, Any]:
    file_info = {
        "path": candidate["path"],
        "size": int(candidate["size"]),
    }
    file_info.update(candidate["metadata"])
    return file_info


def _format_usage_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **summary,
        "top_extensions": [
            {
                "extension": extension,
                "count": values["count"],
                "size": values["size"],
                "size_formatted": format_size(values["size"]),
            }
            for extension, values in summary["top_extensions"]
        ],
        "average_file_size_formatted": format_size(int(summary["average_file_size"])),
    }


def _format_file(file_info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": file_info["path"],
        "size": int(file_info.get("size") or 0),
        "size_formatted": format_size(int(file_info.get("size") or 0)),
        "atime": file_info.get("atime"),
        "mtime": file_info.get("mtime"),
        "ctime": file_info.get("ctime"),
    }


def _format_duplicate_report(report: Dict[str, Any]) -> Dict[str, Any]:
    formatted = {"exact": dict(report.get("exact", {})), "near": dict(report.get("near", {}))}
    for section in ("exact", "near"):
        groups = formatted[section].get("groups", [])
        formatted[section]["groups"] = [_format_duplicate_group(group) for group in groups]
    return formatted


def _format_duplicate_group(group: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(group)
    result["files"] = [_format_file(file_info) for file_info in group.get("files", [])]
    for key in ("size", "total_size", "reclaimable_size", "reviewable_size"):
        if key in result:
            result[f"{key}_formatted"] = format_size(int(result[key]))
    return result


def _is_under_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False
