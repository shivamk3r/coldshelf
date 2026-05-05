"""External drive detection for Unix-like systems."""

import os
import platform
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from utils import get_available_space


LINUX_EXTERNAL_MOUNT_PREFIXES = (
    "/media/",
    "/mnt/",
    "/run/media/",
)

LINUX_PSEUDO_FILESYSTEMS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)


def get_mounted_volumes() -> List[Dict[str, str]]:
    """Get mounted volumes for the current Unix-like platform."""
    system = platform.system()
    if system == "Darwin":
        return _get_macos_mounted_volumes()
    if system == "Linux":
        return _get_linux_mounted_volumes()
    return []


def _get_macos_mounted_volumes() -> List[Dict[str, str]]:
    """Get mounted macOS volumes using diskutil with a /Volumes fallback."""
    volumes = []
    try:
        result = subprocess.run(
            ["diskutil", "list"],
            capture_output=True,
            text=True,
            check=True,
        )

        for line in result.stdout.splitlines():
            if "/dev/disk" not in line:
                continue

            parts = line.split()
            if not parts:
                continue

            disk_id = parts[0]
            try:
                mount_result = subprocess.run(
                    ["diskutil", "info", disk_id],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError:
                continue

            mount_point = None
            volume_name = None
            for info_line in mount_result.stdout.splitlines():
                if "Mount Point" in info_line:
                    mount_point = info_line.split(":", 1)[-1].strip()
                if "Volume Name" in info_line:
                    volume_name = info_line.split(":", 1)[-1].strip()

            if mount_point and mount_point != "Not applicable (no file system)":
                volumes.append(
                    {
                        "path": mount_point,
                        "name": volume_name or mount_point,
                        "device": disk_id,
                    }
                )
    except (subprocess.CalledProcessError, FileNotFoundError):
        volumes_dir = Path("/Volumes")
        if volumes_dir.exists():
            for item in volumes_dir.iterdir():
                if item.is_dir() and item.is_mount():
                    volumes.append(
                        {
                            "path": str(item),
                            "name": item.name,
                            "device": "",
                        }
                    )

    return volumes


def _get_linux_mounted_volumes(
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> List[Dict[str, str]]:
    """Get Linux mounted volumes from /proc/self/mountinfo."""
    try:
        mountinfo_lines = mountinfo_path.read_text().splitlines()
    except (OSError, PermissionError):
        return []

    return list(_parse_linux_mountinfo(mountinfo_lines))


def _parse_linux_mountinfo(lines: Iterable[str]) -> Iterable[Dict[str, str]]:
    """Parse Linux mountinfo records into volume dictionaries."""
    for line in lines:
        parts = line.split()
        if len(parts) < 10 or "-" not in parts:
            continue

        separator_index = parts.index("-")
        if separator_index + 2 >= len(parts):
            continue

        mount_point = _decode_mountinfo_path(parts[4])
        fs_type = parts[separator_index + 1]
        source = parts[separator_index + 2]

        if mount_point == "/" or fs_type in LINUX_PSEUDO_FILESYSTEMS:
            continue
        if not _is_linux_external_mount_path(mount_point):
            continue

        yield {
            "path": mount_point,
            "name": Path(mount_point).name or mount_point,
            "device": source,
            "fs_type": fs_type,
        }


def _decode_mountinfo_path(path: str) -> str:
    """Decode common octal escapes used in Linux mountinfo paths."""
    return (
        path.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _is_linux_external_mount_path(path: str) -> bool:
    """Return True for common user-mounted external drive locations."""
    return path.startswith(LINUX_EXTERNAL_MOUNT_PREFIXES)


def is_external_drive(path: Path) -> bool:
    """Check if a path appears to be on an external drive."""
    try:
        path = Path(path).resolve()

        if path == Path("/"):
            return False

        path_str = str(path)
        if path_str.startswith("/Volumes/"):
            return True
        if _is_linux_external_mount_path(path_str):
            return True

        stat = os.stat(path)
        root_stat = os.stat("/")
        return stat.st_dev != root_stat.st_dev
    except (OSError, PermissionError):
        return False


def detect_external_drives() -> List[Dict[str, str]]:
    """Detect writable external drives."""
    external_drives = []

    for volume in get_mounted_volumes():
        vol_path = Path(volume["path"])

        if vol_path == Path("/") or str(vol_path).startswith("/System"):
            continue
        if not is_external_drive(vol_path):
            continue
        if not os.access(vol_path, os.W_OK):
            continue

        volume["available_space"] = get_available_space(vol_path)
        external_drives.append(volume)

    return external_drives


def select_external_drive(manual_path: Optional[str] = None) -> Optional[Path]:
    """Select an external drive, either manually specified or auto-detected."""
    if manual_path:
        path = Path(manual_path)
        if path.exists() and os.access(path, os.W_OK):
            return path
        raise ValueError(f"Path {manual_path} does not exist or is not writable")

    external_drives = detect_external_drives()
    if not external_drives:
        return None

    return Path(external_drives[0]["path"])
