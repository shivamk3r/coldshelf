"""Archive target resolution for the archive workflow."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .drive_detector import select_external_drive


@dataclass(frozen=True)
class ArchiveTarget:
    """Resolved archive destination."""

    root: Path
    archive_base: Path
    label: str
    source: str


class ArchiveTargetError(RuntimeError):
    """Raised when an archive destination cannot be resolved."""


def is_writable_path(path: Path) -> bool:
    """Return whether a path is writable by the current process."""
    return os.access(path, os.W_OK)


def resolve_archive_target(
    target_path: Optional[Path] = None,
    external_path: Optional[Path] = None,
) -> ArchiveTarget:
    """Resolve archive target using target-path, external-path, auto-detect order."""
    if target_path:
        try:
            target_path.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            raise ArchiveTargetError(
                f"Cannot create archive folder {target_path}: {exc}"
            ) from exc
        return ArchiveTarget(
            root=target_path,
            archive_base=target_path / "archived_files",
            label="local folder",
            source="target_path",
        )

    if external_path:
        if not external_path.exists() or not is_writable_path(external_path):
            raise ArchiveTargetError(
                f"Path {external_path} does not exist or is not writable"
            )
        return ArchiveTarget(
            root=external_path,
            archive_base=external_path / "archived_files",
            label="external drive",
            source="external_path",
        )

    try:
        archive_root = select_external_drive()
    except Exception as exc:
        raise ArchiveTargetError(f"Error detecting external drive: {exc}") from exc

    if not archive_root:
        raise ArchiveTargetError(
            "No external drive detected. Use --external-path or --target-path"
        )

    return ArchiveTarget(
        root=archive_root,
        archive_base=archive_root / "archived_files",
        label="external drive",
        source="auto_detected",
    )
