"""Disk scanning functionality."""

import os
import heapq
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import EXCLUDED_DIRECTORIES, USER_EXCLUDED_DIRECTORIES


class DiskScanner:
    """Scans disk and collects file information."""
    
    def __init__(self, root_path: Optional[Path] = None, progress_callback: Optional[Callable] = None, exclude_paths: Optional[List[Path]] = None):
        self.root_path = root_path or Path.home()
        self.progress_callback = progress_callback
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
        
        # Collect top-level directories for parallel scanning
        top_dirs = []
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
                            self.total_scanned += 1
                            if self.progress_callback and self.total_scanned % 100 == 0:
                                self.progress_callback(self.total_scanned)
                        elif entry.is_dir(follow_symlinks=False):
                            if not entry.path.startswith(excluded_prefixes):
                                top_dirs.append(entry.path)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as e:
            self.errors.append(f"Cannot access {root_str}: {e}")
        
        # Scan subdirectories in parallel threads (os.stat releases GIL)
        if top_dirs:
            n_workers = min(len(top_dirs), os.cpu_count() or 4)
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(_scan_subtree, d, excluded_prefixes): d
                    for d in top_dirs
                }
                for future in as_completed(futures):
                    sub_files, sub_dirs, sub_errors = future.result()
                    self.files.extend(sub_files)
                    for k, v in sub_dirs.items():
                        self.directories[k] += v
                    self.errors.extend(sub_errors)
                    self.total_scanned += len(sub_files)
                    if self.progress_callback:
                        self.progress_callback(self.total_scanned)
        
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


def _scan_subtree(directory, excluded_prefixes):
    """Scan a directory subtree. Runs in a thread pool worker."""
    files = []
    dirs = defaultdict(int)
    errors = []
    _scan_recursive(directory, excluded_prefixes, files, dirs, errors)
    return files, dict(dirs), errors


def _scan_recursive(directory, excluded_prefixes, files, dirs, errors):
    """Recursively scan using os.scandir for maximum performance."""
    try:
        scandir_it = os.scandir(directory)
    except (PermissionError, OSError) as e:
        errors.append(f"Cannot access {directory}: {e}")
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
                elif entry.is_dir(follow_symlinks=False):
                    if not entry.path.startswith(excluded_prefixes):
                        _scan_recursive(entry.path, excluded_prefixes, files, dirs, errors)
            except (PermissionError, OSError):
                continue
