"""Local, read-only Docker accounting and narrowly scoped removals.

The adapter deliberately accepts Unix sockets only. A dashboard connected to a
remote Docker context must never clean a different computer by accident.
"""

from __future__ import annotations

import copy
import http.client
import json
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlencode


KINDS = ("images", "containers", "volumes", "cache")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_CACHE_ID = re.compile(r"[A-Za-z0-9:_.-]+\Z")


class DockerUnavailable(RuntimeError):
    """The local daemon cannot be reached or queried."""


class DockerConflict(RuntimeError):
    """A reviewed Docker object changed or is ineligible."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 45) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _socket_path() -> str:
    host = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    if not host:
        docker = shutil.which("docker")
        if docker:
            try:
                command = [docker, "context", "inspect"]
                if os.environ.get("DOCKER_CONTEXT"):
                    command.append(os.environ["DOCKER_CONTEXT"])
                command += ["--format", "{{.Endpoints.docker.Host}}"]
                result = subprocess.run(
                    command,
                    check=True, capture_output=True, text=True, timeout=3,
                )
                host = result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    if not host and os.environ.get("DOCKER_CONTEXT"):
        raise DockerUnavailable("The selected Docker context could not be inspected.")
    if not host:
        desktop_socket = Path.home() / ".docker/run/docker.sock"
        host = f"unix://{desktop_socket}" if desktop_socket.exists() else "unix:///var/run/docker.sock"
    if not host.startswith("unix://"):
        raise DockerUnavailable("The selected Docker context is remote; only a local daemon is supported.")
    path = host[len("unix://"):]
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            raise DockerUnavailable("The local Docker socket is unavailable.")
    except OSError as exc:
        raise DockerUnavailable("The local Docker socket is unavailable.") from exc
    return path


def _positive(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _items(payload: Dict[str, Any], modern: str, legacy: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    group = payload.get(modern)
    if isinstance(group, dict):
        return group, list(group.get("Items") or [])
    return {}, list(payload.get(legacy) or [])


def _age_seconds(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cache_id_filter(ids: List[str]) -> str:
    """Match only reviewed IDs with the one regex value accepted by BuildKit."""
    if not ids or any(not _CACHE_ID.fullmatch(item_id) for item_id in ids):
        raise DockerConflict("Build cache has an unsupported ID. Review again.")
    return "^(?:" + "|".join(item_id.replace(".", r"\.") for item_id in ids) + ")$"


class DockerStorage:
    """Normalize Engine disk usage and allow only reviewed object removals."""

    def __init__(self, socket_path: Optional[str] = None, cache_seconds: float = 60) -> None:
        self._explicit_socket = socket_path
        self._cache_seconds = cache_seconds
        self._lock = RLock()
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at = 0.0
        self._version: Optional[str] = None

    def _request(self, method: str, path: str) -> Any:
        timeout = 600 if method == "POST" and "/build/prune?" in path else 45
        conn = _UnixConnection(self._explicit_socket or _socket_path(), timeout=timeout)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise DockerUnavailable("Docker returned too much data.")
            if response.status >= 400:
                try:
                    message = str(json.loads(data).get("message") or "Docker request failed")
                except (ValueError, AttributeError):
                    message = "Docker request failed"
                if response.status in (400, 404, 409):
                    raise DockerConflict(message)
                raise DockerUnavailable(message)
            return json.loads(data) if data else {}
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise DockerUnavailable("The local Docker daemon is unavailable.") from exc
        finally:
            conn.close()

    def _api_version(self) -> str:
        if self._version:
            return self._version
        payload = self._request("GET", "/version")
        version = str(payload.get("ApiVersion") or "")
        try:
            parts = tuple(int(part) for part in version.split("."))
        except ValueError as exc:
            raise DockerUnavailable("Docker returned an invalid API version.") from exc
        if parts < (1, 44):
            raise DockerUnavailable("Docker Engine API 1.44 or newer is required.")
        self._version = version
        return version

    def _api(self, method: str, path: str) -> Any:
        return self._request(method, "/v" + self._api_version() + path)

    def snapshot(self, refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            if not refresh and self._cached and time.monotonic() - self._cached_at < self._cache_seconds:
                return copy.deepcopy(self._cached)
            info = self._api("GET", "/info")
            payload = self._api("GET", "/system/df?verbose=1")
            image_group, images_raw = _items(payload, "ImageUsage", "Images")
            container_group, containers_raw = _items(payload, "ContainerUsage", "Containers")
            volume_group, volumes_raw = _items(payload, "VolumeUsage", "Volumes")
            cache_group, cache_raw = _items(payload, "BuildCacheUsage", "BuildCache")

            images = []
            for item in images_raw:
                size = _positive(item.get("Size"))
                shared = _positive(item.get("SharedSize"))
                references = item.get("Containers")
                known_unreferenced = isinstance(references, int) and references == 0
                item_id = str(item.get("Id") or "")
                images.append({
                    "id": item_id,
                    "name": ", ".join(item.get("RepoTags") or []) or "Untagged image",
                    "tags": list(item.get("RepoTags") or []),
                    "size_bytes": size,
                    "shared_bytes": shared,
                    "unique_bytes": max(0, size - shared),
                    "references": references if isinstance(references, int) and references >= 0 else None,
                    "eligible": bool(item_id) and known_unreferenced,
                })
            containers = []
            for item in containers_raw:
                state = str(item.get("State") or "unknown")
                item_id = str(item.get("Id") or "")
                containers.append({
                    "id": item_id,
                    "name": ", ".join(item.get("Names") or []) or str(item.get("Id") or "")[:12],
                    "image": str(item.get("Image") or ""),
                    "state": state,
                    "size_bytes": _positive(item.get("SizeRw")),
                    "eligible": bool(item_id) and state in {"exited", "created", "dead"},
                })
            volumes = []
            for item in volumes_raw:
                usage = item.get("UsageData") or {}
                refs = usage.get("RefCount")
                known_unlinked = isinstance(refs, int) and refs == 0
                driver = str(item.get("Driver") or "")
                item_id = str(item.get("Name") or "")
                volumes.append({
                    "id": item_id,
                    "name": str(item.get("Name") or ""),
                    "driver": driver,
                    "references": refs if isinstance(refs, int) and refs >= 0 else None,
                    "size_bytes": _positive(usage.get("Size")),
                    "eligible": bool(item_id) and known_unlinked and driver == "local",
                })
            cache = []
            for item in cache_raw:
                cache.append({
                    "id": str(item.get("ID") or item.get("Id") or ""),
                    "size_bytes": _positive(item.get("Size")),
                    "in_use": bool(item.get("InUse")),
                    "last_used": item.get("LastUsedAt") or item.get("CreatedAt"),
                })

            groups = {
                "images": self._group(image_group, images, "images", payload.get("LayersSize")),
                "containers": self._group(container_group, containers, "containers"),
                "volumes": self._group(volume_group, volumes, "volumes"),
                "cache": self._group(cache_group, cache, "cache"),
            }
            root = str(info.get("DockerRootDir") or "")
            same_filesystem = False
            if platform.system() == "Linux" and root:
                try:
                    same_filesystem = os.stat(root).st_dev == os.stat(Path.home()).st_dev
                except OSError:
                    pass
            result = {
                "available": True,
                "message": None,
                "daemon_id": str(info.get("ID") or ""),
                "root_path": root,
                "same_filesystem": same_filesystem,
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "groups": groups,
                "items": {"images": images, "containers": containers, "volumes": volumes},
                "cache_items": cache,
            }
            self._cached = result
            self._cached_at = time.monotonic()
            return copy.deepcopy(result)

    @staticmethod
    def _group(group: Dict[str, Any], items: List[Dict[str, Any]], kind: str, legacy_total: Any = None) -> Dict[str, int]:
        if kind == "images":
            fallback_reclaim = sum(item["unique_bytes"] for item in items if item["eligible"])
        elif kind == "cache":
            fallback_reclaim = sum(item["size_bytes"] for item in items if not item["in_use"])
        else:
            fallback_reclaim = sum(item["size_bytes"] for item in items if item["eligible"])
        fallback_active = sum(item["in_use"] for item in items) if kind == "cache" else sum(
            not item.get("eligible", False) for item in items
        )
        return {
            "count": _positive(group.get("TotalCount")) if group else len(items),
            "active_count": _positive(group.get("ActiveCount")) if group else fallback_active,
            "size_bytes": _positive(group.get("TotalSize")) if group else _positive(legacy_total) or sum(item["size_bytes"] for item in items),
            "reclaimable_bytes": _positive(group.get("Reclaimable")) if "Reclaimable" in group else fallback_reclaim,
        }

    def public_usage(self, refresh: bool = False) -> Dict[str, Any]:
        snap = self.snapshot(refresh)
        return {key: value for key, value in snap.items() if key not in {"items", "cache_items"}}

    def list_items(self, kind: str, page: int = 1, limit: int = 50,
                   search: str = "") -> Dict[str, Any]:
        if kind not in {"images", "containers", "volumes"}:
            raise DockerConflict("Unknown Docker item type.")
        items = sorted(self.snapshot()["items"][kind], key=lambda item: item["size_bytes"], reverse=True)
        if search:
            term = search.casefold()
            items = [item for item in items if term in item["id"].casefold() or term in item["name"].casefold()]
        start = (page - 1) * limit
        return {"kind": kind, "total": len(items), "page": page, "limit": limit, "items": items[start:start + limit]}

    def preview(self, kind: str, ids: List[str], older_than_days: int = 30,
                all_eligible: bool = False) -> Dict[str, Any]:
        if kind not in KINDS:
            raise DockerConflict("Unknown Docker cleanup type.")
        snap = self.snapshot(refresh=True)
        if not snap["daemon_id"]:
            raise DockerUnavailable("Docker daemon identity is unavailable; cleanup cannot be previewed safely.")
        if kind == "cache":
            if ids or all_eligible:
                raise DockerConflict("Build cache uses an age cutoff, not item IDs.")
            if older_than_days not in {7, 30, 90}:
                raise DockerConflict("Choose 7, 30, or 90 days for build cache.")
            cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
            selected = [item for item in snap["cache_items"] if not item["in_use"] and
                        (age := _age_seconds(item["last_used"])) is not None and age < cutoff]
            if not selected:
                raise DockerConflict("No unused build cache matches this age.")
            if len(selected) > 1000 or any(not item["id"] for item in selected) or len({item["id"] for item in selected}) != len(selected):
                raise DockerConflict("Build cache cannot be safely reviewed as one selection. Choose an older age cutoff.")
            _cache_id_filter([item["id"] for item in selected])
            return {
                "kind": kind, "daemon_id": snap["daemon_id"], "items": selected,
                "ids": sorted(item["id"] for item in selected),
                "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
                "estimated_bytes": sum(item["size_bytes"] for item in selected),
            }
        if all_eligible:
            if kind == "volumes" or ids:
                raise DockerConflict("Volumes require exact names; choose either IDs or all eligible.")
            ids = [item["id"] for item in snap["items"][kind] if item["eligible"]]
        if not ids or len(ids) > 1000 or len(ids) != len(set(ids)):
            raise DockerConflict("Select between 1 and 1000 distinct Docker items.")
        available = {item["id"]: item for item in snap["items"][kind]}
        selected = []
        for item_id in ids:
            item = available.get(item_id)
            if item is None or not item["eligible"]:
                raise DockerConflict("A selected Docker item changed or is not eligible. Refresh and review again.")
            selected.append(item)
        return {
            "kind": kind, "daemon_id": snap["daemon_id"], "items": selected,
            "ids": list(ids), "cutoff": None,
            "estimated_bytes": sum(item["unique_bytes"] if kind == "images" else item["size_bytes"] for item in selected),
        }

    def execute(self, preview: Dict[str, Any],
                on_outcome: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
        """Revalidate immediately, then make only the reviewed Engine calls."""
        kind = preview["kind"]
        current = self.snapshot(refresh=True)
        if not current["daemon_id"] or current["daemon_id"] != preview["daemon_id"]:
            raise DockerConflict("The Docker daemon changed after preview.")
        if kind == "cache":
            cutoff = _age_seconds(preview["cutoff"])
            selected = sorted(item["id"] for item in current["cache_items"] if not item["in_use"] and
                              (age := _age_seconds(item["last_used"])) is not None and cutoff is not None and age < cutoff)
            if selected != sorted(preview["ids"]):
                raise DockerConflict("Build cache changed after preview. Review again.")
            # Engine's build-prune "until" accepts a duration since last use.
            # Limit it to the exact reviewed IDs as well, so a new cache record
            # cannot enter the prune set between the second scan and the call.
            age_seconds = math.ceil(datetime.now(timezone.utc).timestamp() - cutoff)
            filters = json.dumps({"id": [_cache_id_filter(preview["ids"])],
                                  "until": [f"{max(1, age_seconds)}s"]})
            result = self._api("POST", "/build/prune?" + urlencode({"all": "1", "filters": filters}))
            self.invalidate()
            reclaimed = _positive(result.get("SpaceReclaimed"))
            removed = bool(result.get("CachesDeleted")) or reclaimed > 0
            outcome = {"id": "build-cache", "status": "deleted" if removed else "skipped",
                       "reclaimed_bytes": reclaimed}
            if not removed:
                outcome["error"] = "Docker found no removable cache among the reviewed items."
            if on_outcome:
                on_outcome(outcome)
            return [outcome]
        available = {item["id"]: item for item in current["items"][kind]}
        outcomes = []
        for reviewed in preview["items"]:
            item = available.get(reviewed["id"])
            if item is None or not item["eligible"] or item != reviewed:
                outcome = {"id": reviewed["id"], "status": "skipped", "error": "Item changed after preview"}
                outcomes.append(outcome)
                if on_outcome:
                    on_outcome(outcome)
                continue
            endpoint = {
                "images": "/images/" + quote(item["id"], safe=":") + "?force=false&noprune=true",
                "containers": "/containers/" + quote(item["id"], safe="") + "?force=false&v=false",
                "volumes": "/volumes/" + quote(item["id"], safe="") + "?force=false",
            }[kind]
            try:
                self._api("DELETE", endpoint)
                outcome = {"id": item["id"], "status": "deleted"}
            except (DockerConflict, DockerUnavailable) as exc:
                outcome = {"id": item["id"], "status": "failed", "error": str(exc)}
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)
        self.invalidate()
        return outcomes

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
