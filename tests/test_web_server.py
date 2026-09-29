"""Detached web lifecycle and Click routing."""

import io
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import click
import pytest
from click.testing import CliRunner

from coldshelf.cli import cli
from coldshelf.web import server
from coldshelf.web.repository import WebRepository
from coldshelf.web.schemas import JobCreateRequest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / ".coldshelf-web")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_test_instance():
    with server._instance_lock():
        live, uncertain = server._discover_instances()
        assert not uncertain
        for instance in live:
            prepared = server._control(instance, "prepare")
            assert prepared["ok"], prepared
            assert server._control(instance, "shutdown")["ok"]
            server._wait_stopped(instance)


@contextmanager
def running_web(tmp_path, *, dev=False, token="private-token"):
    backend_port = free_port()
    frontend_port = free_port()
    while frontend_port == backend_port:
        frontend_port = free_port()
    db_path = tmp_path / "web.sqlite3"
    args = [
        "web", "--no-open", "--port", str(backend_port),
        "--db-path", str(db_path), "--token", token,
    ]
    if dev:
        args += ["--dev", "--frontend-port", str(frontend_port)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    try:
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain
            instance = live[0]
        yield instance, backend_port, frontend_port, db_path, result
    finally:
        stop_test_instance()


def test_web_help_and_dry_run_routing(monkeypatch):
    def unexpected(**_kwargs):
        raise AssertionError("Dry-run and help must not start web")

    monkeypatch.setattr(server.main, "callback", unexpected)
    monkeypatch.setattr(server.restart, "callback", unexpected)
    runner = CliRunner()
    root_help = runner.invoke(cli, ["--help"])
    assert root_help.exit_code == 0 and "web" in root_help.output
    web_help = runner.invoke(cli, ["web", "--help"])
    assert web_help.exit_code == 0
    for option in (
        "--host", "--port", "--dev", "--frontend-port", "--db-path",
        "--token", "--open", "--no-open", "restart",
    ):
        assert option in web_help.output
    for args in (["--dry-run", "web"], ["--dry-run", "web", "restart"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 2
        assert "--dry-run is not supported for web" in result.output


def test_urls_and_defaults():
    assert server.DEFAULT_BACKEND_PORT == 38765
    assert server.DEFAULT_FRONTEND_PORT == 38766
    assert server._dashboard_url("0.0.0.0", 38765, "a token&value") == (
        "http://127.0.0.1:38765/?token=a%20token%26value"
    )
    assert server._dashboard_url("::1", 9000, "secret") == (
        "http://[::1]:9000/?token=secret"
    )
    assert server._dashboard_url(
        "::", 38765, "secret", dev=True, frontend_port=45174
    ) == "http://127.0.0.1:45174/?token=secret"


def test_detached_packaged_start_and_browser_options(isolated, monkeypatch):
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)
    port = free_port()
    db_path = isolated / "packaged.sqlite3"
    result = CliRunner().invoke(
        cli, ["web", "--port", str(port), "--db-path", str(db_path), "--token", "abc"],
    )
    assert result.exit_code == 0, result.output
    try:
        assert f"Open: http://127.0.0.1:{port}/?token=abc" in result.output
        assert opened == [f"http://127.0.0.1:{port}/?token=abc"]
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain
            assert live[0]["pid"] != os.getpid()
            assert server._record_path(live[0]["id"]).stat().st_mode & 0o077 == 0
            assert server.STATE_DIR.stat().st_mode & 0o077 == 0
            if Path(f"/proc/{live[0]['pid']}/cmdline").exists():
                assert b"abc" not in Path(f"/proc/{live[0]['pid']}/cmdline").read_bytes()
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            assert json.load(response) == {"status": "ok"}
    finally:
        stop_test_instance()

    with running_web(isolated, token="abc") as (_, _, _, _, no_open):
        assert no_open.exit_code == 0
        assert len(opened) == 1


def test_dev_start_restart_readiness_and_preserved_settings(isolated, monkeypatch):
    monkeypatch.setattr(server.webbrowser, "open", lambda _url: (_ for _ in ()).throw(
        AssertionError("--no-open must suppress browser launch")
    ))
    with running_web(isolated, dev=True, token="dev-secret") as (
        original, backend_port, frontend_port, db_path, start,
    ):
        assert f"Open: http://127.0.0.1:{frontend_port}/?token=dev-secret" in start.output
        with urlopen(f"http://127.0.0.1:{backend_port}/health", timeout=2) as response:
            assert response.status == 200
        with urlopen(f"http://127.0.0.1:{frontend_port}/", timeout=2) as response:
            assert response.status == 200
        monkeypatch.setenv("COLDSHELF_WEB_TOKEN", "different-environment-token")
        result = CliRunner().invoke(cli, ["web", "restart"])
        assert result.exit_code == 0, result.output
        assert "Web dashboard restarted." in result.output
        assert "dev-secret" not in result.output
        assert "different-environment-token" not in result.output
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain
            assert live[0]["pid"] != original["pid"]
            settings = server._read_private_json(server._secret_path(live[0]["id"]))
        assert settings["dev"] is True
        assert settings["port"] == backend_port
        assert settings["frontend_port"] == frontend_port
        assert settings["db_path"] == str(db_path)
        assert settings["token"] == "dev-secret"
        assert settings["open_browser"] is False
        with urlopen(f"http://127.0.0.1:{frontend_port}/", timeout=2) as response:
            assert response.status == 200
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", frontend_port)) != 0


def test_packaged_restart_keeps_host_db_token_and_history(isolated, monkeypatch):
    with running_web(isolated, token="unique-restart-token") as (
        original, port, _, db_path, _,
    ):
        history = WebRepository(db_path)
        history.create_job("historic-job", JobCreateRequest(path=str(isolated)), isolated)
        history.mark_job_completed("historic-job")
        monkeypatch.setenv("COLDSHELF_WEB_TOKEN", "wrong-token")
        result = CliRunner().invoke(cli, ["web", "restart"])
        assert result.exit_code == 0, result.output
        assert "unique-restart-token" not in result.output
        assert "wrong-token" not in result.output
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain
            assert live[0]["pid"] != original["pid"]
            settings = server._read_private_json(server._secret_path(live[0]["id"]))
        assert settings["host"] == "127.0.0.1"
        assert settings["port"] == port
        assert settings["db_path"] == str(db_path)
        assert settings["token"] == "unique-restart-token"
        assert WebRepository(db_path).get_job("historic-job")["status"] == "completed"


def test_restart_retains_open_behavior_without_printing_token(isolated, monkeypatch):
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)
    port = free_port()
    result = CliRunner().invoke(cli, [
        "web", "--port", str(port), "--db-path", str(isolated / "open.sqlite3"),
        "--token", "open-secret",
    ])
    assert result.exit_code == 0, result.output
    try:
        restarted = CliRunner().invoke(cli, ["web", "restart"])
        assert restarted.exit_code == 0, restarted.output
        assert "open-secret" not in restarted.output
        assert opened == [
            f"http://127.0.0.1:{port}/?token=open-secret",
            f"http://127.0.0.1:{port}/?token=open-secret",
        ]
    finally:
        stop_test_instance()


def test_start_keeps_default_options(isolated, monkeypatch):
    captured = []
    monkeypatch.setattr(server, "_launch", lambda settings, _lock_fd: captured.append(settings))
    result = CliRunner().invoke(cli, ["web", "--no-open", "--token", "test-token"])
    assert result.exit_code == 0, result.output
    assert captured[0]["host"] == "127.0.0.1"
    assert captured[0]["port"] == 38765
    assert captured[0]["frontend_port"] == 38766
    assert captured[0]["dev"] is False
    assert captured[0]["open_browser"] is False


def test_restart_without_instance_exits_without_starting(isolated, monkeypatch):
    monkeypatch.setattr(server, "_launch", lambda *_args: pytest.fail("launched"))
    result = CliRunner().invoke(cli, ["web", "restart"])
    assert result.exit_code == 1
    assert "No single Coldshelf web instance could be verified" in result.output


def test_second_start_on_different_port_and_database_does_not_reconcile(isolated):
    with running_web(isolated):
        different_db = isolated / "different.sqlite3"
        result = CliRunner().invoke(cli, [
            "web", "--no-open", "--port", str(free_port()),
            "--db-path", str(different_db), "--token", "other",
        ])
        assert result.exit_code == 1
        assert "already running" in result.output
        assert not different_db.exists()
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain


def test_unregistered_foreground_listener_blocks_start(isolated, monkeypatch):
    monkeypatch.setattr(server, "_unregistered_web_listeners", lambda _recorded: [12345])
    monkeypatch.setattr(server, "_launch", lambda *_args: pytest.fail("launched"))
    result = CliRunner().invoke(cli, ["web", "--no-open", "--token", "secret"])
    assert result.exit_code == 1
    assert "cannot be verified" in result.output


def test_legacy_web_command_recognition():
    assert server._looks_like_web_command(["/usr/bin/python", "-m", "coldshelf.web.server"])
    assert server._looks_like_web_command(["/venv/bin/coldshelf", "web", "--port", "9000"])
    assert not server._looks_like_web_command(["/venv/bin/coldshelf", "web", "restart"])
    assert not server._looks_like_web_command(["/usr/bin/python", "-c", "print('web')"])


@pytest.mark.parametrize("failure", ["backend", "frontend", "backend_with_dev"])
def test_failed_listener_start_has_no_partial_instance(isolated, failure):
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    blocked_port = occupied.getsockname()[1]
    backend_port = free_port() if failure == "frontend" else blocked_port
    frontend_port = blocked_port if failure == "frontend" else free_port()
    args = [
        "web", "--no-open", "--port", str(backend_port),
        "--db-path", str(isolated / "failed.sqlite3"), "--token", "secret",
    ]
    if failure != "backend":
        args += ["--dev", "--frontend-port", str(frontend_port)]
    try:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 1, result.output
        assert "Web startup failed" in result.output
        assert "Web dashboard started" not in result.output
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert not live and not uncertain
        probe_port = backend_port if failure == "frontend" else frontend_port
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", probe_port)) != 0
    finally:
        occupied.close()


def test_concurrent_process_starts_have_one_winner(isolated):
    port = free_port()
    program = (
        "from click.testing import CliRunner; from coldshelf.cli import cli; "
        "import sys; result=CliRunner().invoke(cli, "
        "['web','--no-open','--port',sys.argv[1],'--db-path',sys.argv[2],"
        "'--token','parallel-secret']); print(result.output); "
        "sys.exit(result.exit_code)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    args = [sys.executable, "-c", program, str(port), str(isolated / "parallel.sqlite3")]
    first = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first_out, _ = first.communicate(timeout=50)
        second_out, _ = second.communicate(timeout=50)
        assert sorted([first.returncode, second.returncode]) == [0, 1]
        assert "already running" in first_out + second_out
        with server._instance_lock():
            live, uncertain = server._discover_instances()
            assert len(live) == 1 and not uncertain
    finally:
        if first.poll() is None:
            first.kill()
        if second.poll() is None:
            second.kill()
        stop_test_instance()


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_stale_reused_pid_and_wrong_owner_records_are_ignored(isolated, monkeypatch, wrong_owner):
    with server._instance_lock():
        instance_id = str(server.uuid.uuid4())
        uid, birth = server._process_identity(os.getpid())
        record = {
            "id": instance_id, "pid": os.getpid(),
            "uid": uid + 1 if wrong_owner else uid,
            "birth": birth if wrong_owner else "reused-pid-birth",
        }
        server._write_private_json(server._record_path(instance_id), record)
        server._write_private_json(server._secret_path(instance_id), {"token": "secret"})
        live, uncertain = server._discover_instances()
        assert not live and not uncertain
        assert not server._record_path(instance_id).exists()
        assert not server._secret_path(instance_id).exists()
    started = []
    monkeypatch.setattr(server, "_launch", lambda settings, _lock: started.append(settings))
    result = CliRunner().invoke(cli, ["web", "--no-open", "--token", "new-token"])
    assert result.exit_code == 0, result.output
    assert len(started) == 1


def test_unverifiable_instance_is_never_signalled_or_restarted(isolated, monkeypatch):
    with server._instance_lock():
        instance_id = str(server.uuid.uuid4())
        uid, birth = server._process_identity(os.getpid())
        server._write_private_json(server._record_path(instance_id), {
            "id": instance_id, "pid": os.getpid(), "uid": uid, "birth": birth,
        })
    monkeypatch.setattr(server, "_launch", lambda *_args: pytest.fail("launched"))
    result = CliRunner().invoke(cli, ["web", "restart"])
    assert result.exit_code == 1
    assert "could be verified" in result.output
    assert server._process_identity(os.getpid()) == (uid, birth)


def test_multiple_instances_are_ambiguous_without_shutdown(isolated, monkeypatch):
    monkeypatch.setattr(server, "_discover_instances", lambda: ([{"id": "one"}, {"id": "two"}], []))
    monkeypatch.setattr(server, "_control", lambda *_args: pytest.fail("signalled"))
    result = CliRunner().invoke(cli, ["web", "restart"])
    assert result.exit_code == 1
    assert "ambiguous" in result.output


def test_restart_busy_refusal_does_not_shutdown_or_launch(isolated, monkeypatch):
    instance = {"id": str(server.uuid.uuid4())}
    with server._instance_lock():
        server._write_private_json(server._secret_path(instance["id"]), {
            "host": "127.0.0.1", "port": 38765, "dev": False,
            "frontend_port": 38766, "db_path": str(isolated / "web.sqlite3"),
            "token": "hidden-token", "open_browser": False,
            "python": sys.executable, "source_root": str(isolated), "cwd": str(isolated),
        })
    calls = []
    monkeypatch.setattr(server, "_discover_instances", lambda: ([instance], []))
    monkeypatch.setattr(server, "_control", lambda _record, command: calls.append(command) or {
        "ok": False, "reason": "A Browse deletion is scheduled",
    })
    monkeypatch.setattr(server, "_launch", lambda *_args: pytest.fail("launched"))
    result = CliRunner().invoke(cli, ["web", "restart"])
    assert result.exit_code == 1
    assert "Retry when idle" in result.output
    assert "hidden-token" not in result.output
    assert calls == ["prepare"]


@pytest.mark.parametrize("failure", ["shutdown", "replacement"])
def test_restart_failure_never_reports_success(isolated, monkeypatch, failure):
    instance = {"id": str(server.uuid.uuid4())}
    settings = {
        "host": "127.0.0.1", "port": 38765, "dev": False,
        "frontend_port": 38766, "db_path": str(isolated / "web.sqlite3"),
        "token": "hidden-token", "open_browser": False,
        "python": sys.executable, "source_root": str(isolated), "cwd": str(isolated),
    }
    with server._instance_lock():
        server._write_private_json(server._secret_path(instance["id"]), settings)
    calls = []
    monkeypatch.setattr(server, "_discover_instances", lambda: ([instance], []))
    monkeypatch.setattr(server, "_control", lambda _record, command: calls.append(command) or {"ok": True})
    if failure == "shutdown":
        monkeypatch.setattr(server, "_wait_stopped", lambda _instance: (_ for _ in ()).throw(
            OSError("timeout")
        ))
        monkeypatch.setattr(server, "_launch", lambda *_args: pytest.fail("launched"))
    else:
        monkeypatch.setattr(server, "_wait_stopped", lambda _instance: None)
        monkeypatch.setattr(server, "_launch", lambda *_args: (_ for _ in ()).throw(
            click.ClickException("replacement failed")
        ))
    result = CliRunner().invoke(cli, ["web", "restart"])
    assert result.exit_code == 1
    assert "Web dashboard restarted." not in result.output
    assert "hidden-token" not in result.output
    if failure == "shutdown":
        assert calls == ["prepare", "shutdown", "abort"]
    else:
        assert calls == ["prepare", "shutdown"]


def test_child_process_arguments_do_not_contain_token(isolated, monkeypatch):
    captured = {}

    class Input(io.BytesIO):
        def close(self):
            captured["input"] = self.getvalue()
            super().close()

    class FakeProcess:
        stdin = Input()

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.pid = os.getpid()
            instance_id = args[4]
            server._write_private_json(server._record_path(instance_id), {"id": instance_id})
            captured["record"] = {"id": instance_id}

        def poll(self):
            return None

    monkeypatch.setattr(server.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(server, "_discover_instances", lambda: ([captured["record"]], []))
    settings = {
        "host": "127.0.0.1", "port": 38765, "dev": False,
        "frontend_port": 38766, "db_path": str(isolated / "web.sqlite3"),
        "token": "very-private-token", "open_browser": False,
        "python": sys.executable, "source_root": str(isolated), "cwd": str(isolated),
    }
    with server._instance_lock() as lock_fd:
        server._launch(settings, lock_fd)
    assert "very-private-token" not in " ".join(captured["args"])
    assert captured["kwargs"]["start_new_session"] is True
    assert json.loads(captured["input"])["token"] == "very-private-token"
    assert server._secret_path(captured["record"]["id"]).stat().st_mode & 0o077 == 0
