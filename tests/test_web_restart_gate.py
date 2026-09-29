"""Idle checks cover persisted, queued, and synchronous dashboard work."""

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from coldshelf.protection.repository import ProtectionRepository
from coldshelf.web.app import RestartAdmission, create_app
from coldshelf.web.repository import WebRepository
from coldshelf.web.schemas import JobCreateRequest


@pytest.fixture
def dashboard(tmp_path):
    repo = WebRepository(tmp_path / "web.sqlite3")
    protection_repo = ProtectionRepository(tmp_path / "protected.sqlite3")
    app = create_app(
        repository=repo, protection_repository=protection_repo,
        token="test-token", browse_default_path=tmp_path,
    )
    return app, repo, protection_repo, tmp_path


@pytest.mark.parametrize(
    "category,status",
    [
        ("report", "queued"), ("report", "running"),
        ("browse_sizing", "queued"), ("browse_sizing", "running"),
        ("browse_deletion", "scheduled"), ("browse_deletion", "queued"),
        ("browse_deletion", "running"),
        ("docker_cleanup", "scheduled"), ("docker_cleanup", "queued"),
        ("docker_cleanup", "running"),
        ("protection", "queued"), ("protection", "running"),
    ],
)
def test_restart_refuses_each_accepted_work_state(dashboard, category, status):
    app, repo, protection_repo, root = dashboard
    item_id = str(uuid.uuid4())
    future = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    if category == "report":
        repo.create_job(item_id, JobCreateRequest(path=str(root)), root)
        if status == "running":
            repo.mark_job_running(item_id)
    elif category == "browse_sizing":
        app.state.browse_manager._jobs[item_id] = {"status": status}
    elif category == "browse_deletion":
        repo.create_browse_deletion(
            deletion_id=item_id, current_path=str(root), allowed_root=str(root),
            allowed_root_device=1, items=[], created_at=future, delete_after=future,
        )
        if status != "scheduled":
            with repo._write() as conn:
                conn.execute(
                    "UPDATE browse_deletions SET status = ? WHERE id = ?",
                    (status, item_id),
                )
    elif category == "docker_cleanup":
        repo.create_docker_action(
            item_id, {"kind": "containers"}, future, future,
        )
        if status != "scheduled":
            assert repo.transition_docker_action(item_id, "scheduled", status)
    else:
        protection_repo.create_folder(item_id, "Folder", root / "source", 1)
        operation = protection_repo.create_operation(item_id, "index", {})
        item_id = operation["id"]
        if status == "running":
            protection_repo.start_operation(item_id)

    gate = app.state.restart_gate
    assert gate.prepare() is not None
    assert not gate.draining
    if category == "browse_sizing":
        assert app.state.browse_manager._jobs[item_id]["status"] == status
    elif category == "protection":
        assert protection_repo.get_operation(item_id)["status"] == status
    elif category == "report":
        assert repo.get_job(item_id)["status"] == status
    elif category == "browse_deletion":
        assert repo.get_browse_deletion(item_id)["status"] == status
    else:
        assert repo.get_docker_action(item_id)["status"] == status


def test_shared_queue_blocks_restart_until_work_finishes(dashboard):
    app, _, _, _ = dashboard
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        assert release.wait(5)

    future = app.state.io_queue.submit(work)
    try:
        assert started.wait(5)
        assert app.state.restart_gate.prepare() is not None
        assert not app.state.restart_gate.draining
    finally:
        release.set()
        future.result(timeout=5)
        app.state.io_queue.shutdown()
    assert app.state.restart_gate.prepare() is None
    app.state.restart_gate.abort()


@pytest.mark.parametrize("kind", ["clean", "archive"])
def test_inflight_synchronous_actions_block_restart(dashboard, monkeypatch, kind):
    app, _, _, _ = dashboard
    started = threading.Event()
    release = threading.Event()
    responses = []

    def action(_job_id, _request):
        started.set()
        assert release.wait(5)
        return {
            "id": "action", "job_id": "job", "action_type": kind,
            "dry_run": True, "selected_count": 1, "skipped_count": 0,
            "result": {}, "created_at": "now",
        }

    monkeypatch.setattr(app.state.action_service, kind, action)
    with TestClient(app) as client:
        thread = threading.Thread(
            target=lambda: responses.append(client.post(
                f"/api/jobs/job/actions/{kind}",
                headers={"Authorization": "Bearer test-token"},
                json={"candidate_ids": ["candidate"]},
            )),
        )
        thread.start()
        try:
            assert started.wait(5)
            assert app.state.restart_gate.prepare() is not None
            assert not app.state.restart_gate.draining
        finally:
            release.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert responses[0].status_code == 200


def test_operation_arriving_during_idle_check_is_rejected():
    checking = threading.Event()
    finish_check = threading.Event()
    admitted = []

    def check():
        checking.set()
        assert finish_check.wait(5)
        return None

    gate = RestartAdmission(check)
    prepare = threading.Thread(target=lambda: admitted.append(gate.prepare()))
    prepare.start()
    assert checking.wait(5)
    request = threading.Thread(target=lambda: admitted.append(gate.enter()))
    request.start()
    finish_check.set()
    prepare.join(timeout=5)
    request.join(timeout=5)
    assert not prepare.is_alive() and not request.is_alive()
    assert admitted == [None, False]
    assert gate.draining
    gate.abort()
    assert gate.enter()
    gate.leave()


def test_already_admitted_request_wins_idle_check():
    gate = RestartAdmission(lambda: None)
    assert gate.enter()
    assert "request" in gate.prepare()
    assert not gate.draining
    gate.leave()
