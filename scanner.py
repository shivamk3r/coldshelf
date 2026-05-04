"""Disk scanning functionality."""

import os
import heapq
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from collections import defaultdict
from typing import List, Dict, Optional, Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

from config import EXCLUDED_DIRECTORIES, USER_EXCLUDED_DIRECTORIES


ROOT_PROGRESS_BATCH_SIZE = 100
WORKER_FILE_PROGRESS_BATCH_SIZE = 500
WORKER_DIRECTORY_PROGRESS_BATCH_SIZE = 100
PROGRESS_DRAIN_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class ScanProgress:
    """Incremental scanner state for heuristic progress estimation."""

    files_scanned: int
    directories_discovered: int
    directories_completed: int
    errors: int
    is_finished: bool = False

    @property
    def directories_remaining(self) -> int:
        """Return discovered directories still being scanned."""
        return max(self.directories_discovered - self.directories_completed, 0)


@dataclass(frozen=True)
class _ScanProgressDelta:
    """Batched progress emitted by worker threads."""

    files_scanned: int = 0
    directories_discovered: int = 0
    directories_completed: int = 0
    errors: int = 0


class DiskScanner:
    """Scans disk and collects file information."""
    
    def __init__(
        self,
        root_path: Optional[Path] = None,
        progress_callback: Optional[Callable] = None,
        exclude_paths: Optional[List[Path]] = None,
        detailed_progress_callback: Optional[Callable[[ScanProgress], None]] = None,
    ):
        self.root_path = root_path or Path.home()
        self.progress_callback = progress_callback
        self.detailed_progress_callback = detailed_progress_callback
        self._exclude_paths_raw = exclude_paths or []
        self.files: List[Dict] = []
        self.directories: Dict[str, int] = defaultdict(int)
        self.total_scanned = 0
        self.errors: List[str] = []
        
    def scan(self) -> Dict:
        """Scan the filesystem and return file information."""
        self.files = []
        self.directories = defaultdict(int)
        self.total_scanned = 0
        self.errors = []
        
        # Pre-compute all excluded path prefixes once (avoids per-file Path.home() calls)
        excluded = set()
        home_str = str(Path.home())
        for d in EXCLUDED_DIRECTORIES:
            excluded.add(d)
            if not os.path.isabs(d):
                excluded.add(os.path.join(home_str, d))
        if self.root_path == Path.home():
            for d in USER_EXCLUDED_DIRECTORIES:
                excluded.add(os.path.join(home_str, d))
        for ep in self._exclude_paths_raw:
            excluded.add(str(Path(ep).resolve()))
        excluded_prefixes = tuple(sorted(excluded))
        
        root_str = str(self.root_path)
        use_progress_events = bool(
            self.progress_callback or self.detailed_progress_callback
        )
        progress_queue = Queue() if use_progress_events else None
        root_file_batch_size = (
            ROOT_PROGRESS_BATCH_SIZE
            if self.progress_callback
            else WORKER_FILE_PROGRESS_BATCH_SIZE
        )
        progress_files_scanned = 0
        progress_directories_discovered = 1
        progress_directories_completed = 0
        progress_errors = 0
        last_legacy_progress_count = 0

        def emit_progress(is_finished=False, force_legacy=False):
            nonlocal last_legacy_progress_count

            self.total_scanned = progress_files_scanned

            if self.detailed_progress_callback:
                self.detailed_progress_callback(
                    ScanProgress(
                        files_scanned=progress_files_scanned,
                        directories_discovered=progress_directories_discovered,
                        directories_completed=progress_directories_completed,
                        errors=progress_errors,
                        is_finished=is_finished,
                    )
                )

            if (
                self.progress_callback
                and progress_files_scanned > last_legacy_progress_count
                and (
                    force_legacy
                    or progress_files_scanned - last_legacy_progress_count
                    >= ROOT_PROGRESS_BATCH_SIZE
                )
            ):
                self.progress_callback(progress_files_scanned)
                last_legacy_progress_count = progress_files_scanned

        def apply_progress_delta(delta):
            nonlocal progress_files_scanned
            nonlocal progress_directories_discovered
            nonlocal progress_directories_completed
            nonlocal progress_errors

            progress_files_scanned += delta.files_scanned
            progress_directories_discovered += delta.directories_discovered
            progress_directories_completed += delta.directories_completed
            progress_errors += delta.errors
            emit_progress()

        def drain_progress_queue():
            if progress_queue is None:
                return
            while True:
                try:
                    delta = progress_queue.get_nowait()
                except Empty:
                    break
                apply_progress_delta(delta)

        if self.detailed_progress_callback:
            emit_progress()
        
        # Collect top-level directories for parallel scanning
        top_dirs = []
        root_file_delta = 0
        root_directory_delta = 0
        try:
            with os.scandir(root_str) as it:
                for entry in it:
                    try:
                        if entry.is_file():
                            st = entry.stat()
                            self.files.append({
                                'path': entry.path,
                                'size': st.st_size,
                                'atime': st.st_atime,
                                'mtime': st.st_mtime,
                                'ctime': st.st_ctime,
                            })
                            self.directories[root_str] += st.st_size
                            progress_files_scanned += 1
                            root_file_delta += 1
                            if (
                                use_progress_events
                                and root_file_delta >= root_file_batch_size
                            ):
                                root_file_delta = 0
                                emit_progress()
                        elif entry.is_dir(follow_symlinks=False):
                            if not entry.path.startswith(excluded_prefixes):
                                top_dirs.append(entry.path)
                                progress_directories_discovered += 1
                                root_directory_delta += 1
                                if (
                                    self.detailed_progress_callback
                                    and root_directory_delta
                                    >= WORKER_DIRECTORY_PROGRESS_BATCH_SIZE
                                ):
                                    root_directory_delta = 0
                                    emit_progress()
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as e:
            self.errors.append(f"Cannot access {root_str}: {e}")
            progress_errors += 1

        progress_directories_completed += 1
        if use_progress_events and (
            root_file_delta or root_directory_delta or progress_errors
        ):
            emit_progress(force_legacy=not top_dirs)
        
        # Scan subdirectories in parallel threads (os.stat releases GIL)
        if top_dirs:
            n_workers = min(len(top_dirs), os.cpu_count() or 4)
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(_scan_subtree, d, excluded_prefixes, progress_queue): d
                    for d in top_dirs
                }
                if progress_queue is None:
                    for future in as_completed(futures):
                        sub_files, sub_dirs, sub_errors = future.result()
                        self.files.extend(sub_files)
                        for k, v in sub_dirs.items():
                            self.directories[k] += v
                        self.errors.extend(sub_errors)
                        self.total_scanned += len(sub_files)
                else:
                    pending = set(futures)
                    while pending:
                        done, pending = wait(
                            pending,
                            timeout=PROGRESS_DRAIN_INTERVAL_SECONDS,
                            return_when=FIRST_COMPLETED,
                        )
                        drain_progress_queue()
                        for future in done:
                            sub_files, sub_dirs, sub_errors = future.result()
                            self.files.extend(sub_files)
                            for k, v in sub_dirs.items():
                                self.directories[k] += v
                            self.errors.extend(sub_errors)
                        drain_progress_queue()

        if use_progress_events:
            drain_progress_queue()
            progress_files_scanned = len(self.files)
            progress_errors = len(self.errors)
            progress_directories_completed = progress_directories_discovered
            emit_progress(is_finished=True, force_legacy=True)
        else:
            self.total_scanned = len(self.files)
        
        return {
            'files': self.files,
            'directories': dict(self.directories),
            'total_scanned': self.total_scanned,
            'errors': self.errors
        }
    
    def get_largest_directories(self, limit: int = 20) -> List[tuple]:
        """Get the largest directories by size."""
        return heapq.nlargest(limit, self.directories.items(), key=lambda x: x[1])
    
    def get_largest_files(self, limit: int = 20) -> List[Dict]:
        """Get the largest files."""
        return heapq.nlargest(limit, self.files, key=lambda x: x['size'])


def _scan_subtree(directory, excluded_prefixes, progress_queue=None):
    """Scan a directory subtree. Runs in a thread pool worker."""
    files = []
    dirs = defaultdict(int)
    errors = []
    reporter = _WorkerProgressReporter(progress_queue) if progress_queue else None
    try:
        _scan_recursive(directory, excluded_prefixes, files, dirs, errors, reporter)
    finally:
        if reporter:
            reporter.flush()
    return files, dict(dirs), errors


def _scan_recursive(directory, excluded_prefixes, files, dirs, errors, reporter=None):
    """Recursively scan using os.scandir for maximum performance."""
    try:
        scandir_it = os.scandir(directory)
    except (PermissionError, OSError) as e:
        errors.append(f"Cannot access {directory}: {e}")
        if reporter:
            reporter.error()
            reporter.directory_completed()
        return
    
    with scandir_it:
        for entry in scandir_it:
            try:
                if entry.is_file():
                    st = entry.stat()
                    files.append({
                        'path': entry.path,
                        'size': st.st_size,
                        'atime': st.st_atime,
                        'mtime': st.st_mtime,
                        'ctime': st.st_ctime,
                    })
                    dirs[directory] += st.st_size
                    if reporter:
                        reporter.file()
                elif entry.is_dir(follow_symlinks=False):
                    if not entry.path.startswith(excluded_prefixes):
                        if reporter:
                            reporter.directory_discovered()
                        _scan_recursive(
                            entry.path,
                            excluded_prefixes,
                            files,
                            dirs,
                            errors,
                            reporter,
                        )
            except (PermissionError, OSError):
                continue
    if reporter:
        reporter.directory_completed()


class _WorkerProgressReporter:
    """Batch worker-thread scan events before handing them to the main thread."""

    def __init__(self, progress_queue):
        self.progress_queue = progress_queue
        self.files_scanned = 0
        self.directories_discovered = 0
        self.directories_completed = 0
        self.errors = 0

    def file(self):
        self.files_scanned += 1
        if self.files_scanned >= WORKER_FILE_PROGRESS_BATCH_SIZE:
            self.flush()

    def directory_discovered(self):
        self.directories_discovered += 1
        self._flush_if_directory_batch_ready()

    def directory_completed(self):
        self.directories_completed += 1
        self._flush_if_directory_batch_ready()

    def error(self):
        self.errors += 1
        self.flush()

    def _flush_if_directory_batch_ready(self):
        directory_events = self.directories_discovered + self.directories_completed
        if directory_events >= WORKER_DIRECTORY_PROGRESS_BATCH_SIZE:
            self.flush()

    def flush(self):
        if not (
            self.files_scanned
            or self.directories_discovered
            or self.directories_completed
            or self.errors
        ):
            return
        self.progress_queue.put(
            _ScanProgressDelta(
                files_scanned=self.files_scanned,
                directories_discovered=self.directories_discovered,
                directories_completed=self.directories_completed,
                errors=self.errors,
            )
        )
        self.files_scanned = 0
        self.directories_discovered = 0
        self.directories_completed = 0
        self.errors = 0
