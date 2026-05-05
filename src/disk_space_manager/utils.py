"""Utility functions for the disk cleaner."""

import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def get_file_info(file_path: Path) -> Optional[dict]:
    """Get file information including size and access time."""
    try:
        stat = file_path.stat()
        return {
            'path': file_path,
            'size': stat.st_size,
            'accessed': datetime.fromtimestamp(stat.st_atime),
            'modified': datetime.fromtimestamp(stat.st_mtime),
            'created': datetime.fromtimestamp(stat.st_ctime),
            'is_dir': file_path.is_dir(),
        }
    except (OSError, PermissionError):
        return None


def is_excluded_path(path: Path, excluded_dirs: list) -> bool:
    """Check if a path should be excluded from scanning."""
    path_str = str(path)
    for excluded in excluded_dirs:
        if path_str.startswith(excluded) or path_str.startswith(str(Path.home() / excluded)):
            return True
    return False


def get_directory_size(directory: Path) -> int:
    """Calculate total size of a directory."""
    total_size = 0
    try:
        for item in directory.rglob('*'):
            if item.is_file():
                try:
                    total_size += item.stat().st_size
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return total_size


def get_available_space(path: Path) -> int:
    """Get available disk space for a given path."""
    stat = shutil.disk_usage(path)
    return stat.free


def create_symlink_or_copy(source: Path, target: Path, use_symlink: bool = True) -> bool:
    """Create a symlink or copy file from source to target."""
    try:
        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)
        
        if use_symlink:
            # Create symlink
            if target.exists():
                target.unlink()
            target.symlink_to(source)
        else:
            # Copy file
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)
        return True
    except (OSError, PermissionError) as e:
        print(f"Error creating link/copy: {e}")
        return False


def safe_delete(path: Path) -> bool:
    """Safely delete a file or directory."""
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        return True
    except (OSError, PermissionError) as e:
        print(f"Error deleting {path}: {e}")
        return False


def preserve_structure_move(source: Path, target_base: Path, source_base: Path) -> Path:
    """Move file preserving directory structure relative to source_base."""
    # Get relative path from source_base
    try:
        relative_path = source.relative_to(source_base)
    except ValueError:
        # If source is not under source_base, use just the filename
        relative_path = Path(source.name)
    
    # Create target path
    target = target_base / relative_path
    return target
