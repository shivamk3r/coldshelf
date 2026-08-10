"""Strict subprocess adapter for the user-installed Restic executable."""

import json
import math
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .policy import RESTIC_EXCLUDE_PATTERNS, is_ignored_backup_path


MIN_RESTIC_VERSION = (0, 19, 1)
ResticProgressCallback = Callable[[float, str, Optional[int]], None]


class ResticError(RuntimeError):
    """Raised when Restic is unavailable or reports an unsafe result."""

    def __init__(self, message: str, exit_code: Optional[int] = None):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ResticResult:
    exit_code: int
    messages: List[Dict[str, Any]]
    output: str


class ResticRunner:
    """Run Restic with JSON progress, strict exit codes, and cancellation."""

    def __init__(
        self,
        binary: str = "restic",
        popen_factory: Callable = subprocess.Popen,
        run_factory: Callable = subprocess.run,
    ):
        self.binary = binary
        self._popen_factory = popen_factory
        self._run_factory = run_factory
        self._active: Dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    def preflight(self) -> Dict[str, Any]:
        try:
            result = self._run_factory(
                [self.binary, "version", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ResticError(
                "Restic >= 0.19.1 is required. Install it with 'brew install restic'."
            ) from exc
        if result.returncode != 0:
            raise ResticError(_safe_error(result.stderr or result.stdout), result.returncode)
        try:
            payload = json.loads(result.stdout)
            version_text = str(payload["version"])
        except (json.JSONDecodeError, KeyError, TypeError):
            version_text = str(result.stdout).strip()
            payload = {"version": version_text}
        version = _parse_version(version_text)
        if version < MIN_RESTIC_VERSION:
            raise ResticError(
                f"Restic {version_text} is too old; version 0.19.1 or newer is required"
            )
        return payload

    def initialize_repository(
        self,
        repository: Path,
        folder_id: str,
        operation_id: Optional[str] = None,
    ) -> str:
        result = self.run(
            ["init", "--json"],
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
        )
        for message in result.messages:
            if message.get("message_type") == "initialized" and message.get("id"):
                return str(message["id"])
        raise ResticError("Restic initialized the repository without returning its identity")

    def repository_id(
        self,
        repository: Path,
        folder_id: str,
        operation_id: Optional[str] = None,
    ) -> str:
        result = self.run(
            ["cat", "config"],
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
        )
        payload = _single_json(result)
        repository_id = payload.get("id")
        if not repository_id:
            raise ResticError("Restic repository config does not contain an identity")
        return str(repository_id)

    def backup(
        self,
        repository: Path,
        folder_id: str,
        generation_id: str,
        source_path: Path,
        *,
        operation_id: str,
        progress: Optional[ResticProgressCallback] = None,
    ) -> str:
        tags = _snapshot_tags(folder_id, generation_id)
        args = [
            "backup",
            ".",
            "--json",
            "--exclude=/.disk-space-manager/**",
        ]
        args.extend(f"--exclude={pattern}" for pattern in RESTIC_EXCLUDE_PATTERNS)
        for tag in tags:
            args.extend(["--tag", tag])
        result = self.run(
            args,
            repository=repository,
            folder_id=folder_id,
            cwd=source_path,
            operation_id=operation_id,
            progress=progress,
            include_eta=True,
        )
        for message in reversed(result.messages):
            if message.get("message_type") == "summary" and message.get("snapshot_id"):
                return str(message["snapshot_id"])
        snapshots = self.snapshots(
            repository,
            folder_id,
            required_tags=tags,
            operation_id=operation_id,
        )
        if not snapshots:
            raise ResticError("Restic completed without a snapshot for the indexed generation")
        return str(snapshots[-1]["id"])

    def snapshots(
        self,
        repository: Path,
        folder_id: str,
        required_tags: Optional[Sequence[str]] = None,
        operation_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        args = ["snapshots", "--json"]
        for tag in required_tags or ():
            args.extend(["--tag", tag])
        result = self.run(
            args,
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
        )
        payload = _single_json(result)
        if isinstance(payload, list):
            snapshots = payload
        else:
            snapshots = [
                message
                for message in result.messages
                if message.get("id") and message.get("tree")
            ]
        required = set(required_tags or ())
        if required:
            snapshots = [
                item
                for item in snapshots
                if required.issubset(set(item.get("tags") or ()))
            ]
        return sorted(snapshots, key=lambda item: str(item.get("time", "")))

    def list_snapshot(
        self,
        repository: Path,
        folder_id: str,
        snapshot_id: str,
        operation_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        result = self.run(
            ["ls", snapshot_id, "--json"],
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
        )
        entries = []
        for message in result.messages:
            if message.get("struct_type") != "node" or message.get("type") != "file":
                continue
            relative_path = str(message.get("path") or "").lstrip("/")
            if (
                not relative_path
                or relative_path.startswith(".disk-space-manager/")
                or is_ignored_backup_path(relative_path)
            ):
                continue
            entries.append(
                {
                    "relative_path": relative_path,
                    "size": int(message.get("size") or 0),
                    "mtime": message.get("mtime"),
                    "snapshot_id": snapshot_id,
                }
            )
        return sorted(entries, key=lambda item: item["relative_path"])

    def check(
        self,
        repository: Path,
        folder_id: str,
        *,
        full: bool = False,
        operation_id: Optional[str] = None,
        progress: Optional[ResticProgressCallback] = None,
    ) -> None:
        args = ["check", "--json"]
        if full:
            args.append("--read-data")
        result = self.run(
            args,
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
            progress=progress,
        )
        for message in result.messages:
            if message.get("message_type") == "summary" and int(
                message.get("num_errors") or 0
            ):
                raise ResticError("Restic check reported repository errors", 1)

    def restore(
        self,
        repository: Path,
        folder_id: str,
        snapshot_id: str,
        relative_paths: Sequence[str],
        target: Path,
        *,
        operation_id: str,
        progress: Optional[ResticProgressCallback] = None,
    ) -> None:
        args = ["restore", snapshot_id, "--target", str(target), "--json"]
        for relative_path in relative_paths:
            args.extend(["--include", f"/{relative_path}"])
        self.run(
            args,
            repository=repository,
            folder_id=folder_id,
            operation_id=operation_id,
            progress=progress,
        )

    def verify_snapshot_paths(
        self,
        repository: Path,
        folder_id: str,
        snapshot_id: str,
        relative_paths: Iterable[str],
        *,
        operation_id: str,
        progress: Optional[ResticProgressCallback] = None,
    ) -> None:
        paths = list(relative_paths)
        for index, relative_path in enumerate(paths, start=1):
            self._run_binary_to_null(
                ["dump", snapshot_id, f"/{relative_path}"],
                repository=repository,
                folder_id=folder_id,
                operation_id=operation_id,
            )
            if progress:
                progress(index / max(len(paths), 1), f"Verified {index}/{len(paths)} files")

    def run(
        self,
        args: Sequence[str],
        *,
        repository: Optional[Path] = None,
        folder_id: Optional[str] = None,
        cwd: Optional[Path] = None,
        operation_id: Optional[str] = None,
        progress: Optional[ResticProgressCallback] = None,
        include_eta: bool = False,
    ) -> ResticResult:
        env = self._environment(repository, folder_id)
        cmd = [self.binary, *args]
        try:
            process = self._popen_factory(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ResticError(
                "Restic >= 0.19.1 is required. Install it with 'brew install restic'."
            ) from exc

        if operation_id:
            with self._lock:
                self._active[operation_id] = process
        line_queue: queue.Queue = queue.Queue()

        def reader() -> None:
            stdout = process.stdout
            if stdout is None:
                line_queue.put(None)
                return
            for line in stdout:
                line_queue.put(line)
            line_queue.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        output_lines: List[str] = []
        messages: List[Dict[str, Any]] = []
        reader_finished = False
        try:
            try:
                while not reader_finished or process.poll() is None:
                    try:
                        line = line_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        reader_finished = True
                        continue
                    output_lines.append(line)
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        messages.append(message)
                        _emit_progress(message, progress, include_eta=include_eta)
                exit_code = process.wait()
            except BaseException:
                _interrupt_process(process)
                _wait_or_terminate(process)
                raise
        finally:
            if operation_id:
                with self._lock:
                    self._active.pop(operation_id, None)

        output = "".join(output_lines)
        if not messages:
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                messages = [parsed]
            elif isinstance(parsed, list):
                messages = [{"_document": parsed}]
        if exit_code != 0:
            if exit_code == 130:
                raise ResticError("Restic operation was cancelled", exit_code)
            raise ResticError(_safe_error(output), exit_code)
        return ResticResult(exit_code=exit_code, messages=messages, output=output)

    def cancel(self, operation_id: str) -> bool:
        with self._lock:
            process = self._active.get(operation_id)
        if process is None or process.poll() is not None:
            return False
        _interrupt_process(process)
        threading.Thread(target=_wait_or_terminate, args=(process,), daemon=True).start()
        return True

    def _run_binary_to_null(
        self,
        args: Sequence[str],
        *,
        repository: Path,
        folder_id: str,
        operation_id: str,
    ) -> None:
        env = self._environment(repository, folder_id)
        try:
            process = self._popen_factory(
                [self.binary, *args],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ResticError("Restic executable is unavailable") from exc
        with self._lock:
            self._active[operation_id] = process
        try:
            _, stderr = process.communicate()
            if process.returncode != 0:
                if process.returncode == 130:
                    raise ResticError("Restic operation was cancelled", 130)
                raise ResticError(_safe_error(stderr), process.returncode)
        finally:
            with self._lock:
                self._active.pop(operation_id, None)

    def _environment(
        self, repository: Optional[Path], folder_id: Optional[str]
    ) -> Dict[str, str]:
        env = os.environ.copy()
        for inherited in (
            "RESTIC_PASSWORD",
            "RESTIC_PASSWORD_FILE",
            "RESTIC_PASSWORD_COMMAND",
            "RESTIC_REPOSITORY_FILE",
        ):
            env.pop(inherited, None)
        env["RESTIC_PROGRESS_FPS"] = "2"
        if repository is not None:
            env["RESTIC_REPOSITORY"] = str(repository)
        if folder_id:
            executable = shlex.quote(sys.executable)
            folder_arg = shlex.quote(folder_id)
            env["RESTIC_PASSWORD_COMMAND"] = (
                f"{executable} -m disk_space_manager.protection.password_command {folder_arg}"
            )
        return env


def _interrupt_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            return


def _wait_or_terminate(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except (OSError, ProcessLookupError):
            return


def _snapshot_tags(folder_id: str, generation_id: str) -> List[str]:
    return [f"dsm-set-{folder_id}", f"dsm-generation-{generation_id}", "dsm-protected"]


def generation_id_from_tags(tags: Iterable[str]) -> Optional[str]:
    prefix = "dsm-generation-"
    for tag in tags:
        if str(tag).startswith(prefix):
            return str(tag)[len(prefix) :]
    return None


def _parse_version(value: str) -> tuple:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ResticError(f"Could not parse Restic version: {value}")
    return tuple(int(part) for part in match.groups())


def _safe_error(output: str) -> str:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if not lines:
        return "Restic command failed without an error message"
    return "Restic failed: " + " | ".join(lines[-5:])[:2000]


def _emit_progress(
    message: Dict[str, Any],
    progress: Optional[ResticProgressCallback],
    *,
    include_eta: bool = False,
) -> None:
    if not progress:
        return
    message_type = message.get("message_type")
    if message_type == "status":
        ratio = float(message.get("percent_done") or 0)
        current = message.get("current_files") or []
        label = f"Processing {current[0]}" if current else "Processing files"
        eta_seconds = (
            _parse_eta_seconds(message.get("seconds_remaining")) if include_eta else None
        )
        progress(max(0.0, min(ratio, 1.0)), label, eta_seconds)
    elif message_type == "summary":
        progress(1.0, "Restic operation complete", None)
    elif message_type in {"error", "fatal"}:
        progress(0.0, str(message.get("message") or "Restic error"), None)


def _parse_eta_seconds(value: Any) -> Optional[int]:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return int(math.ceil(seconds))


def _single_json(result: ResticResult):
    if len(result.messages) == 1 and "_document" in result.messages[0]:
        return result.messages[0]["_document"]
    if len(result.messages) == 1:
        return result.messages[0]
    try:
        return json.loads(result.output)
    except json.JSONDecodeError as exc:
        raise ResticError("Restic did not return the expected JSON document") from exc
