"""Disk scanning functionality."""

import os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Callable
from collections import defaultdict

from config import EXCLUDED_DIRECTORIES, USER_EXCLUDED_DIRECTORIES
from utils import get_file_info, is_excluded_path, format_size


class DiskScanner:
    """Scans disk and collects file information."""
    
    def __init__(self, root_path: Optional[Path] = None, progress_callback: Optional[Callable] = None):
        """Initialize scanner.
        
        Args:
            root_path: Root directory to scan (default: user home)
            progress_callback: Optional callback for progress updates
        """
        self.root_path = root_path or Path.home()
        self.progress_callback = progress_callback
        self.files: List[Dict] = []
        self.directories: Dict[Path, int] = defaultdict(int)
        self.total_scanned = 0
        self.errors = []
        
    def scan(self) -> Dict:
        """Scan the filesystem and return file information."""
        self.files = []
        self.directories = defaultdict(int)
        self.total_scanned = 0
        self.errors = []
        
        all_excluded = EXCLUDED_DIRECTORIES.copy()
        if self.root_path == Path.home():
            all_excluded.extend([str(Path.home() / d) for d in USER_EXCLUDED_DIRECTORIES])
        
        self._scan_directory(self.root_path, all_excluded)
        
        return {
            'files': self.files,
            'directories': dict(self.directories),
            'total_scanned': self.total_scanned,
            'errors': self.errors
        }
    
    def _scan_directory(self, directory: Path, excluded_dirs: List[str]):
        """Recursively scan a directory."""
        if is_excluded_path(directory, excluded_dirs):
            return
        
        try:
            items = list(directory.iterdir())
        except (PermissionError, OSError) as e:
            self.errors.append(f"Cannot access {directory}: {e}")
            return
        
        for item in items:
            if self.progress_callback and self.total_scanned % 100 == 0:
                self.progress_callback(self.total_scanned)
            
            if is_excluded_path(item, excluded_dirs):
                continue
            
            try:
                if item.is_file():
                    file_info = get_file_info(item)
                    if file_info:
                        self.files.append(file_info)
                        # Add to parent directory size
                        self.directories[item.parent] += file_info['size']
                        self.total_scanned += 1
                elif item.is_dir():
                    # Recursively scan subdirectories
                    self._scan_directory(item, excluded_dirs)
            except (PermissionError, OSError) as e:
                self.errors.append(f"Cannot access {item}: {e}")
                continue
    
    def get_largest_directories(self, limit: int = 20) -> List[tuple]:
        """Get the largest directories by size."""
        sorted_dirs = sorted(
            self.directories.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return sorted_dirs[:limit]
    
    def get_largest_files(self, limit: int = 20) -> List[Dict]:
        """Get the largest files."""
        sorted_files = sorted(
            self.files,
            key=lambda x: x['size'],
            reverse=True
        )
        return sorted_files[:limit]
