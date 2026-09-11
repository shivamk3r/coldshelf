"""Queued protected-folder operations for the authenticated web API."""

import logging
from concurrent.futures import Future
from threading import RLock
from typing import Any, Dict, Optional

from coldshelf.protection.repository import (
    ProtectionRepository,
    operation_failure_diagnostics,
)
from coldshelf.protection.service import ProtectionError, ProtectionService

from .io_queue import HeavyIOQueue
from .schemas import ProtectionOperationRequest


logger = logging.getLogger(__name__)


class OperationCancelled(RuntimeError):
    """Raised by cooperative non-Restic operation cancellation."""


class ProtectionOperationManager:
    """Persist, serialize, stream, and cancel protection operations."""

    def __init__(
        self,
        repository: ProtectionRepository,
        service: ProtectionService,
        io_queue: HeavyIOQueue,
    ) -> None:
        self.repository = repository
        self.service = service
        self.io_queue = io_queue
        self._futures: Dict[str, Future] = {}
        self._lock = RLock()
        self.repository.fail_interrupted_operations()
        self.service.reconcile_interrupted_file_operations()

    def submit(self, folder_id: str, request: ProtectionOperationRequest) -> Dict[str, Any]:
        payload = request.model_dump(mode="json")
        drive_id = payload.get("drive_target_id")
        operation = self.repository.create_operation(
            folder_id, request.kind, payload, drive_id=drive_id
        )
        future = self.io_queue.submit(self._run, operation["id"], folder_id, request)
        with self._lock:
            self._futures[operation["id"]] = future
        future.add_done_callback(lambda _: self._forget_future(operation["id"]))
        return operation

    def cancel(self, operation_id: str) -> Dict[str, Any]:
        operation = self.repository.get_operation(operation_id)
        if operation["status"] not in {"queued", "running"}:
            return operation
        self.repository.request_cancel(operation_id)
        self.service.restic.cancel(operation_id)
        with self._lock:
            future = self._futures.get(operation_id)
        if future and future.cancel():
            self.repository.fail_operation(
                operation_id,
                "Cancelled before the operation started",
                cancelled=True,
                diagnostics={"exception_type": "CancelledBeforeStart"},
            )
        return self.repository.get_operation(operation_id)

    def _run(
        self,
        operation_id: str,
        folder_id: str,
        request: ProtectionOperationRequest,
    ) -> None:
        operation = self.repository.get_operation(operation_id)
        if operation["cancel_requested"]:
            self.repository.fail_operation(operation_id, "Cancelled", cancelled=True)
            return
        self.repository.start_operation(operation_id)

        def progress(
            ratio: float,
            message: str,
            eta_seconds: Optional[int] = None,
        ) -> None:
            current = self.repository.get_operation(operation_id)
            if current["cancel_requested"]:
                self.service.restic.cancel(operation_id)
                raise OperationCancelled("Operation cancelled")
            self.repository.update_operation_progress(
                operation_id,
                ratio * 100,
                message,
                eta_seconds,
            )

        try:
            if request.kind == "index":
                result = self.service.index_folder(folder_id, progress)
            elif request.kind == "backup":
                result = self.service.backup(
                    folder_id,
                    getattr(request, "drive_target_id", None) or "",
                    operation_id=operation_id,
                    progress=progress,
                )
            elif request.kind == "verify":
                result = self.service.verify(
                    folder_id,
                    getattr(request, "drive_target_id", None) or "",
                    full=getattr(request, "full", False),
                    file_ids=getattr(request, "file_ids", []),
                    operation_id=operation_id,
                    progress=progress,
                )
            elif request.kind == "evict":
                result = self.service.evict(
                    folder_id,
                    file_ids=getattr(request, "file_ids", []),
                    all_files=getattr(request, "all_files", False),
                    dry_run=getattr(request, "dry_run", True),
                    confirmation_phrase=getattr(request, "confirmation_phrase", None),
                    operation_id=operation_id,
                    progress=progress,
                )
            elif request.kind == "restore":
                result = self.service.restore(
                    folder_id,
                    file_ids=getattr(request, "file_ids", []),
                    drive_id=getattr(request, "drive_target_id", None),
                    destination=getattr(request, "destination", None),
                    operation_id=operation_id,
                    progress=progress,
                )
            else:  # pragma: no cover - Pydantic rejects unknown kinds
                raise ProtectionError(f"Unsupported operation: {request.kind}")
            self.repository.finish_operation(operation_id, result)
        except OperationCancelled as exc:
            self.repository.fail_operation(
                operation_id,
                str(exc),
                cancelled=True,
                diagnostics=operation_failure_diagnostics(exc),
            )
        except Exception as exc:
            if isinstance(exc, ProtectionError):
                logger.warning(
                    "Protection operation %s (%s) failed: %s",
                    operation_id,
                    request.kind,
                    exc,
                )
            else:
                logger.exception(
                    "Protection operation %s (%s) failed unexpectedly",
                    operation_id,
                    request.kind,
                )
            cancelled = self.repository.get_operation(operation_id)["cancel_requested"]
            self.repository.fail_operation(
                operation_id,
                str(exc),
                cancelled=cancelled,
                diagnostics=operation_failure_diagnostics(exc),
            )

    def _forget_future(self, operation_id: str) -> None:
        with self._lock:
            self._futures.pop(operation_id, None)
