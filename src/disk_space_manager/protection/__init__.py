"""Replicated, Restic-backed protected-folder support."""

from .repository import DEFAULT_PROTECTION_DB_PATH, ProtectionRepository
from .service import ProtectionError, ProtectionService

__all__ = [
    "DEFAULT_PROTECTION_DB_PATH",
    "ProtectionError",
    "ProtectionRepository",
    "ProtectionService",
]
