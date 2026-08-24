"""Cancellable in-memory jobs for one-level filesystem browsing."""

from __future__ import annotations

import copy
import platform
import uuid
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Dict, List, Optional

from disk_space_manager.directory_sizer import (
    DirectorySizer,
    DirectorySizingCancelled,
    PreparedDirectory,
)

from .io_queue import HeavyIOQueue


MAX_BROWSE_JOBS = 32
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class BrowseJobNotFound(KeyError):
    """Raised when an ephemeral browse job is no longer available."""


class BrowseResultNotReady(RuntimeError):
    """Raised when a browse result is requested before completion."""


class BrowseJobManager:
    """Prepare listings synchronously and size them on the shared I/O queue."""

    def __init__(
        self,
        io_queue: HeavyIOQueue,
        roots_provider: Callable[[], List[Dict[str, Any]]],
        *,
        max_jobs: int = MAX_BROWSE_JOBS,
        sizer: Optional[DirectorySizer] = None,
        default_path: Optional[Path] = None,
        deletion_annotator: Optional[
            Callable[
                [List[Dict[str, Any]], Path, List[Dict[str, Any]]],
                List[Dict[str, Any]],
            ]
        ] = None,
    ) -> None:
        self.io_queue = io_queue
        self.roots_provider = roots_provider
        self.max_jobs = max(1, int(max_jobs))
        self.sizer = sizer or DirectorySizer()
        self.default_path = Path(default_path) if default_path is not None else None
        self.deletion_annotator = deletion_annotator
        self._jobs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._prepared: Dict[str, PreparedDirectory] = {}
        self._results: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, Event] = {}
        self._futures: Dict[str, Future] = {}
        self._lock = RLock()

    def submit(self, path: Optional[str]) -> Dict[str, Any]:
        requested_path = (
            str(self.default_path)
            if path is None and self.default_path is not None
            else path
        )
        prepared = self.sizer.prepare(requested_path)
        roots = self.roots_provider()
        scan_id = str(uuid.uuid4())
        created_at = _utc_now()
        job = {
            "id": scan_id,
            "status": "queued",
            "path": str(prepared.root),
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "progress_percent": 0.0,
            "progress_message": "Queued for sizing",
            "files_scanned": 0,
            "directories_scanned": 0,
            "completed_entries": 0,
            "total_entries": sum(
                1
                for entry in prepared.entries
                if entry["kind"] == "directory" and entry["size_state"] == "pending"
            ),
            "cancel_requested": False,
        }
        listing = prepared.initial_listing()
        listing["roots"] = copy.deepcopy(roots)
        listing_entries = [
            _annotate_protection_eligibility(entry, roots)
            for entry in listing["entries"]
        ]
        listing["entries"] = self._annotate_deletion(
            listing_entries, prepared.root, roots
        )
        cancel_event = Event()
        with self._lock:
            self._jobs[scan_id] = job
            self._prepared[scan_id] = prepared
            self._events[scan_id] = cancel_event
            self._trim_locked()
        future = self.io_queue.submit(self._run, scan_id, roots)
        with self._lock:
            self._futures[scan_id] = future
        return {"scan": self.get_job(scan_id), "listing": copy.deepcopy(listing)}

    def get_job(self, scan_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(scan_id)
            if job is None:
                raise BrowseJobNotFound(scan_id)
            self._jobs.move_to_end(scan_id)
            return copy.deepcopy(job)

    def get_result(self, scan_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(scan_id)
            if job is None:
                raise BrowseJobNotFound(scan_id)
            if job["status"] != "completed":
                raise BrowseResultNotReady(
                    f"Browse scan is {job['status']}; a result is available after completion"
                )
            self._jobs.move_to_end(scan_id)
            return copy.deepcopy(self._results[scan_id])

    def cancel(self, scan_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(scan_id)
            if job is None:
                raise BrowseJobNotFound(scan_id)
            if job["status"] in TERMINAL_STATUSES:
                return copy.deepcopy(job)
            job["cancel_requested"] = True
            job["progress_message"] = "Cancellation requested"
            event = self._events[scan_id]
            event.set()
            future = self._futures.get(scan_id)
            if future and future.cancel():
                self._mark_cancelled_locked(scan_id, "Cancelled before sizing started")
                self._prepared.pop(scan_id, None)
            return copy.deepcopy(job)

    def _run(self, scan_id: str, roots: List[Dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs.get(scan_id)
            if job is None:
                return
            if job["cancel_requested"]:
                self._mark_cancelled_locked(scan_id, "Cancelled before sizing started")
                self._prepared.pop(scan_id, None)
                return
            job["status"] = "running"
            job["started_at"] = _utc_now()
            job["progress_percent"] = 2.0
            job["progress_message"] = "Sizing folder contents"
            prepared = self._prepared[scan_id]
            event = self._events[scan_id]

        def progress(files: int, directories: int, completed: int, total: int) -> None:
            with self._lock:
                current = self._jobs.get(scan_id)
                if current is None or current["status"] != "running":
                    return
                current["files_scanned"] = files
                current["directories_scanned"] = directories
                current["completed_entries"] = completed
                current["total_entries"] = total
                ratio = completed / total if total else 1.0
                current["progress_percent"] = min(95.0, 5.0 + ratio * 90.0)
                current["progress_message"] = (
                    f"Scanned {files:,} files in {directories:,} folders"
                )

        try:
            result = self.sizer.scan(prepared, event, progress)
            result["roots"] = copy.deepcopy(roots)
            result_entries = [
                _annotate_protection_eligibility(entry, roots)
                for entry in result["entries"]
            ]
            result["entries"] = self._annotate_deletion(
                result_entries, prepared.root, roots
            )
            with self._lock:
                current = self._jobs.get(scan_id)
                if current is None:
                    return
                if current["cancel_requested"]:
                    self._mark_cancelled_locked(scan_id, "Browse scan cancelled")
                    return
                self._results[scan_id] = result
                current["status"] = "completed"
                current["finished_at"] = _utc_now()
                current["progress_percent"] = 100.0
                current["progress_message"] = "Sizing complete"
        except DirectorySizingCancelled:
            with self._lock:
                if scan_id in self._jobs:
                    self._mark_cancelled_locked(scan_id, "Browse scan cancelled")
        except Exception as exc:
            with self._lock:
                current = self._jobs.get(scan_id)
                if current is None:
                    return
                current["status"] = "failed"
                current["finished_at"] = _utc_now()
                current["error"] = str(exc)
                current["progress_message"] = str(exc)
        finally:
            with self._lock:
                self._prepared.pop(scan_id, None)
                self._trim_locked()

    def _annotate_deletion(
        self,
        entries: List[Dict[str, Any]],
        current_path: Path,
        roots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if self.deletion_annotator is not None:
            return self.deletion_annotator(entries, current_path, roots)
        annotated = []
        for entry in entries:
            item = copy.deepcopy(entry)
            item["deletion_eligible"] = False
            item["deletion_blocker"] = "Browse deletion is unavailable"
            annotated.append(item)
        return annotated

    def _mark_cancelled_locked(self, scan_id: str, message: str) -> None:
        job = self._jobs[scan_id]
        job["status"] = "cancelled"
        job["finished_at"] = _utc_now()
        job["progress_message"] = message

    def _trim_locked(self) -> None:
        while len(self._jobs) > self.max_jobs:
            removable = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job["status"] in TERMINAL_STATUSES
                ),
                None,
            )
            if removable is None:
                removable = next(iter(self._jobs))
                event = self._events.get(removable)
                if event is not None:
                    event.set()
                future = self._futures.get(removable)
                if future is not None:
                    future.cancel()
            self._jobs.pop(removable, None)
            self._prepared.pop(removable, None)
            self._results.pop(removable, None)
            self._events.pop(removable, None)
            self._futures.pop(removable, None)


def _annotate_protection_eligibility(
    entry: Dict[str, Any], roots: List[Dict[str, Any]]
) -> Dict[str, Any]:
    item = copy.deepcopy(entry)
    blocker: Optional[str] = None
    if item["kind"] != "directory":
        blocker = "Only folders can be protected"
    elif item["symlink"]:
        blocker = "Folder shortcuts are not followed or protectable"
    elif item["mount_boundary"]:
        blocker = "Mounted filesystems must be browsed from their own drive shortcut"
    elif not item["readable"]:
        blocker = "The folder is not readable"
    elif not item["writable"]:
        blocker = "Offloading requires a writable local folder"
    elif platform.system() != "Darwin":
        blocker = "Protected folders currently require macOS"
    elif _is_external_path(Path(item["path"]), roots):
        blocker = "Protected sources must be on the Mac, not an external drive"
    item["protection_eligible"] = blocker is None
    item["protection_blocker"] = blocker
    return item


def _is_external_path(path: Path, roots: List[Dict[str, Any]]) -> bool:
    external_roots = [
        Path(str(root["path"])) for root in roots if root.get("kind") == "drive"
    ]
    if platform.system() == "Darwin":
        external_roots.append(Path("/Volumes"))
    return any(_is_under(path, root) for root in external_roots)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
