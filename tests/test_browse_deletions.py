"""Safety and lifecycle tests for delayed Browse deletion."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from coldshelf.protection.repository import ProtectionRepository
from coldshelf.web.app import create_app
from coldshelf.web.repository import WebRepository


def _auth(token="secret"):
    return {"Authorization": f"Bearer {token}"}


def _roots(root: Path):
    return lambda: [
        {
            "name": "Test Home",
            "path": str(root),
            "kind": "home",
            "readable": True,
            "symlink": False,
        }
    ]


def _wait_for_scan(client, scan_id, token="secret"):
    for _ in range(200):
        response = client.get(f"/api/browse/scans/{scan_id}", headers=_auth(token))
        assert response.status_code == 200
        scan = response.json()
        if scan["status"] in {"completed", "failed", "cancelled"}:
            return scan
        time.sleep(0.01)
    raise AssertionError("Browse scan did not finish")


def _scan(client, source: Path, token="secret"):
    created = client.post(
        "/api/browse/scans",
        headers=_auth(token),
        json={"path": str(source)},
    )
    assert created.status_code == 200, created.text
    scan = _wait_for_scan(client, created.json()["scan"]["id"], token)
    assert scan["status"] == "completed", scan
    result = client.get(
        f"/api/browse/scans/{scan['id']}/result", headers=_auth(token)
    )
    assert result.status_code == 200, result.text
    return scan, result.json()


def _preview(client, scan_id, paths, token="secret"):
    response = client.post(
        "/api/browse/deletion-previews",
        headers=_auth(token),
        json={"scan_id": scan_id, "paths": [str(path) for path in paths]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_deletion(client, deletion_id, token="secret"):
    for _ in range(300):
        response = client.get(
            f"/api/browse/deletions/{deletion_id}", headers=_auth(token)
        )
        assert response.status_code == 200, response.text
        deletion = response.json()
        if deletion["status"] in {"completed", "failed", "cancelled"}:
            return deletion
        time.sleep(0.01)
    raise AssertionError("Browse deletion did not finish")


class _FakeClock:
    def __init__(self):
        self.now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _app(
    tmp_path,
    source,
    *,
    delay=0.15,
    repository=None,
    protection=None,
    clock=None,
    preview_ttl=300,
):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    repository = repository or WebRepository(state / "web.sqlite3")
    protection = protection or ProtectionRepository(state / "protected.sqlite3")
    app = create_app(
        repository=repository,
        protection_repository=protection,
        token="secret",
        filesystem_roots_provider=_roots(source),
        browse_default_path=source,
        browse_delete_delay_seconds=delay,
        browse_delete_preview_ttl_seconds=preview_ttl,
    )
    if clock is not None:
        app.state.deletion_manager.clock = clock
    return app


def test_preview_schedule_and_undo_leave_items_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log"
    )
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "notes.txt"
    selected.write_text("keep me")

    with TestClient(_app(tmp_path, source, delay=0.2)) as client:
        scan, result = _scan(client, source)
        entry = next(item for item in result["entries"] if item["path"] == str(selected))
        assert entry["deletion_eligible"] is True

        unauthorized = client.post(
            "/api/browse/deletion-previews",
            json={"scan_id": scan["id"], "paths": [str(selected)]},
        )
        assert unauthorized.status_code == 401

        preview = _preview(client, scan["id"], [selected])
        assert preview["can_schedule"] is True
        assert preview["selected_count"] == 1
        assert selected.exists()

        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": preview["id"]},
        )
        assert scheduled.status_code == 200, scheduled.text
        deletion = scheduled.json()
        assert deletion["status"] == "scheduled"
        assert deletion["undo_available"] is True
        assert client.get(
            "/api/browse/deletions/active", headers=_auth()
        ).json()["deletion"]["id"] == deletion["id"]
        assert client.get("/api/browse/deletions/active").status_code == 401
        assert client.get(
            f"/api/browse/deletions/{deletion['id']}"
        ).status_code == 401
        assert client.post(
            f"/api/browse/deletions/{deletion['id']}/undo"
        ).status_code == 401

        undone = client.post(
            f"/api/browse/deletions/{deletion['id']}/undo", headers=_auth()
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["status"] == "cancelled"
        time.sleep(0.25)
        assert selected.read_text() == "keep me"
        assert client.get(
            "/api/browse/deletions/active", headers=_auth()
        ).json() == {"deletion": None}

    log = (tmp_path / "actions.log").read_text()
    assert "DELETE_PREVIEW" in log
    assert "DELETE_SCHEDULED" in log
    assert "DELETE_CANCELLED" in log
    assert "DRY-RUN" in log


def test_deletes_regular_file_and_folder_without_following_nested_symlink(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log"
    )
    source = tmp_path / "source"
    source.mkdir()
    selected_file = source / "old.txt"
    selected_file.write_text("old")
    selected_folder = source / "project"
    selected_folder.mkdir()
    (selected_folder / "nested.txt").write_text("nested")
    outside = tmp_path / "outside.txt"
    outside.write_text("must remain")
    (selected_folder / "outside-link").symlink_to(outside)

    with TestClient(_app(tmp_path, source, delay=0.03)) as client:
        scan, _ = _scan(client, source)
        preview = _preview(client, scan["id"], [selected_file, selected_folder])
        response = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": preview["id"]},
        )
        assert response.status_code == 200, response.text
        completed = _wait_for_deletion(client, response.json()["id"])

    assert completed["status"] == "completed"
    assert {item["status"] for item in completed["items"]} == {"deleted"}
    assert not selected_file.exists()
    assert not selected_folder.exists()
    assert outside.read_text() == "must remain"
    assert "DELETE" in (tmp_path / "actions.log").read_text()


def test_changed_item_invalidates_preview_and_only_one_batch_can_be_active(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.txt"
    second = source / "second.txt"
    first.write_text("first")
    second.write_text("second")

    with TestClient(_app(tmp_path, source, delay=0.3)) as client:
        scan, _ = _scan(client, source)
        stale = _preview(client, scan["id"], [first])
        first.write_text("changed after preview")
        rejected = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": stale["id"]},
        )
        assert rejected.status_code == 409
        consumed = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": stale["id"]},
        )
        assert consumed.status_code == 404

        scan, _ = _scan(client, source)
        first_preview = _preview(client, scan["id"], [first])
        second_preview = _preview(client, scan["id"], [second])
        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": first_preview["id"]},
        )
        assert scheduled.status_code == 200
        blocked = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": second_preview["id"]},
        )
        assert blocked.status_code == 409
        client.post(
            f"/api/browse/deletions/{scheduled.json()['id']}/undo", headers=_auth()
        )


def test_blocks_roots_symlinks_and_protected_overlaps(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    protected = source / "protected"
    protected.mkdir()
    (protected / "file.txt").write_text("protected")
    shortcut = source / "shortcut"
    shortcut.symlink_to(protected, target_is_directory=True)
    state = tmp_path / "state"
    state.mkdir()
    protection = ProtectionRepository(state / "protected.sqlite3")
    protection.create_folder("protected-1", "Protected", protected, 2)
    repository = WebRepository(state / "web.sqlite3")

    with TestClient(
        _app(
            tmp_path,
            source,
            repository=repository,
            protection=protection,
        )
    ) as client:
        scan, result = _scan(client, source)
        entries = {item["name"]: item for item in result["entries"]}
        assert entries["protected"]["deletion_eligible"] is False
        assert "Protected" in entries["protected"]["deletion_blocker"]
        assert entries["shortcut"]["deletion_eligible"] is False
        assert "Symbolic" in entries["shortcut"]["deletion_blocker"]
        preview = _preview(client, scan["id"], [protected, shortcut])
        assert preview["can_schedule"] is False
        assert len(preview["blockers"]) == 2

        parent_scan, parent_result = _scan(client, tmp_path)
        root_entry = next(
            item for item in parent_result["entries"] if item["path"] == str(source)
        )
        assert root_entry["deletion_eligible"] is False
        root_preview = _preview(client, parent_scan["id"], [source])
        assert root_preview["can_schedule"] is False


def test_blocks_active_application_state_and_non_child_paths(tmp_path):
    source = tmp_path
    state = tmp_path / "state"
    state.mkdir()
    repository = WebRepository(state / "web.sqlite3")
    protection = ProtectionRepository(state / "protected.sqlite3")
    nested = tmp_path / "ordinary" / "nested.txt"
    nested.parent.mkdir()
    nested.write_text("nested")

    with TestClient(
        _app(
            tmp_path,
            source,
            repository=repository,
            protection=protection,
        )
    ) as client:
        scan, result = _scan(client, source)
        state_entry = next(item for item in result["entries"] if item["path"] == str(state))
        assert state_entry["deletion_eligible"] is False
        assert "coldshelf state" in state_entry["deletion_blocker"].lower()

        preview = _preview(client, scan["id"], [state, nested])
        assert preview["can_schedule"] is False
        assert len(preview["blockers"]) == 2


def test_expired_preview_is_rejected_and_deadline_closes_undo(tmp_path):
    clock = _FakeClock()
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "deadline.txt"
    selected.write_text("deadline")

    with TestClient(
        _app(tmp_path, source, delay=10, clock=clock, preview_ttl=5)
    ) as client:
        scan, _ = _scan(client, source)
        expired = _preview(client, scan["id"], [selected])
        clock.advance(6)
        response = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": expired["id"]},
        )
        assert response.status_code == 404

        fresh = _preview(client, scan["id"], [selected])
        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": fresh["id"]},
        )
        assert scheduled.status_code == 200
        deletion_id = scheduled.json()["id"]
        clock.advance(10)
        undo = client.post(
            f"/api/browse/deletions/{deletion_id}/undo", headers=_auth()
        )
        assert undo.status_code == 409
        completed = _wait_for_deletion(client, deletion_id)
        assert completed["items"][0]["status"] == "deleted"
    assert not selected.exists()


def test_changed_item_is_skipped_independently_at_execution(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    changed = source / "changed.txt"
    unchanged = source / "unchanged.txt"
    changed.write_text("before")
    unchanged.write_text("delete")

    with TestClient(_app(tmp_path, source, delay=0.08)) as client:
        scan, _ = _scan(client, source)
        preview = _preview(client, scan["id"], [changed, unchanged])
        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": preview["id"]},
        )
        assert scheduled.status_code == 200
        changed.write_text("changed during undo window")
        completed = _wait_for_deletion(client, scheduled.json()["id"])

    outcomes = {item["name"]: item["status"] for item in completed["items"]}
    assert completed["status"] == "completed"
    assert outcomes == {"changed.txt": "skipped", "unchanged.txt": "deleted"}
    assert changed.read_text() == "changed during undo window"
    assert not unchanged.exists()


def test_overdue_scheduled_batch_resumes_after_server_restart(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "resume.txt"
    selected.write_text("resume")
    state = tmp_path / "state"
    state.mkdir()
    repository = WebRepository(state / "web.sqlite3")
    protection = ProtectionRepository(state / "protected.sqlite3")
    deletion_id = ""

    first_app = _app(
        tmp_path,
        source,
        delay=0.08,
        repository=repository,
        protection=protection,
    )
    with TestClient(first_app) as client:
        scan, _ = _scan(client, source)
        preview = _preview(client, scan["id"], [selected])
        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": preview["id"]},
        )
        assert scheduled.status_code == 200
        deletion_id = scheduled.json()["id"]

    time.sleep(0.1)
    assert selected.exists()

    second_app = _app(
        tmp_path,
        source,
        delay=0.08,
        repository=repository,
        protection=protection,
    )
    with TestClient(second_app) as client:
        completed = _wait_for_deletion(client, deletion_id)
        assert completed["status"] == "completed"
    assert not selected.exists()


def test_interrupted_running_batch_is_requeued_after_server_restart(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "running.txt"
    selected.write_text("running")
    state = tmp_path / "state"
    state.mkdir()
    repository = WebRepository(state / "web.sqlite3")
    protection = ProtectionRepository(state / "protected.sqlite3")

    with TestClient(
        _app(
            tmp_path,
            source,
            delay=60,
            repository=repository,
            protection=protection,
        )
    ) as client:
        scan, _ = _scan(client, source)
        preview = _preview(client, scan["id"], [selected])
        scheduled = client.post(
            "/api/browse/deletions",
            headers=_auth(),
            json={"preview_id": preview["id"]},
        )
        assert scheduled.status_code == 200
        deletion_id = scheduled.json()["id"]

    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    assert repository.claim_due_browse_deletion(deletion_id, future)
    assert repository.mark_browse_deletion_running(deletion_id, future)

    with TestClient(
        _app(
            tmp_path,
            source,
            repository=repository,
            protection=protection,
        )
    ) as client:
        completed = _wait_for_deletion(client, deletion_id)
        assert completed["status"] == "completed"
        assert completed["items"][0]["status"] == "deleted"
    assert not selected.exists()
