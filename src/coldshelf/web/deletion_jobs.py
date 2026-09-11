"""Guarded, persistent Browse deletion previews and delayed jobs."""

from __future__ import annotations

import copy
import logging
import os
import stat as stat_module
import uuid
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, RLock, Thread
from typing import Any, Callable, Dict, Iterable, List, Optional

from coldshelf.config import ACTION_LOG_FILE
from coldshelf.executor import ActionExecutor
from coldshelf.protection.repository import ProtectionRepository

from .browse_jobs import BrowseJobManager
from .io_queue import HeavyIOQueue
from .repository import ActiveBrowseDeletionError, WebRepository
from .security import DEFAULT_TOKEN_FILE


logger = logging.getLogger(__name__)

DELETE_DELAY_SECONDS = 10
PREVIEW_TTL_SECONDS = 5 * 60
MAX_PREVIEWS = 128
ACTIVE_STATUSES = frozenset({"scheduled", "queued", "running"})


class BrowseDeletionError(RuntimeError):
    """Base class for user-correctable Browse deletion failures."""


class BrowseDeletionNotFound(BrowseDeletionError):
    """Raised when a preview or deletion does not exist."""


class BrowseDeletionConflict(BrowseDeletionError):
    """Raised when state changed or the Undo window closed."""


@dataclass(frozen=True)
class DeletionEligibility:
    eligible: bool
    blocker: Optional[str]
    allowed_root: Optional[Path]


class BrowseDeletionPolicy:
    """Centralize Browse deletion eligibility and execution-time guards."""

    def __init__(
        self,
        protection_repository: ProtectionRepository,
        *,
        state_paths: Iterable[Path] = (),
    ) -> None:
        self.protection_repository = protection_repository
        paths = {
            Path(ACTION_LOG_FILE).expanduser(),
            Path(DEFAULT_TOKEN_FILE).expanduser(),
            *(Path(path).expanduser() for path in state_paths),
        }
        expanded: set[Path] = set()
        for path in paths:
            expanded.add(path)
            expanded.add(Path(f"{path}-wal"))
            expanded.add(Path(f"{path}-shm"))
        self.state_paths = tuple(expanded)

    def annotate_entries(
        self,
        entries: List[Dict[str, Any]],
        current_path: Path,
        roots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        try:
            protected_roots = self.protection_repository.protected_roots()
        except Exception:
            protected_roots = None
        annotated: List[Dict[str, Any]] = []
        for entry in entries:
            item = copy.deepcopy(entry)
            if protected_roots is None:
                eligibility = DeletionEligibility(
                    False,
                    "Protected-folder status is unavailable",
                    None,
                )
            else:
                eligibility = self.evaluate_entry(
                    item,
                    current_path,
                    roots,
                    protected_roots=protected_roots,
                )
            item["deletion_eligible"] = eligibility.eligible
            item["deletion_blocker"] = eligibility.blocker
            annotated.append(item)
        return annotated

    def evaluate_entry(
        self,
        entry: Dict[str, Any],
        current_path: Path,
        roots: List[Dict[str, Any]],
        *,
        protected_roots: Optional[List[Path]] = None,
    ) -> DeletionEligibility:
        path = Path(str(entry["path"]))
        if entry.get("symlink"):
            return DeletionEligibility(
                False, "Symbolic links cannot be deleted from Browse", None
            )
        if entry.get("kind") not in {"file", "directory"}:
            return DeletionEligibility(
                False, "Only regular files and folders can be deleted", None
            )
        if entry.get("mount_boundary"):
            return DeletionEligibility(
                False, "Drive roots and mounted filesystem boundaries are protected", None
            )
        if not entry.get("readable"):
            return DeletionEligibility(False, "The item is not readable", None)
        if entry.get("size_state") != "complete":
            return DeletionEligibility(
                False, "Wait for a complete, error-free size scan", None
            )
        if int(entry.get("error_count") or 0) > 0:
            return DeletionEligibility(
                False, "Folders with scan errors cannot be deleted", None
            )
        if int(entry.get("skipped_mount_count") or 0) > 0:
            return DeletionEligibility(
                False, "Folders containing mounted filesystems cannot be deleted", None
            )

        try:
            current = current_path.resolve(strict=True)
            candidate = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return DeletionEligibility(False, "The item no longer exists", None)
        if candidate.parent != current:
            return DeletionEligibility(
                False, "Only items directly visible in this folder can be deleted", None
            )

        allowed_root = _find_allowed_root(candidate, roots)
        if allowed_root is None:
            return DeletionEligibility(
                False,
                "Deletion is limited to items below Home or a detected writable drive",
                None,
            )
        if not os.access(current, os.W_OK | os.X_OK):
            return DeletionEligibility(
                False, "The containing folder is not writable", allowed_root
            )

        protected = (
            protected_roots
            if protected_roots is not None
            else self.protection_repository.protected_roots()
        )
        for root in protected:
            if _paths_overlap(candidate, Path(root).expanduser().resolve(strict=False)):
                return DeletionEligibility(
                    False, "Protected folders and their ancestors cannot be deleted", allowed_root
                )
        for state_path in self.state_paths:
            if _paths_overlap(
                candidate, state_path.expanduser().resolve(strict=False)
            ):
                return DeletionEligibility(
                    False, "Active coldshelf state cannot be deleted", allowed_root
                )
        return DeletionEligibility(True, None, allowed_root)

    def validate_persisted_item(
        self,
        batch: Dict[str, Any],
        item: Dict[str, Any],
        roots: List[Dict[str, Any]],
    ) -> Optional[str]:
        path = Path(str(item["path"]))
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return "The item no longer exists"
        except OSError as exc:
            return f"The item cannot be inspected: {exc}"

        if not _fingerprint_matches(metadata, item):
            return "The item changed after deletion was scheduled"
        if item["kind"] == "file" and not stat_module.S_ISREG(metadata.st_mode):
            return "The selected path is no longer a regular file"
        if item["kind"] == "directory" and not stat_module.S_ISDIR(metadata.st_mode):
            return "The selected path is no longer a directory"

        try:
            candidate = path.resolve(strict=True)
            current = Path(str(batch["current_path"])).resolve(strict=True)
            stored_root = Path(str(batch["allowed_root"])).resolve(strict=True)
            root_metadata = os.stat(stored_root)
        except (OSError, RuntimeError):
            return "The allowed deletion root is no longer available"
        if candidate.parent != current:
            return "The selected item is no longer in the reviewed folder"
        current_root = _find_allowed_root(candidate, roots)
        if current_root is None or current_root != stored_root:
            return "The item is no longer below an allowed deletion root"
        if int(root_metadata.st_dev) != int(batch["allowed_root_device"]):
            return "The allowed drive changed after deletion was scheduled"
        if not os.access(current, os.W_OK | os.X_OK):
            return "The containing folder is no longer writable"

        try:
            protected_roots = self.protection_repository.protected_roots()
        except Exception:
            return "Protected-folder status is unavailable"
        for root in protected_roots:
            if _paths_overlap(candidate, Path(root).expanduser().resolve(strict=False)):
                return "The item now overlaps a protected folder"
        for state_path in self.state_paths:
            if _paths_overlap(
                candidate, state_path.expanduser().resolve(strict=False)
            ):
                return "The item overlaps active coldshelf state"
        return None


class BrowseDeletionManager:
    """Preview, schedule, undo, recover, and execute Browse deletions."""

    def __init__(
        self,
        repository: WebRepository,
        browse_manager: BrowseJobManager,
        protection_repository: ProtectionRepository,
        io_queue: HeavyIOQueue,
        roots_provider: Callable[[], List[Dict[str, Any]]],
        policy: BrowseDeletionPolicy,
        *,
        delete_delay_seconds: float = DELETE_DELAY_SECONDS,
        preview_ttl_seconds: float = PREVIEW_TTL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.browse_manager = browse_manager
        self.protection_repository = protection_repository
        self.io_queue = io_queue
        self.roots_provider = roots_provider
        self.policy = policy
        self.delete_delay_seconds = max(0.0, float(delete_delay_seconds))
        self.preview_ttl_seconds = max(1.0, float(preview_ttl_seconds))
        self.clock = clock
        self._previews: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._preview_lock = RLock()
        self._condition = Condition(RLock())
        self._futures: Dict[str, Future] = {}
        self._thread: Optional[Thread] = None
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self.repository.requeue_interrupted_browse_deletions()
            self._stopping = False
            self._thread = Thread(
                target=self._scheduler_loop,
                name="coldshelf-browse-delete",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=2)

    def create_preview(self, scan_id: str, paths: Iterable[Path]) -> Dict[str, Any]:
        result = self.browse_manager.get_result(scan_id)
        current_path = Path(str(result["current_path"]))
        roots = list(result["roots"])
        entries = {str(item["path"]): item for item in result["entries"]}
        try:
            protected_roots = self.protection_repository.protected_roots()
        except Exception as exc:
            raise BrowseDeletionConflict(
                "Protected-folder status is unavailable"
            ) from exc

        items: List[Dict[str, Any]] = []
        blockers: List[Dict[str, str]] = []
        seen: set[str] = set()
        allowed_root: Optional[Path] = None
        for raw_path in paths:
            requested = str(Path(raw_path).expanduser())
            if requested in seen:
                blockers.append({"path": requested, "reason": "Duplicate selection"})
                continue
            seen.add(requested)
            entry = entries.get(requested)
            if entry is None:
                blockers.append(
                    {
                        "path": requested,
                        "reason": "The item is not part of this completed Browse scan",
                    }
                )
                continue
            eligibility = self.policy.evaluate_entry(
                entry,
                current_path,
                roots,
                protected_roots=protected_roots,
            )
            if not eligibility.eligible or eligibility.allowed_root is None:
                blockers.append(
                    {
                        "path": requested,
                        "reason": eligibility.blocker or "The item cannot be deleted",
                    }
                )
                continue
            if allowed_root is None:
                allowed_root = eligibility.allowed_root
            elif allowed_root != eligibility.allowed_root:
                blockers.append(
                    {
                        "path": requested,
                        "reason": "A deletion batch cannot span storage roots",
                    }
                )
                continue
            try:
                metadata = os.lstat(requested)
            except OSError as exc:
                blockers.append(
                    {"path": requested, "reason": f"The item cannot be inspected: {exc}"}
                )
                continue
            if entry["kind"] == "file" and not stat_module.S_ISREG(metadata.st_mode):
                blockers.append(
                    {"path": requested, "reason": "The item is no longer a regular file"}
                )
                continue
            if entry["kind"] == "directory" and not stat_module.S_ISDIR(metadata.st_mode):
                blockers.append(
                    {"path": requested, "reason": "The item is no longer a directory"}
                )
                continue
            items.append(
                {
                    "path": requested,
                    "name": str(entry["name"]),
                    "kind": str(entry["kind"]),
                    "logical_bytes": int(entry.get("logical_bytes") or 0),
                    "allocated_bytes": int(entry.get("allocated_bytes") or 0),
                    "device": int(metadata.st_dev),
                    "inode": int(metadata.st_ino),
                    "mode": int(metadata.st_mode),
                    "size": int(metadata.st_size),
                    "mtime_ns": int(metadata.st_mtime_ns),
                    "ctime_ns": int(metadata.st_ctime_ns),
                    "entry": copy.deepcopy(entry),
                }
            )

        preview_id = str(uuid.uuid4())
        now = _ensure_utc(self.clock())
        expires_at = now + timedelta(seconds=self.preview_ttl_seconds)
        root_device = 0
        if allowed_root is not None:
            try:
                root_device = int(os.stat(allowed_root).st_dev)
            except OSError as exc:
                blockers.append(
                    {
                        "path": str(allowed_root),
                        "reason": f"The allowed storage root is unavailable: {exc}",
                    }
                )
        can_schedule = (
            bool(items)
            and not blockers
            and len(items) == len(seen)
            and allowed_root is not None
        )
        preview = {
            "id": preview_id,
            "created_at": now,
            "expires_at": expires_at,
            "current_path": str(current_path),
            "scan_id": scan_id,
            "items": items,
            "blockers": blockers,
            "can_schedule": can_schedule,
            "allowed_root": str(allowed_root) if allowed_root else "",
            "allowed_root_device": root_device,
            "roots": copy.deepcopy(roots),
        }
        with self._preview_lock:
            self._discard_expired_previews_locked(now)
            self._previews[preview_id] = preview
            while len(self._previews) > MAX_PREVIEWS:
                self._previews.popitem(last=False)

        executor = ActionExecutor(dry_run=True)
        executor.log_action(
            "DELETE_PREVIEW",
            current_path,
            size=sum(int(item["logical_bytes"]) for item in items),
            success=can_schedule,
            error=None if can_schedule else "Deletion preview contains blocked items",
            details={
                "source": "browse",
                "preview_id": preview_id,
                "selected_count": len(items),
                "blocker_count": len(blockers),
            },
        )
        if can_schedule:
            for item in items:
                executor.delete_browse_entry(item, deletion_id=preview_id)
        return self._public_preview(preview)

    def schedule(self, preview_id: str) -> Dict[str, Any]:
        now = _ensure_utc(self.clock())
        with self._preview_lock:
            self._discard_expired_previews_locked(now)
            preview = self._previews.pop(preview_id, None)
            if preview is None:
                raise BrowseDeletionNotFound(
                    "Deletion preview was not found or has expired"
                )
            preview = copy.deepcopy(preview)
        if not preview["can_schedule"]:
            raise BrowseDeletionConflict("The preview contains blocked items")
        if self.repository.get_active_browse_deletion() is not None:
            raise BrowseDeletionConflict(
                "Another Browse deletion is already active"
            )

        roots = self.roots_provider()
        try:
            protected_roots = self.protection_repository.protected_roots()
        except Exception as exc:
            raise BrowseDeletionConflict(
                "Protected-folder status is unavailable"
            ) from exc
        for item in preview["items"]:
            eligibility = self.policy.evaluate_entry(
                item["entry"],
                Path(preview["current_path"]),
                roots,
                protected_roots=protected_roots,
            )
            if (
                not eligibility.eligible
                or eligibility.allowed_root is None
                or str(eligibility.allowed_root) != preview["allowed_root"]
            ):
                raise BrowseDeletionConflict(
                    f"Selection changed; review again: {item['name']}"
                )
            try:
                metadata = os.lstat(item["path"])
            except OSError as exc:
                raise BrowseDeletionConflict(
                    f"Selection changed; review again: {item['name']}"
                ) from exc
            if not _fingerprint_matches(metadata, item):
                raise BrowseDeletionConflict(
                    f"Selection changed; review again: {item['name']}"
                )
        try:
            current_root_device = int(os.stat(preview["allowed_root"]).st_dev)
        except OSError as exc:
            raise BrowseDeletionConflict(
                "The allowed storage root is no longer available"
            ) from exc
        if current_root_device != int(preview["allowed_root_device"]):
            raise BrowseDeletionConflict(
                "The allowed drive changed; create a new preview"
            )

        deletion_id = str(uuid.uuid4())
        delete_after = now + timedelta(seconds=self.delete_delay_seconds)
        persisted_items = [
            {key: value for key, value in item.items() if key != "entry"}
            for item in preview["items"]
        ]
        try:
            batch = self.repository.create_browse_deletion(
                deletion_id=deletion_id,
                current_path=preview["current_path"],
                allowed_root=preview["allowed_root"],
                allowed_root_device=preview["allowed_root_device"],
                items=persisted_items,
                created_at=_iso(now),
                delete_after=_iso(delete_after),
            )
        except ActiveBrowseDeletionError as exc:
            raise BrowseDeletionConflict(str(exc)) from exc
        ActionExecutor(dry_run=False).log_action(
            "DELETE_SCHEDULED",
            Path(preview["current_path"]),
            size=sum(int(item["logical_bytes"]) for item in persisted_items),
            details={
                "source": "browse",
                "deletion_id": deletion_id,
                "selected_count": len(persisted_items),
                "delete_after": _iso(delete_after),
            },
        )
        self._wake_scheduler()
        return self._public_batch(batch)

    def get(self, deletion_id: str) -> Dict[str, Any]:
        try:
            batch = self.repository.get_browse_deletion(deletion_id)
        except KeyError as exc:
            raise BrowseDeletionNotFound("Browse deletion not found") from exc
        return self._public_batch(batch)

    def active(self) -> Optional[Dict[str, Any]]:
        batch = self.repository.get_active_browse_deletion()
        return self._public_batch(batch) if batch else None

    def undo(self, deletion_id: str) -> Dict[str, Any]:
        now = _ensure_utc(self.clock())
        try:
            current = self.repository.get_browse_deletion(deletion_id)
        except KeyError as exc:
            raise BrowseDeletionNotFound("Browse deletion not found") from exc
        if current["status"] == "cancelled":
            return self._public_batch(current)
        cancelled = self.repository.cancel_browse_deletion(
            deletion_id, _iso(now), _iso(now)
        )
        if not cancelled:
            raise BrowseDeletionConflict(
                "The 10-second Undo window has closed"
            )
        batch = self.repository.get_browse_deletion(deletion_id)
        ActionExecutor(dry_run=False).log_action(
            "DELETE_CANCELLED",
            Path(batch["current_path"]),
            size=int(batch["logical_bytes"]),
            details={
                "source": "browse",
                "deletion_id": deletion_id,
                "selected_count": int(batch["selected_count"]),
            },
        )
        self._wake_scheduler()
        return self._public_batch(batch)

    def _scheduler_loop(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            active = self.repository.get_active_browse_deletion()
            if active is None:
                self._wait_for_scheduler(None)
                continue
            if active["status"] == "scheduled":
                deadline = _parse_iso(active["delete_after"])
                now = _ensure_utc(self.clock())
                remaining = (deadline - now).total_seconds()
                if remaining > 0:
                    self._wait_for_scheduler(min(remaining, 1.0))
                    continue
                if self.repository.claim_due_browse_deletion(
                    active["id"], _iso(now)
                ):
                    self._submit(active["id"])
                continue
            if active["status"] == "queued":
                self._submit(active["id"])
            self._wait_for_scheduler(0.25)

    def _submit(self, deletion_id: str) -> None:
        with self._condition:
            future = self._futures.get(deletion_id)
            if future is not None and not future.done():
                return
            future = self.io_queue.submit(self._run, deletion_id)
            self._futures[deletion_id] = future
            future.add_done_callback(
                lambda _: self._forget_future(deletion_id)
            )

    def _run(self, deletion_id: str) -> None:
        try:
            before = self.repository.get_browse_deletion(deletion_id)
        except KeyError:
            return
        resuming = str(before.get("error") or "").startswith("Resuming")
        if not self.repository.mark_browse_deletion_running(
            deletion_id, _iso(_ensure_utc(self.clock()))
        ):
            return
        executor = ActionExecutor(dry_run=False)
        try:
            batch = self.repository.get_browse_deletion(deletion_id)
            roots = self.roots_provider()
            for item in batch["items"]:
                if item["status"] != "pending":
                    continue
                reason = self.policy.validate_persisted_item(batch, item, roots)
                if reason:
                    if resuming and reason == "The item no longer exists":
                        executor.log_action(
                            "DELETE",
                            Path(item["path"]),
                            size=int(item["logical_bytes"]),
                            success=True,
                            details={
                                "source": "browse",
                                "deletion_id": deletion_id,
                                "kind": item["kind"],
                                "reconciled_after_restart": True,
                            },
                        )
                        self.repository.update_browse_deletion_item(
                            deletion_id, item["position"], "deleted", reason
                        )
                    else:
                        executor.log_action(
                            "DELETE",
                            Path(item["path"]),
                            size=int(item["logical_bytes"]),
                            success=False,
                            error=reason,
                            details={
                                "source": "browse",
                                "deletion_id": deletion_id,
                                "kind": item["kind"],
                                "outcome": "skipped",
                            },
                        )
                        self.repository.update_browse_deletion_item(
                            deletion_id, item["position"], "skipped", reason
                        )
                    continue
                outcome = executor.delete_browse_entry(
                    item, deletion_id=deletion_id
                )
                self.repository.update_browse_deletion_item(
                    deletion_id,
                    item["position"],
                    str(outcome["status"]),
                    outcome.get("error"),
                )
            self.repository.complete_browse_deletion(
                deletion_id, _iso(_ensure_utc(self.clock()))
            )
        except Exception as exc:
            logger.exception("Browse deletion %s failed", deletion_id)
            ActionExecutor(dry_run=False).log_action(
                "DELETE_BATCH_FAILED",
                Path(str(before["current_path"])),
                size=int(before["logical_bytes"]),
                success=False,
                error=str(exc) or exc.__class__.__name__,
                details={
                    "source": "browse",
                    "deletion_id": deletion_id,
                    "selected_count": int(before["selected_count"]),
                },
            )
            self.repository.fail_browse_deletion(
                deletion_id,
                _iso(_ensure_utc(self.clock())),
                str(exc) or exc.__class__.__name__,
            )
        finally:
            self._wake_scheduler()

    def _forget_future(self, deletion_id: str) -> None:
        with self._condition:
            self._futures.pop(deletion_id, None)
            self._condition.notify_all()

    def _wake_scheduler(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _wait_for_scheduler(self, timeout: Optional[float]) -> None:
        with self._condition:
            if not self._stopping:
                self._condition.wait(timeout=timeout)

    def _discard_expired_previews_locked(self, now: datetime) -> None:
        expired = [
            preview_id
            for preview_id, preview in self._previews.items()
            if preview["expires_at"] <= now
        ]
        for preview_id in expired:
            self._previews.pop(preview_id, None)

    def _public_preview(self, preview: Dict[str, Any]) -> Dict[str, Any]:
        items = [_public_item(item) for item in preview["items"]]
        return {
            "id": preview["id"],
            "expires_at": _iso(preview["expires_at"]),
            "current_path": preview["current_path"],
            "selected_count": len(items),
            "logical_bytes": sum(item["logical_bytes"] for item in items),
            "allocated_bytes": sum(item["allocated_bytes"] for item in items),
            "can_schedule": bool(preview["can_schedule"]),
            "items": items,
            "blockers": copy.deepcopy(preview["blockers"]),
        }

    def _public_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        now = _ensure_utc(self.clock())
        return {
            "id": batch["id"],
            "status": batch["status"],
            "current_path": batch["current_path"],
            "selected_count": int(batch["selected_count"]),
            "logical_bytes": int(batch["logical_bytes"]),
            "allocated_bytes": int(batch["allocated_bytes"]),
            "created_at": batch["created_at"],
            "delete_after": batch["delete_after"],
            "server_time": _iso(now),
            "started_at": batch.get("started_at"),
            "finished_at": batch.get("finished_at"),
            "error": batch.get("error"),
            "undo_available": (
                batch["status"] == "scheduled"
                and _parse_iso(batch["delete_after"]) > now
            ),
            "items": [_public_item(item) for item in batch["items"]],
        }


def _public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": str(item["path"]),
        "name": str(item["name"]),
        "kind": str(item["kind"]),
        "logical_bytes": int(item.get("logical_bytes") or 0),
        "allocated_bytes": int(item.get("allocated_bytes") or 0),
        "status": str(item.get("status") or "pending"),
        "error": item.get("error"),
    }


def _find_allowed_root(
    candidate: Path, roots: List[Dict[str, Any]]
) -> Optional[Path]:
    matches: List[Path] = []
    for root in roots:
        if root.get("kind") not in {"home", "drive"} or not root.get("readable", True):
            continue
        try:
            canonical = Path(str(root["path"])).expanduser().resolve(strict=True)
            candidate.relative_to(canonical)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == canonical:
            continue
        if not os.access(canonical, os.W_OK | os.X_OK):
            continue
        matches.append(canonical)
    if not matches:
        return None
    return max(matches, key=lambda path: len(path.parts))


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _fingerprint_matches(metadata: os.stat_result, item: Dict[str, Any]) -> bool:
    return (
        int(metadata.st_dev) == int(item["device"])
        and int(metadata.st_ino) == int(item["inode"])
        and int(metadata.st_mode) == int(item["mode"])
        and int(metadata.st_size) == int(item["size"])
        and int(metadata.st_mtime_ns) == int(item["mtime_ns"])
        and int(metadata.st_ctime_ns) == int(item["ctime_ns"])
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        _ensure_utc(value)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
