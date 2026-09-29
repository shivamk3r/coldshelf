"""Detached, single-user lifecycle for the web dashboard."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

import click
import uvicorn

from coldshelf.protection.repository import DEFAULT_PROTECTION_DB_PATH, ProtectionRepository

from .app import create_app
from .repository import DEFAULT_DB_PATH, WebRepository
from .security import get_or_create_token


DEFAULT_BACKEND_PORT = 38765
DEFAULT_FRONTEND_PORT = 38766
STATE_DIR = Path.home() / ".coldshelf-web"
STARTUP_TIMEOUT = 40.0
SHUTDOWN_TIMEOUT = 20.0


@click.group(invoke_without_command=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=DEFAULT_BACKEND_PORT, show_default=True, type=int)
@click.option("--dev", is_flag=True, help="Run FastAPI with the Vite dev frontend.")
@click.option("--frontend-port", default=DEFAULT_FRONTEND_PORT, show_default=True, type=int)
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--token", default=None, help="API token. Defaults to env or local token file.")
@click.option(
    "--open/--no-open", "open_browser", default=True, show_default=True,
    help="Open the authenticated dashboard in the default browser.",
)
@click.pass_context
def main(
    ctx: click.Context,
    host: str,
    port: int,
    dev: bool,
    frontend_port: int,
    db_path: Path,
    token: Optional[str],
    open_browser: bool,
) -> None:
    """Start the dashboard in the background, or restart its running instance."""
    if ctx.invoked_subcommand:
        return
    with _instance_lock() as lock_fd:
        live, uncertain = _discover_instances()
        if live or uncertain:
            raise click.ClickException(
                "A Coldshelf web instance is already running or cannot be verified. "
                "Use 'coldshelf web restart' after it is idle."
            )
        if dev and port == frontend_port:
            raise click.ClickException("Backend and development frontend ports must differ.")
        settings = {
            "host": host,
            "port": port,
            "dev": dev,
            "frontend_port": frontend_port,
            "db_path": str(db_path.expanduser().resolve()),
            "token": get_or_create_token(token),
            "open_browser": open_browser,
            "python": sys.executable,
            "source_root": str(Path(__file__).resolve().parents[2]),
            "cwd": str(Path.cwd()),
        }
        _launch(settings, lock_fd)
    _report_ready(settings, restarted=False)


@main.command()
def restart() -> None:
    """Restart the one verified, idle web instance using its saved settings."""
    with _instance_lock() as lock_fd:
        live, uncertain = _discover_instances()
        if len(live) + len(uncertain) > 1:
            raise click.ClickException(
                "Multiple web instances or live instance records are present; "
                "restart is ambiguous. No server was stopped."
            )
        if uncertain or not live:
            raise click.ClickException(
                "No single Coldshelf web instance could be verified; no server was stopped."
            )
        instance = live[0]
        try:
            settings = _read_private_json(_secret_path(instance["id"]))
            _validate_settings(settings)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(
                "The running instance settings cannot be verified; no server was stopped."
            ) from exc

        try:
            prepared = _control(instance, "prepare")
        except (OSError, ValueError) as exc:
            raise click.ClickException(
                "The running instance did not answer the idle check; no server was stopped."
            ) from exc
        if not prepared.get("ok"):
            raise click.ClickException(
                f"Web work is pending or running ({prepared.get('reason', 'busy')}). "
                "Retry when idle; no server was stopped."
            )
        try:
            answer = _control(instance, "shutdown")
            if not answer.get("ok"):
                raise OSError("The running instance refused shutdown")
            _wait_stopped(instance)
        except (OSError, ValueError) as exc:
            try:
                _control(instance, "abort")
            except (OSError, ValueError):
                pass
            raise click.ClickException(
                "The running web server did not shut down cleanly; "
                "no replacement was started."
            ) from exc

        _launch(settings, lock_fd)
    _report_ready(settings, restarted=True)


def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _dashboard_url(
    host: str, port: int, token: str, *, dev: bool = False,
    frontend_port: int = DEFAULT_FRONTEND_PORT,
) -> str:
    encoded_token = quote(token, safe="")
    if dev:
        return f"http://127.0.0.1:{frontend_port}/?token={encoded_token}"
    return f"http://{_url_host(_browser_host(host))}:{port}/?token={encoded_token}"


def _report_ready(settings: Dict[str, Any], *, restarted: bool) -> None:
    url = _dashboard_url(
        settings["host"], settings["port"], settings["token"],
        dev=settings["dev"], frontend_port=settings["frontend_port"],
    )
    click.echo("Web dashboard restarted." if restarted else "Web dashboard started.")
    if restarted:
        click.echo(f"Dashboard: {url.split('?', 1)[0]}")
    else:
        click.echo(f"Open: {url}")
    if settings["dev"]:
        click.echo(f"Backend: http://{_url_host(settings['host'])}:{settings['port']}")
    click.echo(f"Database: {settings['db_path']}")
    if settings["host"] != "127.0.0.1":
        click.echo("Network mode enabled. Keep the API token private.")
    if settings["open_browser"]:
        hint = (
            "use an existing authenticated browser session."
            if restarted else "use the printed URL."
        )
        try:
            if not webbrowser.open(url):
                click.echo(f"Could not open a browser automatically; {hint}", err=True)
        except Exception:
            # Browser exceptions sometimes contain the URL, including its token.
            click.echo(f"Could not open a browser automatically; {hint}", err=True)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise click.ClickException("The web instance directory is not owned by this user.")
    if info.st_mode & 0o077:
        path.chmod(0o700)


@contextmanager
def _instance_lock():
    _ensure_private_directory(STATE_DIR)
    _ensure_private_directory(STATE_DIR / "instances")
    _ensure_private_directory(STATE_DIR / "secrets")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(STATE_DIR / "lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise click.ClickException("The web instance lock is not owned by this user.")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _record_path(instance_id: str) -> Path:
    return STATE_DIR / "instances" / f"{instance_id}.json"


def _secret_path(instance_id: str) -> Path:
    return STATE_DIR / "secrets" / f"{instance_id}.json"


def _socket_path(instance_id: str) -> Path:
    return STATE_DIR / f"s-{instance_id[:16]}.sock"


def _frontend_path(instance_id: str) -> Path:
    return STATE_DIR / f"frontend-{instance_id}.json"


def _write_private_json(path: Path, value: Dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_private_json(path: Path) -> Dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe instance state")
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("Invalid instance state")
    return value


def _process_identity(pid: int) -> Optional[tuple[int, str]]:
    """Kernel process owner and birth marker; a reused PID cannot match."""
    if pid <= 0:
        return None
    proc = Path("/proc") / str(pid)
    if proc.is_dir():
        try:
            raw = (proc / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            return proc.stat().st_uid, fields[19]
        except (OSError, IndexError, ValueError):
            return None
    try:
        output = subprocess.check_output(
            ["ps", "-o", "uid=", "-o", "lstart=", "-p", str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        uid, birth = output.split(maxsplit=1)
        return int(uid), birth
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _control(instance: Dict[str, Any], command: str) -> Dict[str, Any]:
    path = _socket_path(instance["id"])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Unsafe control socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        if hasattr(socket, "SO_PEERCRED"):
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, _ = struct.unpack("3i", peer)
            if peer_pid != instance["pid"] or peer_uid != os.getuid():
                raise ValueError("Control peer mismatch")
        elif hasattr(connection, "getpeereid"):
            peer_uid, _ = connection.getpeereid()
            if peer_uid != os.getuid():
                raise ValueError("Control peer mismatch")
        connection.sendall((json.dumps({"command": command}) + "\n").encode())
        data = bytearray()
        while not data.endswith(b"\n") and len(data) < 8192:
            chunk = connection.recv(8192 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    answer = json.loads(data)
    if not isinstance(answer, dict):
        raise ValueError("Invalid control reply")
    return answer


def _discover_instances() -> tuple[list[Dict[str, Any]], list[Path]]:
    live: list[Dict[str, Any]] = []
    uncertain: list[Path] = []
    recorded_pids: set[int] = set()
    for path in sorted((STATE_DIR / "instances").glob("*.json")):
        try:
            record = _read_private_json(path)
            instance_id = str(record["id"])
            if not uuid.UUID(instance_id) or path != _record_path(instance_id):
                raise ValueError("Invalid record name")
            pid = int(record["pid"])
            uid = int(record["uid"])
            birth = str(record["birth"])
        except (OSError, ValueError, KeyError, TypeError):
            uncertain.append(path)
            continue
        identity = _process_identity(pid)
        if identity is None or identity != (uid, birth) or uid != os.getuid():
            path.unlink(missing_ok=True)
            _secret_path(instance_id).unlink(missing_ok=True)
            continue
        recorded_pids.add(pid)
        try:
            answer = _control(record, "identify")
            if (
                answer.get("kind") == "coldshelf-web"
                and answer.get("id") == instance_id
                and answer.get("pid") == pid
                and answer.get("uid") == uid
                and answer.get("birth") == birth
            ):
                live.append(record)
            else:
                uncertain.append(path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            uncertain.append(path)
    uncertain.extend(
        Path(f"unregistered-pid-{pid}")
        for pid in _unregistered_web_listeners(recorded_pids)
    )
    return live, uncertain


def _unregistered_web_listeners(recorded_pids: set[int]) -> list[int]:
    """Find earlier foreground web commands, which have no control record."""
    found = []
    if Path("/proc/self/cmdline").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            if pid == os.getpid() or pid in recorded_pids:
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                args = (entry / "cmdline").read_bytes().decode(
                    "utf-8", errors="replace"
                ).split("\0")
            except OSError:
                continue
            if _looks_like_web_command(args) and _has_listening_socket(pid):
                found.append(pid)
        return found
    try:
        lines = subprocess.check_output(
            ["ps", "-axo", "pid=,uid=,command="], text=True, stderr=subprocess.DEVNULL,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return found
    for line in lines:
        try:
            pid_text, uid_text, command = line.strip().split(maxsplit=2)
            pid = int(pid_text)
            if int(uid_text) != os.getuid() or pid == os.getpid() or pid in recorded_pids:
                continue
            args = shlex.split(command)
        except (ValueError, IndexError):
            continue
        if _looks_like_web_command(args) and _has_listening_socket(pid):
            found.append(pid)
    return found


def _looks_like_web_command(args: list[str]) -> bool:
    for index, arg in enumerate(args[:4]):
        if arg == "-m" and index + 1 < len(args):
            if args[index + 1] == "coldshelf.web.server":
                return True
        name = Path(arg).name
        if name in {"coldshelf-web", "disk-space-manager-web"}:
            return True
        if name == "coldshelf" and index + 1 < len(args) and args[index + 1] == "web":
            return index + 2 >= len(args) or args[index + 2] != "restart"
    return False


def _has_listening_socket(pid: int) -> bool:
    if Path("/proc/self/fd").exists():
        listeners: set[str] = set()
        for table in (
            Path("/proc") / str(pid) / "net/tcp",
            Path("/proc") / str(pid) / "net/tcp6",
        ):
            try:
                for line in table.read_text(encoding="ascii").splitlines()[1:]:
                    fields = line.split()
                    if fields[3] == "0A":
                        listeners.add(fields[9])
            except (OSError, IndexError):
                continue
        try:
            for fd in (Path("/proc") / str(pid) / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    if target[8:-1] in listeners:
                        return True
            return False
        except OSError:
            return False
    try:
        return bool(subprocess.check_output(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            stderr=subprocess.DEVNULL,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        return False


def _validate_settings(settings: Dict[str, Any]) -> None:
    expected = {
        "host": str, "port": int, "dev": bool, "frontend_port": int,
        "db_path": str, "token": str, "open_browser": bool,
        "python": str, "source_root": str, "cwd": str,
    }
    if any(type(settings.get(key)) is not value for key, value in expected.items()):
        raise ValueError("Invalid instance settings")


def _launch(settings: Dict[str, Any], lock_fd: int) -> Dict[str, Any]:
    _validate_settings(settings)
    instance_id = str(uuid.uuid4())
    secret = _secret_path(instance_id)
    _write_private_json(secret, settings)
    env = os.environ.copy()
    env.pop("COLDSHELF_WEB_TOKEN", None)
    env.pop("DISK_SPACE_MANAGER_WEB_TOKEN", None)
    env["PYTHONPATH"] = settings["source_root"] + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        settings["python"], "-m", "coldshelf.web.server", "_serve",
        instance_id, str(STATE_DIR), str(lock_fd),
    ]
    log_fd: Optional[int] = None
    try:
        log_fd = os.open(
            STATE_DIR / "server.log",
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(log_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("Unsafe web server log")
        os.fchmod(log_fd, 0o600)
        working_dir = (
            settings["cwd"] if Path(settings["cwd"]).is_dir()
            else str(Path(settings["source_root"]).parent)
        )
        process = subprocess.Popen(
            command, cwd=working_dir, env=env, stdin=subprocess.PIPE,
            stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True,
            pass_fds=(lock_fd,),
        )
    except (OSError, ValueError) as exc:
        secret.unlink(missing_ok=True)
        raise click.ClickException("Could not launch the web server.") from exc
    finally:
        if log_fd is not None:
            os.close(log_fd)
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(settings).encode("utf-8"))
        process.stdin.close()
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The web server exited during startup")
            record_path = _record_path(instance_id)
            if record_path.exists():
                live, uncertain = _discover_instances()
                if uncertain:
                    raise RuntimeError("The new web instance could not be verified")
                for record in live:
                    if record["id"] == instance_id:
                        return record
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for the web server to become ready")
    except (OSError, RuntimeError, ValueError) as exc:
        _stop_partial_launch(process, instance_id)
        _record_path(instance_id).unlink(missing_ok=True)
        secret.unlink(missing_ok=True)
        _socket_path(instance_id).unlink(missing_ok=True)
        raise click.ClickException(
            f"Web startup failed: {exc}. See {STATE_DIR / 'server.log'}."
        ) from exc


def _stop_partial_launch(process: subprocess.Popen, instance_id: str) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The child owns a new session, so this cannot signal the terminal.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    _stop_recorded_frontend(instance_id)


def _stop_recorded_frontend(instance_id: str) -> None:
    path = _frontend_path(instance_id)
    try:
        record = _read_private_json(path)
        pid = int(record["pid"])
        identity = (int(record["uid"]), str(record["birth"]))
        if identity[0] == os.getuid() and _process_identity(pid) == identity:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _process_identity(pid) == identity:
                time.sleep(0.1)
            if _process_identity(pid) == identity:
                os.killpg(pid, signal.SIGKILL)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    finally:
        path.unlink(missing_ok=True)


def _wait_stopped(instance: Dict[str, Any]) -> None:
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if _process_identity(instance["pid"]) != (instance["uid"], instance["birth"]):
            return
        time.sleep(0.1)
    raise OSError("Graceful shutdown timed out")


def _find_frontend_dir() -> Path:
    candidates = [Path.cwd() / "frontend", Path(__file__).resolve().parents[3] / "frontend"]
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    raise RuntimeError("Cannot find frontend/package.json for --dev mode")


def _start_frontend_dev(
    frontend_dir: Path, backend_host: str, backend_port: int,
    frontend_port: int, token: str,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["VITE_COLDSHELF_API_BASE"] = f"http://{_url_host(backend_host)}:{backend_port}"
    env["VITE_COLDSHELF_TOKEN"] = token
    vite = frontend_dir / "node_modules" / ".bin" / "vite"
    if vite.exists():
        command = [str(vite), "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"]
    else:
        command = [
            "npm", "run", "dev", "--", "--host", "127.0.0.1",
            "--port", str(frontend_port), "--strictPort",
        ]
    return subprocess.Popen(
        command, cwd=str(frontend_dir), env=env, start_new_session=True,
    )


def _stop_frontend(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _vite_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/@vite/client", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _serve_child(instance_id: str, state_dir: Path, inherited_lock: int) -> None:
    global STATE_DIR
    STATE_DIR = state_dir
    settings = json.load(sys.stdin)
    _validate_settings(settings)
    identity = _process_identity(os.getpid())
    if identity is None or identity[0] != os.getuid():
        raise RuntimeError("Cannot verify the web process identity")
    record = {
        "id": instance_id, "pid": os.getpid(), "uid": identity[0],
        "birth": identity[1],
    }
    frontend: Optional[subprocess.Popen] = None
    control_thread: Optional[threading.Thread] = None
    stop_control = threading.Event()
    try:
        repository = WebRepository(Path(settings["db_path"]))
        dev_origin = None
        if settings["dev"]:
            frontend = _start_frontend_dev(
                _find_frontend_dir(), _browser_host(settings["host"]),
                settings["port"], settings["frontend_port"], settings["token"],
            )
            frontend_identity = _process_identity(frontend.pid)
            if frontend_identity is not None:
                _write_private_json(_frontend_path(instance_id), {
                    "pid": frontend.pid, "uid": frontend_identity[0],
                    "birth": frontend_identity[1],
                })
            dev_origin = f"http://127.0.0.1:{settings['frontend_port']}"
        app = create_app(
            repository=repository,
            protection_repository=ProtectionRepository(DEFAULT_PROTECTION_DB_PATH),
            token=settings["token"],
            dev_origin=dev_origin,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app, host=settings["host"], port=settings["port"],
                log_level="info", access_log=False,
            )
        )
        control_thread = threading.Thread(
            target=_control_loop,
            args=(
                record, app.state.restart_gate, server, frontend,
                settings["frontend_port"], stop_control, inherited_lock,
            ),
            daemon=True, name="coldshelf-web-control",
        )
        control_thread.start()
        server.run()
    finally:
        stop_control.set()
        if control_thread is not None:
            control_thread.join(timeout=3)
        else:
            os.close(inherited_lock)
        _record_path(instance_id).unlink(missing_ok=True)
        _socket_path(instance_id).unlink(missing_ok=True)
        _secret_path(instance_id).unlink(missing_ok=True)
        _stop_frontend(frontend)
        _frontend_path(instance_id).unlink(missing_ok=True)


def _control_loop(
    record: Dict[str, Any], gate: Any, server: uvicorn.Server,
    frontend: Optional[subprocess.Popen], frontend_port: int,
    stop: threading.Event, inherited_lock: int,
) -> None:
    listener: Optional[socket.socket] = None
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT - 2
        while not stop.is_set() and time.monotonic() < deadline:
            if server.started and (
                frontend is None or
                (frontend.poll() is None and _vite_ready(frontend_port))
            ):
                break
            if frontend is not None and frontend.poll() is not None:
                server.should_exit = True
                return
            time.sleep(0.1)
        else:
            server.should_exit = True
            return
        path = _socket_path(record["id"])
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(4)
        listener.settimeout(0.2)
        _write_private_json(_record_path(record["id"]), record)
        os.close(inherited_lock)
        inherited_lock = -1
        while not stop.is_set():
            if frontend is not None and frontend.poll() is not None:
                server.should_exit = True
                return
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(3)
                try:
                    data = bytearray()
                    while not data.endswith(b"\n") and len(data) < 4096:
                        chunk = connection.recv(4096 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    command = json.loads(data).get("command")
                    if command == "identify":
                        answer = {"ok": True, "kind": "coldshelf-web", **record}
                    elif command == "prepare":
                        reason = gate.prepare()
                        answer = {"ok": reason is None, "reason": reason}
                    elif command == "abort":
                        gate.abort()
                        answer = {"ok": True}
                    elif command == "shutdown" and gate.draining:
                        answer = {"ok": True}
                    else:
                        answer = {"ok": False, "reason": "Invalid control request"}
                    connection.sendall((json.dumps(answer) + "\n").encode())
                    if command == "shutdown" and answer["ok"]:
                        server.should_exit = True
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
    finally:
        if inherited_lock >= 0:
            os.close(inherited_lock)
        if listener is not None:
            listener.close()
        if not stop.is_set():
            server.should_exit = True


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "_serve":
        _serve_child(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
    else:
        main()
