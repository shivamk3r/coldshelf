"""Tests for Unix-like external drive detection."""

from pathlib import Path

import pytest

import drive_detector


def test_parse_linux_mountinfo_detects_common_external_mounts():
    lines = [
        "26 23 0:21 / / rw,relatime - ext4 /dev/sda2 rw",
        "37 26 8:17 / /media/alex/Backup rw,nosuid,nodev - ext4 /dev/sdb1 rw",
        "38 26 8:33 / /run/media/alex/Photos\\040Drive rw,nosuid,nodev - exfat /dev/sdc1 rw",
        "39 26 8:49 / /mnt/work rw,nosuid,nodev - xfs /dev/sdd1 rw",
    ]

    volumes = list(drive_detector._parse_linux_mountinfo(lines))

    assert [v["path"] for v in volumes] == [
        "/media/alex/Backup",
        "/run/media/alex/Photos Drive",
        "/mnt/work",
    ]
    assert volumes[0]["device"] == "/dev/sdb1"
    assert volumes[1]["fs_type"] == "exfat"


def test_parse_linux_mountinfo_ignores_pseudo_and_system_mounts():
    lines = [
        "26 23 0:21 / / rw,relatime - ext4 /dev/sda2 rw",
        "40 26 0:34 / /proc rw,nosuid,nodev,noexec - proc proc rw",
        "41 26 0:35 / /run rw,nosuid,nodev - tmpfs tmpfs rw",
        "42 26 8:65 / /var/lib/data rw,relatime - ext4 /dev/sde1 rw",
    ]

    assert list(drive_detector._parse_linux_mountinfo(lines)) == []


def test_detect_external_drives_filters_to_writable_external_paths(monkeypatch, tmp_path):
    writable = tmp_path / "writable"
    readonly = tmp_path / "readonly"
    internal = tmp_path / "internal"
    writable.mkdir()
    readonly.mkdir()
    internal.mkdir()

    monkeypatch.setattr(
        drive_detector,
        "get_mounted_volumes",
        lambda: [
            {"path": str(writable), "name": "writable", "device": "/dev/sdb1"},
            {"path": str(readonly), "name": "readonly", "device": "/dev/sdc1"},
            {"path": str(internal), "name": "internal", "device": "/dev/sda2"},
        ],
    )
    monkeypatch.setattr(
        drive_detector,
        "is_external_drive",
        lambda path: Path(path) != internal,
    )
    monkeypatch.setattr(
        drive_detector.os,
        "access",
        lambda path, mode: Path(path) == writable,
    )
    monkeypatch.setattr(drive_detector, "get_available_space", lambda path: 12345)

    assert drive_detector.detect_external_drives() == [
        {
            "path": str(writable),
            "name": "writable",
            "device": "/dev/sdb1",
            "available_space": 12345,
        }
    ]


def test_select_external_drive_accepts_writable_manual_path(tmp_path):
    assert drive_detector.select_external_drive(str(tmp_path)) == tmp_path


def test_select_external_drive_rejects_missing_manual_path(tmp_path):
    with pytest.raises(ValueError, match="does not exist or is not writable"):
        drive_detector.select_external_drive(str(tmp_path / "missing"))


def test_select_external_drive_returns_first_detected_drive(monkeypatch, tmp_path):
    selected = tmp_path / "drive"
    monkeypatch.setattr(
        drive_detector,
        "detect_external_drives",
        lambda: [{"path": str(selected), "name": "drive", "device": "/dev/sdb1"}],
    )

    assert drive_detector.select_external_drive() == selected
