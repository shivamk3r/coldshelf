"""Metadata-only sizing for one-level filesystem browsing."""

from __future__ import annotations

import copy
import os
import stat as stat_module
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional


ALLOCATION_BLOCK_BYTES = 512
ERROR_DISPLAY_LIMIT = 100
PROGRESS_FILE_BATCH_SIZE = 500
PROGRESS_DIRECTORY_BATCH_SIZE = 100
PROGRESS_WAIT_SECONDS = 0.1


class DirectorySizingError(RuntimeError):
    """Raised when the selected browse path cannot be sized."""


class DirectorySizingCancelled(RuntimeError):
    """Raised when a cooperative browse scan is cancelled."""


ProgressCallback = Callable[[int, int, int, int], None]


@dataclass
class PreparedDirectory:
    """Validated one-level listing captured before background sizing starts."""

    root: Path
    root_device: int
    parent_path: Optional[str]
    entries: List[Dict[str, Any]]
    error_count: int
    errors: List[str]

    def initial_listing(self) -> Dict[str, Any]:
        return {
            "current_path": str(self.root),
            "parent_path": self.parent_path,
            "entries": copy.deepcopy(self.entries),
        }


class DirectorySizer:
    """Size immediate entries without retaining descendant file metadata."""

    def __init__(self, error_limit: int = ERROR_DISPLAY_LIMIT):
        self.error_limit = max(1, int(error_limit))

    def prepare(self, path: Optional[str]) -> PreparedDirectory:
        requested = Path(path).expanduser() if path else Path.home()
        try:
            root = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DirectorySizingError(
                f"Folder does not exist or cannot be opened: {requested}"
            ) from exc
        if not root.is_dir():
            raise DirectorySizingError(f"Path is not a folder: {root}")
        if not os.access(root, os.R_OK | os.X_OK):
            raise DirectorySizingError(f"Folder is not readable: {root}")

        try:
            root_stat = root.stat()
        except OSError as exc:
            raise DirectorySizingError(f"Folder cannot be inspected: {root}") from exc

        entries: List[Dict[str, Any]] = []
        errors: List[str] = []
        error_count = 0
        try:
            with os.scandir(root) as children:
                for child in children:
                    try:
                        entry = _initial_entry(child, int(root_stat.st_dev))
                    except OSError as exc:
                        entries.append(_unavailable_initial_entry(child))
                        if len(errors) < self.error_limit:
                            errors.append(f"Cannot inspect {child.path}: {exc}")
                        continue
                    entries.append(entry)
        except (OSError, PermissionError) as exc:
            raise DirectorySizingError(f"Folder cannot be listed: {root}") from exc

        entries.sort(key=lambda item: item["name"].casefold())
        parent = root.parent if root.parent != root else None
        return PreparedDirectory(
            root=root,
            root_device=int(root_stat.st_dev),
            parent_path=str(parent) if parent else None,
            entries=entries,
            error_count=error_count,
            errors=errors,
        )

    def scan(
        self,
        prepared: PreparedDirectory,
        cancel_event: Optional[Event] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        cancel_event = cancel_event or Event()
        _raise_if_cancelled(cancel_event)
        entries = copy.deepcopy(prepared.entries)
        errors = list(prepared.errors[: self.error_limit])
        pending = [
            (index, entry)
            for index, entry in enumerate(entries)
            if entry["kind"] == "directory" and entry["size_state"] == "pending"
        ]
        initial_files = sum(1 for entry in entries if entry["kind"] == "file")
        reporter = _ProgressReporter(
            initial_files=initial_files,
            total_entries=len(pending),
            callback=progress_callback,
        )
        reporter.emit()

        if pending:
            workers = min(len(pending), os.cpu_count() or 4)
            executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="coldshelf-browse",
            )
            futures = {
                executor.submit(
                    _size_subtree,
                    Path(entry["path"]),
                    prepared.root_device,
                    cancel_event,
                    reporter,
                    self.error_limit,
                ): index
                for index, entry in pending
            }
            try:
                remaining = set(futures)
                while remaining:
                    _raise_if_cancelled(cancel_event)
                    done, remaining = wait(
                        remaining,
                        timeout=PROGRESS_WAIT_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        index = futures[future]
                        try:
                            sized = future.result()
                        except DirectorySizingCancelled:
                            raise
                        except OSError as exc:
                            sized = _unavailable_directory_entry(entries[index], str(exc))
                        entries[index].update(sized)
                        for message in sized.pop("errors", []):
                            if len(errors) >= self.error_limit:
                                break
                            errors.append(message)
                        reporter.entry_completed()
            finally:
                if cancel_event.is_set():
                    for future in futures:
                        future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)

        _raise_if_cancelled(cancel_event)
        reporter.finish()
        return _build_result(prepared, entries, errors)


class _ProgressReporter:
    """Aggregate worker progress without emitting a callback for every entry."""

    def __init__(
        self,
        *,
        initial_files: int,
        total_entries: int,
        callback: Optional[ProgressCallback],
    ) -> None:
        self.files = initial_files
        self.directories = 0
        self.completed_entries = 0
        self.total_entries = total_entries
        self.callback = callback
        self._pending_files = 0
        self._pending_directories = 0
        self._lock = Lock()

    def add(self, *, files: int = 0, directories: int = 0) -> None:
        snapshot = None
        with self._lock:
            self.files += files
            self.directories += directories
            self._pending_files += files
            self._pending_directories += directories
            if (
                self._pending_files >= PROGRESS_FILE_BATCH_SIZE
                or self._pending_directories >= PROGRESS_DIRECTORY_BATCH_SIZE
            ):
                self._pending_files = 0
                self._pending_directories = 0
                snapshot = self._snapshot()
        if snapshot and self.callback:
            self.callback(*snapshot)

    def entry_completed(self) -> None:
        with self._lock:
            self.completed_entries += 1
            self._pending_files = 0
            self._pending_directories = 0
            snapshot = self._snapshot()
        if self.callback:
            self.callback(*snapshot)

    def emit(self) -> None:
        if self.callback:
            with self._lock:
                snapshot = self._snapshot()
            self.callback(*snapshot)

    def finish(self) -> None:
        self.emit()

    def _snapshot(self) -> tuple[int, int, int, int]:
        return (
            self.files,
            self.directories,
            self.completed_entries,
            self.total_entries,
        )


def _initial_entry(child: os.DirEntry, root_device: int) -> Dict[str, Any]:
    metadata = child.stat(follow_symlinks=False)
    mode = metadata.st_mode
    is_symlink = stat_module.S_ISLNK(mode)
    is_directory = stat_module.S_ISDIR(mode)
    is_file = stat_module.S_ISREG(mode)
    kind = (
        "symlink"
        if is_symlink
        else "directory"
        if is_directory
        else "file"
        if is_file
        else "other"
    )
    allocation, fallback = _allocated_bytes(metadata)
    mount_boundary = is_directory and int(metadata.st_dev) != root_device
    if is_directory and not mount_boundary:
        logical_bytes: Optional[int] = None
        allocated_bytes: Optional[int] = None
        size_state = "pending"
        file_count = 0
        directory_count = 0
        allocation_fallback_count = 0
    else:
        logical_bytes = 0 if is_directory else int(metadata.st_size)
        allocated_bytes = 0 if mount_boundary else allocation
        size_state = "boundary" if mount_boundary else "complete"
        file_count = 1 if is_file else 0
        directory_count = 0
        allocation_fallback_count = 0 if mount_boundary else int(fallback)
    readable_mode = os.R_OK | (os.X_OK if is_directory else 0)
    writable_mode = os.W_OK | (os.X_OK if is_directory else 0)
    return {
        "name": child.name,
        "path": str(Path(child.path)),
        "kind": kind,
        "hidden": child.name.startswith("."),
        "readable": os.access(child.path, readable_mode),
        "writable": os.access(child.path, writable_mode),
        "symlink": is_symlink,
        "mount_boundary": mount_boundary,
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "allocation_fallback_count": allocation_fallback_count,
        "file_count": file_count,
        "directory_count": directory_count,
        "error_count": 0,
        "skipped_mount_count": int(mount_boundary),
        "size_state": size_state,
    }


def _size_subtree(
    root: Path,
    root_device: int,
    cancel_event: Event,
    reporter: _ProgressReporter,
    error_limit: int,
) -> Dict[str, Any]:
    _raise_if_cancelled(cancel_event)
    errors: List[str] = []
    logical_bytes = 0
    allocated_bytes = 0
    allocation_fallback_count = 0
    file_count = 0
    directory_count = 0
    error_count = 0
    skipped_mount_count = 0
    stack = [root]

    while stack:
        _raise_if_cancelled(cancel_event)
        directory = stack.pop()
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
        except OSError as exc:
            error_count += 1
            if len(errors) < error_limit:
                errors.append(f"Cannot inspect {directory}: {exc}")
            continue
        if int(directory_stat.st_dev) != root_device:
            skipped_mount_count += 1
            continue
        allocated, fallback = _allocated_bytes(directory_stat)
        allocated_bytes += allocated
        allocation_fallback_count += int(fallback)
        directory_count += 1
        reporter.add(directories=1)

        try:
            children = os.scandir(directory)
        except (OSError, PermissionError) as exc:
            error_count += 1
            if len(errors) < error_limit:
                errors.append(f"Cannot access {directory}: {exc}")
            continue
        with children:
            for child in children:
                _raise_if_cancelled(cancel_event)
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    error_count += 1
                    if len(errors) < error_limit:
                        errors.append(f"Cannot inspect {child.path}: {exc}")
                    continue
                if int(metadata.st_dev) != root_device:
                    skipped_mount_count += 1
                    continue
                mode = metadata.st_mode
                if stat_module.S_ISDIR(mode):
                    stack.append(Path(child.path))
                    continue
                allocated, fallback = _allocated_bytes(metadata)
                allocated_bytes += allocated
                allocation_fallback_count += int(fallback)
                logical_bytes += int(metadata.st_size)
                if stat_module.S_ISREG(mode):
                    file_count += 1
                    reporter.add(files=1)

    state = "partial" if error_count else "complete"
    return {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "allocation_fallback_count": allocation_fallback_count,
        "file_count": file_count,
        "directory_count": max(0, directory_count - 1),
        "error_count": error_count,
        "skipped_mount_count": skipped_mount_count,
        "size_state": state,
        "errors": errors,
    }


def _unavailable_directory_entry(entry: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "logical_bytes": 0,
        "allocated_bytes": 0,
        "allocation_fallback_count": 0,
        "file_count": 0,
        "directory_count": 0,
        "error_count": 1,
        "skipped_mount_count": 0,
        "size_state": "unavailable",
        "errors": [f"Cannot size {entry['path']}: {message}"],
    }


def _unavailable_initial_entry(child: os.DirEntry) -> Dict[str, Any]:
    """Keep an immediate name visible when its metadata cannot be inspected."""
    return {
        "name": child.name,
        "path": str(Path(child.path)),
        "kind": "other",
        "hidden": child.name.startswith("."),
        "readable": False,
        "writable": False,
        "symlink": False,
        "mount_boundary": False,
        "logical_bytes": None,
        "allocated_bytes": None,
        "allocation_fallback_count": 0,
        "file_count": 0,
        "directory_count": 0,
        "error_count": 1,
        "skipped_mount_count": 0,
        "size_state": "unavailable",
    }


def _allocated_bytes(metadata: os.stat_result) -> tuple[int, bool]:
    blocks = getattr(metadata, "st_blocks", None)
    if blocks is None:
        return int(metadata.st_size), True
    return int(blocks) * ALLOCATION_BLOCK_BYTES, False


def _build_result(
    prepared: PreparedDirectory,
    entries: List[Dict[str, Any]],
    errors: List[str],
) -> Dict[str, Any]:
    logical_bytes = sum(int(entry.get("logical_bytes") or 0) for entry in entries)
    allocated_bytes = sum(int(entry.get("allocated_bytes") or 0) for entry in entries)
    hidden_entries = [entry for entry in entries if entry["hidden"]]
    error_count = prepared.error_count + sum(int(entry["error_count"]) for entry in entries)
    return {
        "current_path": str(prepared.root),
        "parent_path": prepared.parent_path,
        "entries": entries,
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "file_count": sum(int(entry["file_count"]) for entry in entries),
        "directory_count": sum(
            int(entry["directory_count"]) + int(entry["kind"] == "directory")
            for entry in entries
        ),
        "hidden_entry_count": len(hidden_entries),
        "hidden_logical_bytes": sum(
            int(entry.get("logical_bytes") or 0) for entry in hidden_entries
        ),
        "hidden_allocated_bytes": sum(
            int(entry.get("allocated_bytes") or 0) for entry in hidden_entries
        ),
        "allocation_fallback_count": sum(
            int(entry["allocation_fallback_count"]) for entry in entries
        ),
        "error_count": error_count,
        "errors": errors,
        "measured_at": _utc_now(),
    }


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise DirectorySizingCancelled("Browse scan cancelled")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
