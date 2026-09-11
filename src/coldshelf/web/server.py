"""Command entrypoint for the coldshelf web app."""

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import click
import uvicorn

from .app import create_app
from coldshelf.protection.repository import (
    DEFAULT_PROTECTION_DB_PATH,
    ProtectionRepository,
)
from .repository import DEFAULT_DB_PATH, WebRepository
from .security import get_or_create_token


DEFAULT_BACKEND_PORT = 38765
DEFAULT_FRONTEND_PORT = 38766


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=DEFAULT_BACKEND_PORT, show_default=True, type=int)
@click.option("--dev", is_flag=True, help="Run FastAPI with the Vite dev frontend.")
@click.option(
    "--frontend-port", default=DEFAULT_FRONTEND_PORT, show_default=True, type=int
)
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--token", default=None, help="API token. Defaults to env or local token file.")
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    show_default=True,
    help="Open the authenticated dashboard in the default browser.",
)
def main(
    host: str,
    port: int,
    dev: bool,
    frontend_port: int,
    db_path: Path,
    token: Optional[str],
    open_browser: bool,
) -> None:
    """Start the web server."""
    api_token = get_or_create_token(token)
    repository = WebRepository(db_path.expanduser())
    frontend_process = None
    dev_origin = None
    browser_host = _browser_host(host)

    if dev:
        frontend_dir = _find_frontend_dir()
        dev_origin = f"http://127.0.0.1:{frontend_port}"
        frontend_process = _start_frontend_dev(
            frontend_dir, browser_host, port, frontend_port, api_token
        )
        dashboard_url = _dashboard_url(
            host, port, api_token, dev=True, frontend_port=frontend_port
        )
        click.echo(f"Open:     {dashboard_url}")
        click.echo(f"Backend:  http://{host}:{port}")
    else:
        dashboard_url = _dashboard_url(host, port, api_token)
        click.echo(f"Open: {dashboard_url}")

    click.echo(f"Database: {repository.db_path}")
    if host != "127.0.0.1":
        click.echo("Network mode enabled. Keep the printed token private.")

    app = create_app(
        repository=repository,
        protection_repository=ProtectionRepository(DEFAULT_PROTECTION_DB_PATH),
        token=api_token,
        dev_origin=dev_origin,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info")
    )
    if open_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(server, dashboard_url, webbrowser.open),
            daemon=True,
            name="coldshelf-browser",
        ).start()
    try:
        server.run()
    finally:
        if frontend_process:
            frontend_process.terminate()
            try:
                frontend_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                frontend_process.kill()


def _browser_host(host: str) -> str:
    if host in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host


def _url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _dashboard_url(
    host: str,
    port: int,
    token: str,
    *,
    dev: bool = False,
    frontend_port: int = DEFAULT_FRONTEND_PORT,
) -> str:
    """Build a usable local URL even when Uvicorn binds a wildcard host."""
    encoded_token = quote(token, safe="")
    if dev:
        return f"http://127.0.0.1:{frontend_port}/?token={encoded_token}"
    return f"http://{_url_host(_browser_host(host))}:{port}/?token={encoded_token}"


def _open_when_ready(
    server: uvicorn.Server,
    url: str,
    opener: Callable[[str], bool],
    timeout: float = 15.0,
) -> None:
    """Open only after this Uvicorn instance has successfully started."""
    deadline = time.monotonic() + timeout
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        return
    try:
        opened = opener(url)
    except Exception as exc:
        click.echo(f"Could not open a browser automatically: {exc}", err=True)
        return
    if not opened:
        click.echo("Could not open a browser automatically; use the printed URL.", err=True)


def _find_frontend_dir() -> Path:
    candidates = [
        Path.cwd() / "frontend",
        Path(__file__).resolve().parents[3] / "frontend",
    ]
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    raise click.ClickException("Cannot find frontend/package.json for --dev mode")


def _start_frontend_dev(
    frontend_dir: Path,
    backend_host: str,
    backend_port: int,
    frontend_port: int,
    token: str,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["VITE_COLDSHELF_API_BASE"] = f"http://{_url_host(backend_host)}:{backend_port}"
    env["VITE_COLDSHELF_TOKEN"] = token
    cmd = [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        str(frontend_port),
    ]
    try:
        return subprocess.Popen(cmd, cwd=str(frontend_dir), env=env)
    except FileNotFoundError as exc:
        raise click.ClickException("npm is required for --dev mode") from exc


if __name__ == "__main__":
    sys.exit(main())
