import json
import plistlib
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coldshelf.protection.keychain import KeychainSecretStore
from coldshelf.protection.restic import (
    ResticError,
    ResticResult,
    ResticRunner,
    _emit_progress,
)
from coldshelf.protection.volumes import MacOSVolumeProvider


def _fake_restic_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-restic"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "version":
    print(json.dumps({"message_type": "version", "version": "0.19.1"}))
elif command == "fail":
    print(json.dumps({"message_type": "exit_error", "code": 3, "message": "partial read"}))
    raise SystemExit(3)
elif command == "ls":
    print(json.dumps({"struct_type": "snapshot", "id": "snapshot"}))
    print(json.dumps({"struct_type": "node", "type": "file", "path": "/a.bin", "size": 7}))
    print(json.dumps({"struct_type": "node", "type": "file", "path": "/nested/.DS_Store", "size": 8}))
else:
    print(json.dumps({"message_type": "summary"}))
"""
    )
    executable.chmod(0o755)
    return executable


def test_restic_json_version_ls_and_strict_exit_code_three(tmp_path):
    runner = ResticRunner(binary=str(_fake_restic_executable(tmp_path)))
    assert runner.preflight()["version"] == "0.19.1"
    assert runner.list_snapshot(tmp_path, "folder", "snapshot") == [
        {
            "relative_path": "a.bin",
            "size": 7,
            "mtime": None,
            "snapshot_id": "snapshot",
        }
    ]

    with pytest.raises(ResticError) as raised:
        runner.run(["fail"])
    assert raised.value.exit_code == 3


def test_restic_rejects_old_version_and_keeps_secret_out_of_arguments(monkeypatch):
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"version": "0.18.1"}),
        stderr="",
    )
    runner = ResticRunner(run_factory=lambda *args, **kwargs: completed)
    with pytest.raises(ResticError, match="too old"):
        runner.preflight()

    monkeypatch.setenv("RESTIC_PASSWORD", "must-not-leak")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/tmp/must-not-leak")
    monkeypatch.setenv("RESTIC_REPOSITORY_FILE", "/tmp/wrong-repository")
    environment = runner._environment(Path("/repo"), "folder-id")
    assert environment["RESTIC_REPOSITORY"] == "/repo"
    assert "password_command" in environment["RESTIC_PASSWORD_COMMAND"]
    assert "folder-id" in environment["RESTIC_PASSWORD_COMMAND"]
    assert "RESTIC_PASSWORD" not in environment
    assert "RESTIC_PASSWORD_FILE" not in environment
    assert "RESTIC_REPOSITORY_FILE" not in environment


def test_restic_cancel_sends_sigint_without_blocking():
    class Process:
        def __init__(self):
            self.signals = []

        def poll(self):
            return None

        def send_signal(self, value):
            self.signals.append(value)

        def wait(self, timeout=None):
            return 130

    process = Process()
    runner = ResticRunner()
    runner._active["operation"] = process
    assert runner.cancel("operation")
    assert process.signals == [signal.SIGINT]


def test_restic_backup_exclusion_and_transfer_eta_are_forwarded(tmp_path):
    runner = ResticRunner()
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["include_eta"] = kwargs["include_eta"]
        return ResticResult(
            exit_code=0,
            messages=[{"message_type": "summary", "snapshot_id": "snapshot"}],
            output="",
        )

    runner.run = run
    assert runner.backup(
        tmp_path / "repo",
        "folder",
        "generation",
        tmp_path,
        operation_id="backup",
    ) == "snapshot"
    assert "--exclude=**/.DS_Store" in captured["args"]
    assert captured["include_eta"] is True

    events = []
    callback = lambda ratio, message, eta: events.append((ratio, message, eta))
    _emit_progress(
        {
            "message_type": "status",
            "percent_done": 0.5,
            "current_files": ["video.bin"],
            "seconds_remaining": 12.2,
        },
        callback,
        include_eta=True,
    )
    _emit_progress(
        {
            "message_type": "status",
            "percent_done": 0.6,
            "seconds_remaining": "unknown",
        },
        callback,
        include_eta=True,
    )
    _emit_progress(
        {"message_type": "summary"},
        callback,
        include_eta=True,
    )

    assert events == [
        (0.5, "Processing video.bin", 13),
        (0.6, "Processing files", None),
        (1.0, "Restic operation complete", None),
    ]


def test_keychain_abstraction_round_trip(monkeypatch):
    values = {}
    monkeypatch.setattr(
        "coldshelf.protection.keychain.keyring.set_password",
        lambda service, key, value: values.__setitem__((service, key), value),
    )
    monkeypatch.setattr(
        "coldshelf.protection.keychain.keyring.get_password",
        lambda service, key: values.get((service, key)),
    )
    monkeypatch.setattr(
        "coldshelf.protection.keychain.keyring.delete_password",
        lambda service, key: values.pop((service, key)),
    )
    store = KeychainSecretStore("test-service")
    store.set("folder", "secret")
    assert store.get("folder") == "secret"
    assert store.matches("folder", "secret")
    assert not store.matches("folder", "wrong")
    store.delete("folder")


def test_diskutil_plist_volume_uuid_and_rename(tmp_path):
    volume_path = tmp_path / "Volumes" / "Renamed HDD"
    volume_path.mkdir(parents=True)
    payload = plistlib.dumps(
        {
            "VolumeUUID": "stable-volume-uuid",
            "VolumeName": "Renamed HDD",
            "MountPoint": str(volume_path),
            "FilesystemName": "ExFAT",
            "DeviceIdentifier": "disk4s1",
            "TotalSize": 1000,
            "FreeSpace": 900,
            "Writable": True,
            "Internal": False,
            "RemovableMedia": True,
        }
    )

    def run(command, **kwargs):
        assert command[:3] == ["diskutil", "info", "-plist"]
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr=b"")

    provider = MacOSVolumeProvider(
        volumes_root=volume_path.parent,
        run=run,
        system=lambda: "Darwin",
    )
    volume = provider.resolve(volume_path)
    assert volume.volume_uuid == "stable-volume-uuid"
    assert volume.name == "Renamed HDD"
    assert provider.find_by_uuid("stable-volume-uuid") == volume
