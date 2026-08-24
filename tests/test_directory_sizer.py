"""Tests for metadata-only one-level directory sizing."""

import os
import stat
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from disk_space_manager import directory_sizer as sizing
from disk_space_manager.directory_sizer import (
    DirectorySizer,
    DirectorySizingCancelled,
)


def test_sizes_immediate_files_and_recursive_folders_with_hidden_totals(tmp_path):
    (tmp_path / "top.bin").write_bytes(b"abc")
    nested = tmp_path / "project"
    nested.mkdir()
    (nested / "nested.bin").write_bytes(b"1234567")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.bin").write_bytes(b"hidden")

    sizer = DirectorySizer()
    prepared = sizer.prepare(str(tmp_path))
    initial = {entry["name"]: entry for entry in prepared.entries}
    assert initial["top.bin"]["logical_bytes"] == 3
    assert initial["project"]["logical_bytes"] is None
    assert initial["project"]["size_state"] == "pending"

    result = sizer.scan(prepared)
    entries = {entry["name"]: entry for entry in result["entries"]}
    assert entries["project"]["logical_bytes"] == 7
    assert entries["project"]["file_count"] == 1
    assert entries["project"]["directory_count"] == 0
    assert result["logical_bytes"] == 3 + 7 + 6
    assert result["file_count"] == 3
    assert result["directory_count"] == 2
    assert result["hidden_entry_count"] == 1
    assert result["hidden_logical_bytes"] == 6
    assert result["allocated_bytes"] == sum(
        entry["allocated_bytes"] for entry in result["entries"]
    )


def test_does_not_follow_symlink_entries(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 1000)
    source = tmp_path / "source"
    source.mkdir()
    shortcut = source / "shortcut"
    shortcut.symlink_to(outside, target_is_directory=True)

    result = DirectorySizer().scan(DirectorySizer().prepare(str(source)))
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["kind"] == "symlink"
    assert entry["symlink"] is True
    assert entry["logical_bytes"] == shortcut.lstat().st_size
    assert result["file_count"] == 0


def test_same_filesystem_scan_skips_nested_mount_metadata(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    prepared = DirectorySizer().prepare(str(tmp_path))
    real_scandir = sizing.os.scandir

    class FakeEntry:
        name = "mounted.bin"
        path = str(child / "mounted.bin")

        @staticmethod
        def stat(follow_symlinks=False):
            return SimpleNamespace(
                st_dev=prepared.root_device + 1,
                st_mode=stat.S_IFREG,
                st_size=999,
                st_blocks=8,
            )

    class FakeScandir:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def __iter__(self):
            return iter([FakeEntry()])

    monkeypatch.setattr(
        sizing.os,
        "scandir",
        lambda path: FakeScandir() if Path(path) == child else real_scandir(path),
    )
    result = DirectorySizer().scan(prepared)
    entry = result["entries"][0]
    assert entry["logical_bytes"] == 0
    assert entry["skipped_mount_count"] == 1


def test_unreadable_descendant_produces_partial_result(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    prepared = DirectorySizer().prepare(str(tmp_path))
    real_scandir = sizing.os.scandir

    def guarded_scandir(path):
        if Path(path) == child:
            raise PermissionError("blocked for test")
        return real_scandir(path)

    monkeypatch.setattr(sizing.os, "scandir", guarded_scandir)
    result = DirectorySizer().scan(prepared)
    entry = result["entries"][0]
    assert entry["size_state"] == "partial"
    assert entry["error_count"] == 1
    assert result["error_count"] == 1
    assert "blocked for test" in result["errors"][0]


def test_immediate_entry_with_unreadable_metadata_remains_visible(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked.bin"
    blocked.write_bytes(b"private")
    real_initial_entry = sizing._initial_entry

    def fail_blocked_entry(child, root_device):
        if child.name == blocked.name:
            raise PermissionError("metadata blocked for test")
        return real_initial_entry(child, root_device)

    monkeypatch.setattr(sizing, "_initial_entry", fail_blocked_entry)
    sizer = DirectorySizer()
    prepared = sizer.prepare(str(tmp_path))

    entry = prepared.entries[0]
    assert entry["name"] == blocked.name
    assert entry["size_state"] == "unavailable"
    assert entry["logical_bytes"] is None
    assert "errors" not in entry

    result = sizer.scan(prepared)
    assert result["error_count"] == 1
    assert "metadata blocked for test" in result["errors"][0]


def test_allocation_falls_back_to_logical_size_without_block_metadata():
    allocated, fallback = sizing._allocated_bytes(SimpleNamespace(st_size=123))
    assert allocated == 123
    assert fallback is True


def test_result_counts_all_errors_but_caps_error_details(tmp_path, monkeypatch):
    for index in range(3):
        (tmp_path / f"blocked-{index}").mkdir()
    sizer = DirectorySizer(error_limit=2)
    prepared = sizer.prepare(str(tmp_path))
    real_scandir = sizing.os.scandir

    def blocked_children(path):
        if Path(path).parent == tmp_path:
            raise PermissionError(f"blocked {Path(path).name}")
        return real_scandir(path)

    monkeypatch.setattr(sizing.os, "scandir", blocked_children)
    result = sizer.scan(prepared)

    assert result["error_count"] == 3
    assert len(result["errors"]) == 2


def test_result_exposes_allocation_fallback_count(tmp_path, monkeypatch):
    (tmp_path / "file.bin").write_bytes(b"content")
    monkeypatch.setattr(
        sizing,
        "_allocated_bytes",
        lambda metadata: (int(metadata.st_size), True),
    )

    result = DirectorySizer().scan(DirectorySizer().prepare(str(tmp_path)))

    assert result["entries"][0]["allocation_fallback_count"] == 1
    assert result["allocation_fallback_count"] == 1


def test_prepare_returns_canonical_current_and_parent_paths(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    prepared = DirectorySizer().prepare(str(source / ".." / "source"))

    assert prepared.root == source.resolve()
    assert prepared.parent_path == str(tmp_path.resolve())


def test_pre_cancelled_scan_stops_before_traversal(tmp_path):
    (tmp_path / "file.bin").write_bytes(b"data")
    prepared = DirectorySizer().prepare(str(tmp_path))
    cancelled = Event()
    cancelled.set()
    with pytest.raises(DirectorySizingCancelled):
        DirectorySizer().scan(prepared, cancelled)


def test_scan_cancels_cooperatively_during_directory_entries(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    for index in range(510):
        (child / f"file-{index}").touch()
    prepared = DirectorySizer().prepare(str(tmp_path))
    cancelled = Event()

    def cancel_after_batch(files, directories, completed, total):
        if files >= sizing.PROGRESS_FILE_BATCH_SIZE:
            cancelled.set()

    with pytest.raises(DirectorySizingCancelled):
        DirectorySizer().scan(prepared, cancelled, cancel_after_batch)
