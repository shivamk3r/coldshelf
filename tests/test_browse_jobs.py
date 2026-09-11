"""Tests for cancellable ephemeral Browse jobs."""

import time
from threading import Event

import pytest

from coldshelf.directory_sizer import (
    DirectorySizer,
    DirectorySizingCancelled,
)
from coldshelf.web.browse_jobs import BrowseJobManager, BrowseJobNotFound
from coldshelf.web.io_queue import HeavyIOQueue


def _wait_for_status(manager, scan_id, statuses, attempts=200):
    for _ in range(attempts):
        job = manager.get_job(scan_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"browse scan did not reach {statuses}")


class BlockingSizer(DirectorySizer):
    def scan(self, prepared, cancel_event=None, progress_callback=None):
        if progress_callback:
            progress_callback(0, 0, 0, 1)
        while cancel_event is not None and not cancel_event.wait(0.01):
            pass
        raise DirectorySizingCancelled("cancelled for test")


def test_cancels_running_and_queued_browse_jobs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    queue = HeavyIOQueue()
    manager = BrowseJobManager(queue, lambda: [], sizer=BlockingSizer())
    try:
        running_id = manager.submit(str(first))["scan"]["id"]
        _wait_for_status(manager, running_id, {"running"})
        queued_id = manager.submit(str(second))["scan"]["id"]
        assert manager.get_job(queued_id)["status"] == "queued"

        queued = manager.cancel(queued_id)
        assert queued["status"] == "cancelled"
        assert queued_id not in manager._prepared
        manager.cancel(running_id)
        running = _wait_for_status(manager, running_id, {"cancelled"})
        assert running["cancel_requested"] is True
    finally:
        queue.shutdown()


def test_retains_only_the_configured_number_of_completed_jobs(tmp_path):
    folders = []
    for index in range(3):
        folder = tmp_path / str(index)
        folder.mkdir()
        folders.append(folder)
    queue = HeavyIOQueue()
    manager = BrowseJobManager(queue, lambda: [], max_jobs=2)
    try:
        ids = []
        for folder in folders:
            scan_id = manager.submit(str(folder))["scan"]["id"]
            ids.append(scan_id)
            _wait_for_status(manager, scan_id, {"completed"})
        with pytest.raises(BrowseJobNotFound):
            manager.get_job(ids[0])
        assert manager.get_job(ids[1])["status"] == "completed"
        assert manager.get_job(ids[2])["status"] == "completed"
    finally:
        queue.shutdown()


def test_retention_cap_cancels_and_evicts_the_oldest_nonterminal_job(tmp_path):
    folders = []
    for index in range(3):
        folder = tmp_path / str(index)
        folder.mkdir()
        folders.append(folder)
    queue = HeavyIOQueue()
    manager = BrowseJobManager(queue, lambda: [], max_jobs=2, sizer=BlockingSizer())
    try:
        first_id = manager.submit(str(folders[0]))["scan"]["id"]
        _wait_for_status(manager, first_id, {"running"})
        second_id = manager.submit(str(folders[1]))["scan"]["id"]
        third_id = manager.submit(str(folders[2]))["scan"]["id"]

        with pytest.raises(BrowseJobNotFound):
            manager.get_job(first_id)
        assert manager.get_job(second_id)["status"] == "queued"
        assert manager.get_job(third_id)["status"] == "queued"
        manager.cancel(second_id)
        manager.cancel(third_id)
    finally:
        queue.shutdown()


def test_browse_jobs_wait_behind_existing_work_on_the_shared_queue(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    queue = HeavyIOQueue()
    blocker_started = Event()
    release_blocker = Event()

    def block_queue():
        blocker_started.set()
        release_blocker.wait()

    queue.submit(block_queue)
    assert blocker_started.wait(1)
    manager = BrowseJobManager(queue, lambda: [])
    try:
        scan_id = manager.submit(str(source))["scan"]["id"]
        assert manager.get_job(scan_id)["status"] == "queued"
        release_blocker.set()
        assert _wait_for_status(manager, scan_id, {"completed"})["status"] == "completed"
    finally:
        release_blocker.set()
        queue.shutdown()
