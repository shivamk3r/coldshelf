"""Shared inclusion policy for protected-folder catalogs and snapshots."""

from pathlib import PurePath
from typing import Union


IGNORED_BACKUP_FILE_NAMES = frozenset({".DS_Store"})
RESTIC_EXCLUDE_PATTERNS = ("**/.DS_Store",)


def is_ignored_backup_path(path: Union[str, PurePath]) -> bool:
    """Return whether a relative catalog or snapshot path is intentionally ignored."""
    return PurePath(str(path)).name in IGNORED_BACKUP_FILE_NAMES
