"""Stable external-volume discovery for protected-folder targets on macOS."""

import os
import ctypes
import platform
import plistlib
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


class VolumeError(RuntimeError):
    """Raised when a mounted volume cannot be safely identified."""


@dataclass(frozen=True)
class VolumeInfo:
    volume_uuid: str
    name: str
    mount_path: str
    filesystem: str
    device_identifier: str
    total_bytes: int
    free_bytes: int
    writable: bool
    internal: bool
    removable: bool

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class MacOSVolumeProvider:
    """Discover writable external volumes using diskutil's plist interface."""

    def __init__(
        self,
        volumes_root: Path = Path("/Volumes"),
        run: Callable = subprocess.run,
        system: Callable[[], str] = platform.system,
    ):
        self.volumes_root = volumes_root
        self._run = run
        self._system = system

    def ensure_supported(self) -> None:
        if self._system() != "Darwin":
            raise VolumeError("Protected folders are currently supported on macOS only")

    def source_volume_uuid(self, path: Path) -> str:
        """Identify the actual containing filesystem, including APFS firmlinks.

        getattrlist accepts a file path; diskutil info only accepts disk/mount
        identifiers. Never infer this UUID from '/' or from the boot volume.
        """
        self.ensure_supported()
        return _source_volume_uuid(path)

    def list_volumes(self) -> List[VolumeInfo]:
        self.ensure_supported()
        if not self.volumes_root.exists():
            return []
        volumes: List[VolumeInfo] = []
        for path in sorted(self.volumes_root.iterdir(), key=lambda item: item.name.lower()):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                volume = self.resolve(path)
            except VolumeError:
                continue
            if volume.internal or not volume.writable:
                continue
            volumes.append(volume)
        return volumes

    def resolve(self, mount_path: Path) -> VolumeInfo:
        self.ensure_supported()
        mount_path = Path(mount_path).expanduser().resolve(strict=True)
        try:
            mount_path.relative_to(self.volumes_root.resolve(strict=True))
        except ValueError as exc:
            raise VolumeError(f"Target is not mounted under {self.volumes_root}: {mount_path}") from exc

        result = self._run(
            ["diskutil", "info", "-plist", str(mount_path)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            error = _decode_output(result.stderr).strip() or "diskutil could not inspect volume"
            raise VolumeError(error)
        try:
            payload = plistlib.loads(_bytes_output(result.stdout))
        except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
            raise VolumeError(f"Invalid diskutil response for {mount_path}") from exc

        volume_uuid = str(payload.get("VolumeUUID") or "").strip()
        actual_mount = str(payload.get("MountPoint") or "").strip()
        if not volume_uuid or not actual_mount:
            raise VolumeError(f"Volume {mount_path} has no stable UUID or mount point")
        try:
            if Path(actual_mount).resolve(strict=True) != mount_path:
                raise VolumeError(f"Volume mount changed while inspecting {mount_path}")
        except OSError as exc:
            raise VolumeError(f"Volume is no longer mounted: {mount_path}") from exc

        writable = bool(payload.get("Writable", os.access(mount_path, os.W_OK)))
        if not writable:
            raise VolumeError(f"Volume is read-only: {mount_path}")

        return VolumeInfo(
            volume_uuid=volume_uuid,
            name=str(payload.get("VolumeName") or mount_path.name),
            mount_path=str(mount_path),
            filesystem=str(
                payload.get("FilesystemName")
                or payload.get("FilesystemType")
                or payload.get("FileSystemPersonality")
                or "unknown"
            ),
            device_identifier=str(payload.get("DeviceIdentifier") or ""),
            total_bytes=int(payload.get("TotalSize") or payload.get("VolumeSize") or 0),
            free_bytes=int(payload.get("FreeSpace") or payload.get("VolumeFreeSpace") or 0),
            writable=writable,
            internal=bool(payload.get("Internal", False)),
            removable=bool(payload.get("RemovableMedia", True)),
        )

    def find_by_uuid(self, volume_uuid: str) -> Optional[VolumeInfo]:
        for volume in self.list_volumes():
            if volume.volume_uuid == volume_uuid:
                return volume
        return None


def _bytes_output(value) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode()


def _source_volume_uuid(path: Path) -> str:
    class AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                    ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                    ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                    ("forkattr", ctypes.c_uint32)]

    # sys/attr.h: ATTR_BIT_MAP_COUNT, ATTR_VOL_INFO | ATTR_VOL_UUID.
    attributes = AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
    result = ctypes.create_string_buffer(20)  # uint32 length, uuid_t
    libc = ctypes.CDLL(None, use_errno=True)
    getattrlist = libc.getattrlist
    getattrlist.argtypes = [ctypes.c_char_p, ctypes.POINTER(AttrList),
                           ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
    getattrlist.restype = ctypes.c_int
    if getattrlist(os.fsencode(path), ctypes.byref(attributes), result, 20, 0) != 0:
        raise VolumeError(f"Cannot identify source volume: {os.strerror(ctypes.get_errno())}")
    if int.from_bytes(result.raw[:4], byteorder="little") != 20:
        raise VolumeError("Source filesystem returned an invalid volume identity")
    value = uuid.UUID(bytes=result.raw[4:20])
    if value.int == 0:
        raise VolumeError("Source filesystem has no persistent volume UUID")
    return str(value)


def _decode_output(value) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value or "")
