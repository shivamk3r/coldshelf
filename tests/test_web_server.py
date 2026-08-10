"""Tests for web-server URL selection and browser startup behavior."""

from types import SimpleNamespace

from click.testing import CliRunner

from disk_space_manager.web import server as web_server


def test_dashboard_urls_cover_wildcard_ipv6_development_and_token_encoding():
    assert web_server._dashboard_url("0.0.0.0", 8765, "a token&value") == (
        "http://127.0.0.1:8765/?token=a%20token%26value"
    )
    assert web_server._dashboard_url("::", 8765, "secret") == (
        "http://127.0.0.1:8765/?token=secret"
    )
    assert web_server._dashboard_url("::1", 9000, "secret") == (
        "http://[::1]:9000/?token=secret"
    )
    assert web_server._dashboard_url(
        "0.0.0.0", 8765, "secret", dev=True, frontend_port=5174
    ) == "http://127.0.0.1:5174/?token=secret"


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
        web_server.main,
        ["--db-path", str(tmp_path / "open.sqlite3"), "--token", "secret"],
    )
    assert opened.exit_code == 0, opened.output
    assert "Open: http://127.0.0.1:8765/?token=secret" in opened.output
    assert len(created_threads) == 1
    assert created_threads[0].target is web_server._open_when_ready
    assert created_threads[0].started is True

    not_opened = runner.invoke(
        web_server.main,
        [
            "--db-path",
            str(tmp_path / "closed.sqlite3"),
            "--token",
            "secret",
            "--no-open",
        ],
    )
    assert not_opened.exit_code == 0, not_opened.output
    assert len(created_threads) == 1
