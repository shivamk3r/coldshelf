"""Tests for web-server URL selection and browser startup behavior."""

from types import SimpleNamespace

from click.testing import CliRunner

from coldshelf.cli import cli
from coldshelf.web import server as web_server


def test_cli_help_exposes_web_options_without_starting_server(monkeypatch):
    def unexpected_startup(**kwargs):
        raise AssertionError("Help must not start the server")

    monkeypatch.setattr(web_server.main, "callback", unexpected_startup)
    runner = CliRunner()
    root_help = runner.invoke(cli, ["--help"])
    assert root_help.exit_code == 0, root_help.output
    assert "web" in root_help.output

    web_help = runner.invoke(cli, ["web", "--help"])
    assert web_help.exit_code == 0, web_help.output
    assert "cli web [OPTIONS]" in web_help.output
    for option in (
        "--host", "--port", "--dev", "--frontend-port", "--db-path",
        "--token", "--open", "--no-open",
    ):
        assert option in web_help.output


def test_cli_rejects_web_dry_run_before_startup(monkeypatch):
    def unexpected_startup(**kwargs):
        raise AssertionError("Dry-run rejection must precede server startup")

    def unexpected_banner():
        raise AssertionError("Web must not display a misleading dry-run banner")

    monkeypatch.setattr(web_server.main, "callback", unexpected_startup)
    monkeypatch.setattr("coldshelf.cli.ui.show_dry_run_banner", unexpected_banner)
    result = CliRunner().invoke(cli, ["--dry-run", "web"])
    assert result.exit_code == 2, result.output
    assert "--dry-run is not supported for web" in result.output
    assert "read-only" in result.output


def test_default_ports_are_project_specific():
    assert web_server.DEFAULT_BACKEND_PORT == 38765
    assert web_server.DEFAULT_FRONTEND_PORT == 38766


def test_dashboard_urls_cover_wildcard_ipv6_development_and_token_encoding():
    assert web_server._dashboard_url("0.0.0.0", 38765, "a token&value") == (
        "http://127.0.0.1:38765/?token=a%20token%26value"
    )
    assert web_server._dashboard_url("::", 38765, "secret") == (
        "http://127.0.0.1:38765/?token=secret"
    )
    assert web_server._dashboard_url("::1", 9000, "secret") == (
        "http://[::1]:9000/?token=secret"
    )
    assert web_server._dashboard_url("0.0.0.0", 38765, "secret", dev=True) == (
        "http://127.0.0.1:38766/?token=secret"
    )
    assert web_server._dashboard_url(
        "0.0.0.0", 38765, "secret", dev=True, frontend_port=45174
    ) == "http://127.0.0.1:45174/?token=secret"


def test_open_when_ready_uses_opener_and_handles_failure(capsys):
    ready = SimpleNamespace(started=True, should_exit=False)
    opened = []
    web_server._open_when_ready(ready, "http://example.test", lambda url: opened.append(url) or True)
    assert opened == ["http://example.test"]

    web_server._open_when_ready(ready, "http://example.test", lambda _url: False)
    assert "printed URL" in capsys.readouterr().err

    def failing_opener(_url):
        raise OSError("no desktop session")

    web_server._open_when_ready(ready, "http://example.test", failing_opener)
    assert "no desktop session" in capsys.readouterr().err


def test_cli_open_is_default_and_no_open_skips_browser_thread(tmp_path, monkeypatch):
    created_threads = []

    class FakeThread:
        def __init__(self, *, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name
            self.started = False
            created_threads.append(self)

        def start(self):
            self.started = True

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(web_server.threading, "Thread", FakeThread)
    monkeypatch.setattr(web_server.uvicorn, "Config", lambda *args, **kwargs: (args, kwargs))
    monkeypatch.setattr(web_server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(web_server, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(web_server, "ProtectionRepository", lambda *_args, **_kwargs: object())

    runner = CliRunner()
    opened = runner.invoke(
        cli,
        ["web", "--db-path", str(tmp_path / "open.sqlite3"), "--token", "secret"],
    )
    assert opened.exit_code == 0, opened.output
    assert "Open: http://127.0.0.1:38765/?token=secret" in opened.output
    assert len(created_threads) == 1
    assert created_threads[0].target is web_server._open_when_ready
    assert created_threads[0].started is True

    not_opened = runner.invoke(
        cli,
        [
            "web",
            "--host",
            "0.0.0.0",
            "--port",
            "42001",
            "--db-path",
            str(tmp_path / "closed.sqlite3"),
            "--token",
            "secret",
            "--no-open",
        ],
    )
    assert not_opened.exit_code == 0, not_opened.output
    assert "Open: http://127.0.0.1:42001/?token=secret" in not_opened.output
    assert len(created_threads) == 1


def test_dev_cli_uses_new_defaults_and_preserves_port_overrides(
    tmp_path, monkeypatch
):
    frontend_starts = []
    server_configs = []
    app_configs = []
    processes = []

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.wait_timeout = None
            processes.append(self)

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.wait_timeout = timeout

    class FakeServer:
        started = False
        should_exit = False

        def __init__(self, config):
            self.config = config

        def run(self):
            return None

    def fake_start_frontend(
        frontend_dir, backend_host, backend_port, frontend_port, token
    ):
        frontend_starts.append(
            (frontend_dir, backend_host, backend_port, frontend_port, token)
        )
        return FakeProcess()

    def fake_create_app(**kwargs):
        app_configs.append(kwargs)
        return object()

    def fake_uvicorn_config(*args, **kwargs):
        server_configs.append((args, kwargs))
        return (args, kwargs)

    monkeypatch.setattr(web_server, "_find_frontend_dir", lambda: tmp_path)
    monkeypatch.setattr(web_server, "_start_frontend_dev", fake_start_frontend)
    monkeypatch.setattr(web_server, "create_app", fake_create_app)
    monkeypatch.setattr(
        web_server, "ProtectionRepository", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(web_server.uvicorn, "Config", fake_uvicorn_config)
    monkeypatch.setattr(web_server.uvicorn, "Server", FakeServer)

    runner = CliRunner()
    default_result = runner.invoke(
        cli,
        [
            "web",
            "--dev",
            "--db-path",
            str(tmp_path / "default.sqlite3"),
            "--token",
            "secret",
            "--no-open",
        ],
    )

    assert default_result.exit_code == 0, default_result.output
    assert (
        "Open:     http://127.0.0.1:38766/?token=secret" in default_result.output
    )
    assert "Backend:  http://127.0.0.1:38765" in default_result.output
    assert frontend_starts[-1][1:] == ("127.0.0.1", 38765, 38766, "secret")
    assert server_configs[-1][1]["port"] == 38765
    assert app_configs[-1]["dev_origin"] == "http://127.0.0.1:38766"
    assert processes[-1].terminated is True
    assert processes[-1].wait_timeout == 5

    override_result = runner.invoke(
        cli,
        [
            "web",
            "--dev",
            "--port",
            "42001",
            "--frontend-port",
            "42002",
            "--db-path",
            str(tmp_path / "override.sqlite3"),
            "--token",
            "secret",
            "--no-open",
        ],
    )

    assert override_result.exit_code == 0, override_result.output
    assert (
        "Open:     http://127.0.0.1:42002/?token=secret" in override_result.output
    )
    assert "Backend:  http://127.0.0.1:42001" in override_result.output
    assert frontend_starts[-1][1:] == ("127.0.0.1", 42001, 42002, "secret")
    assert server_configs[-1][1]["port"] == 42001
    assert app_configs[-1]["dev_origin"] == "http://127.0.0.1:42002"
