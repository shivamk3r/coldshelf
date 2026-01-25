"""External SSD detection and management."""

import subprocess
import os
from pathlib import Path
from typing import List, Optional, Dict
from utils import get_available_space


def get_mounted_volumes() -> List[Dict[str, str]]:
    """Get list of all mounted volumes using diskutil."""
    volumes = []
    try:
        result = subprocess.run(
            ['diskutil', 'list'],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse diskutil output to find mounted volumes
        lines = result.stdout.split('\n')
        for line in lines:
            if '/dev/disk' in line:
                # Extract disk identifier
                parts = line.split()
                if parts:
                    disk_id = parts[0]
                    # Get mount point
                    try:
                        mount_result = subprocess.run(
                            ['diskutil', 'info', disk_id],
                            capture_output=True,
                            text=True,
                            check=True
                        )
                        mount_point = None
                        volume_name = None
                        for info_line in mount_result.stdout.split('\n'):
                            if 'Mount Point' in info_line:
                                mount_point = info_line.split(':')[-1].strip()
                            if 'Volume Name' in info_line:
                                volume_name = info_line.split(':')[-1].strip()
                        
                        if mount_point and mount_point != 'Not applicable (no file system)':
                            volumes.append({
                                'path': mount_point,
                                'name': volume_name or mount_point,
                                'disk_id': disk_id
                            })
                    except subprocess.CalledProcessError:
                        continue
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: use /Volumes directory
        volumes_dir = Path('/Volumes')
        if volumes_dir.exists():
            for item in volumes_dir.iterdir():
                if item.is_dir() and item.is_mount():
                    volumes.append({
                        'path': str(item),
                        'name': item.name,
                        'disk_id': None
                    })
    
    return volumes


def is_external_drive(path: Path) -> bool:
    """Check if a path is on an external drive (not the main system drive)."""
    try:
        # Get the mount point
        path = Path(path).resolve()
        
        # Check if it's in /Volumes (external drives are typically mounted there)
        if '/Volumes/' in str(path):
            return True
        
        # Check if it's the root filesystem
        if path == Path('/'):
            return False
        
        # Get device info
        stat = os.stat(path)
        # Compare with root device
        root_stat = os.stat('/')
        
        # If different device, likely external
        return stat.st_dev != root_stat.st_dev
    except (OSError, PermissionError):
        return False


def detect_external_ssds() -> List[Dict[str, str]]:
    """Detect external SSDs/hard drives."""
    all_volumes = get_mounted_volumes()
    external_drives = []
    
    for volume in all_volumes:
        vol_path = Path(volume['path'])
        
        # Skip system volumes
        if vol_path == Path('/') or str(vol_path).startswith('/System'):
            continue
        
        # Check if it's external
        if is_external_drive(vol_path) or '/Volumes/' in str(vol_path):
            # Check if writable
            if os.access(vol_path, os.W_OK):
                available_space = get_available_space(vol_path)
                volume['available_space'] = available_space
                external_drives.append(volume)
    
    return external_drives


def select_external_ssd(manual_path: Optional[str] = None) -> Optional[Path]:
    """Select external SSD, either auto-detected or manually specified."""
    if manual_path:
        path = Path(manual_path)
        if path.exists() and os.access(path, os.W_OK):
            return path
        else:
            raise ValueError(f"Path {manual_path} does not exist or is not writable")
    
    # Auto-detect
    external_drives = detect_external_ssds()
    
    if not external_drives:
        return None
    
    # Return the first available external drive
    # In a full implementation, this could prompt the user to choose
    return Path(external_drives[0]['path'])
