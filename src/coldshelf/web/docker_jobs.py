"""Server-owned Docker previews and persistent, delayed cleanup jobs."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, RLock, Thread
from typing import Any, Dict, List, Optional

from coldshelf.docker_storage import DockerConflict, DockerStorage
from coldshelf.executor import ActionExecutor

from .io_queue import HeavyIOQueue
from .repository import WebRepository


logger = logging.getLogger(__name__)


class DockerActionManager:
    def __init__(self, repository: WebRepository, storage: DockerStorage,
                 io_queue: HeavyIOQueue, delay_seconds: float = 10,
                 preview_seconds: float = 300) -> None:
        self.repository = repository
        self.storage = storage
        self.io_queue = io_queue
        self.delay_seconds = delay_seconds
        self.preview_seconds = preview_seconds
        self._previews: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = RLock()
        self._condition = Condition(RLock())
        self._thread: Optional[Thread] = None
        self._stopping = False
        self._submitted: set[str] = set()

    def start(self) -> None:
        self.repository.interrupt_docker_actions(_now())
        with self._condition:
            self._stopping = False
            self._thread = Thread(target=self._scheduler, name="coldshelf-docker-clean", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=2)

    def create_preview(self, kind: str, ids: List[str], older_than_days: int = 30,
                       all_eligible: bool = False) -> Dict[str, Any]:
        selection = self.storage.preview(kind, ids, older_than_days, all_eligible)
        now = datetime.now(timezone.utc)
        preview_id = str(uuid.uuid4())
        record = {
            **selection,
            "id": preview_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.preview_seconds)).isoformat(),
            "confirmation_phrase": _phrase(kind, len(selection["items"])),
        }
        with self._lock:
            self._prune_previews()
            self._previews[preview_id] = record
            while len(self._previews) > 128:
                self._previews.popitem(last=False)
        ActionExecutor(dry_run=True).log_action(
            "DOCKER_PREVIEW", Path("docker"), details={
                "preview_id": preview_id, "kind": kind,
                "selected_count": len(selection["items"]),
                "estimated_bytes": selection["estimated_bytes"],
                "ids": selection["ids"], "cutoff": selection["cutoff"],
                "cache_summary": selection.get("cache_summary"),
            },
        )
        return self._public_preview(record)

    def schedule(self, preview_id: str, phrase: str) -> Dict[str, Any]:
        with self._lock:
            self._prune_previews()
            preview = self._previews.get(preview_id)
            if preview is None:
                raise DockerConflict("Docker preview expired or was already used.")
            if phrase != preview["confirmation_phrase"]:
                raise DockerConflict("Type the displayed confirmation phrase exactly.")
            if not preview["items"]:
                raise DockerConflict("No removable build cache was reviewed.")
            if self.repository.get_active_docker_action():
                raise DockerConflict("Another Docker cleanup is already active.")
            self._ensure_log_writable()
            action_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            try:
                action = self.repository.create_docker_action(
                    action_id, preview, now.isoformat(),
                    (now + timedelta(seconds=self.delay_seconds)).isoformat(),
                )
            except sqlite3.IntegrityError as exc:
                raise DockerConflict("Another Docker cleanup is already active.") from exc
            self._previews.pop(preview_id)
        ActionExecutor().log_action("DOCKER_SCHEDULED", Path("docker"), details={
            "action_id": action_id, "kind": preview["kind"],
            "selected_count": len(preview["items"]),
            "ids": preview["ids"], "cutoff": preview["cutoff"],
        })
        with self._condition:
            self._condition.notify_all()
        return self._public_action(action)

    def get(self, action_id: str) -> Dict[str, Any]:
        try:
            return self._public_action(self.repository.get_docker_action(action_id))
        except KeyError as exc:
            raise DockerConflict("Docker action not found.") from exc

    def active(self) -> Optional[Dict[str, Any]]:
        action = self.repository.get_active_docker_action()
        return self._public_action(action) if action else None

    def recent(self) -> List[Dict[str, Any]]:
        return [self._public_action(action) for action in self.repository.list_recent_docker_actions()]

    def undo(self, action_id: str) -> Dict[str, Any]:
        try:
            action = self.repository.get_docker_action(action_id)
        except KeyError as exc:
            raise DockerConflict("Docker action not found.") from exc
        if action["status"] != "scheduled" or datetime.now(timezone.utc) >= datetime.fromisoformat(action["delete_after"]):
            raise DockerConflict("The cancellation window has closed.")
        if not self.repository.transition_docker_action(action_id, "scheduled", "cancelled", finished_at=_now()):
            raise DockerConflict("The cancellation window has closed.")
        ActionExecutor().log_action("DOCKER_CANCELLED", Path("docker"), details={"action_id": action_id})
        with self._condition:
            self._condition.notify_all()
        return self.get(action_id)

    def _scheduler(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            action = self.repository.get_active_docker_action()
            if action and action["status"] == "scheduled":
                remaining = (datetime.fromisoformat(action["delete_after"]) - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    self.repository.transition_docker_action(action["id"], "scheduled", "queued")
                    continue
                delay = min(remaining, 1.0)
            elif action and action["status"] == "queued":
                if action["id"] not in self._submitted:
                    self._submitted.add(action["id"])
                    self.io_queue.submit(self._run, action["id"])
                delay = 0.25
            else:
                delay = 1.0
            with self._condition:
                self._condition.wait(timeout=delay)

    def _run(self, action_id: str) -> None:
        if not self.repository.transition_docker_action(action_id, "queued", "running", started_at=_now()):
            return
        action = self.repository.get_docker_action(action_id)
        recorded: List[Dict[str, Any]] = []
        def record(outcome: Dict[str, Any]) -> None:
            recorded.append(outcome)
            self.repository.transition_docker_action(
                action_id, "running", "running", outcomes_json=json.dumps(recorded)
            )
            ActionExecutor().log_action("DOCKER_ITEM", Path("docker"),
                                        success=outcome["status"] == "deleted",
                                        error=outcome.get("error"),
                                        details={"action_id": action_id, "kind": action["kind"],
                                                 "outcome": outcome})
        try:
            outcomes = self.storage.execute(action["preview"], on_outcome=record)
            ActionExecutor().log_action("DOCKER_CLEAN", Path("docker"), details={
                "action_id": action_id, "kind": action["kind"], "outcomes": outcomes,
            })
            self.repository.transition_docker_action(
                action_id, "running", "completed", finished_at=_now(),
                outcomes_json=json.dumps(outcomes),
            )
        except Exception as exc:
            logger.exception("Docker cleanup failed")
            ActionExecutor().log_action("DOCKER_CLEAN", Path("docker"), success=False,
                                        error=str(exc), details={"action_id": action_id})
            self.repository.transition_docker_action(
                action_id, "running", "failed", finished_at=_now(), error=str(exc),
            )
        finally:
            self._submitted.discard(action_id)
            with self._condition:
                self._condition.notify_all()

    def _prune_previews(self) -> None:
        now = datetime.now(timezone.utc)
        for key in list(self._previews):
            if datetime.fromisoformat(self._previews[key]["expires_at"]) <= now:
                self._previews.pop(key)

    @staticmethod
    def _ensure_log_writable() -> None:
        path = ActionExecutor().log_file
        try:
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise DockerConflict("The action log is unavailable; Docker cleanup was not scheduled.") from exc

    @staticmethod
    def _public_preview(record: Dict[str, Any]) -> Dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in record.items() if key != "daemon_id"}

    @staticmethod
    def _public_action(action: Dict[str, Any]) -> Dict[str, Any]:
        preview = action["preview"]
        return {
            "id": action["id"], "status": action["status"], "kind": action["kind"],
            "items": preview["items"], "estimated_bytes": preview["estimated_bytes"],
            "cache_summary": preview.get("cache_summary"),
            "outcomes": action["outcomes"], "created_at": action["created_at"],
            "delete_after": action["delete_after"], "started_at": action["started_at"],
            "finished_at": action["finished_at"], "error": action["error"],
            "server_time": _now(), "undo_available": action["status"] == "scheduled",
        }


def _phrase(kind: str, count: int) -> str:
    return f"DELETE {count} DOCKER {kind.upper()}" if kind == "volumes" else f"REMOVE {count} DOCKER {kind.upper()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
