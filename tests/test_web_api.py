"""Tests for the FastAPI web interface."""

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from disk_space_manager.web.app import create_app
from disk_space_manager.web.repository import WebRepository
from disk_space_manager.protection.repository import ProtectionRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_old_large_file(path, size_bytes=1024 * 1024 + 1, age_days=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)
    old_time = time.time() - age_days * 24 * 60 * 60
    os.utime(path, (old_time, old_time))


def _wait_for_job(client, token, job_id):
    for _ in range(100):
        response = client.get(f"/api/jobs/{job_id}", headers=_auth(token))
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_requires_token(tmp_path):
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", headers=_auth("secret")).status_code == 200


def test_directory_browser_lists_only_directories_and_can_show_hidden(tmp_path):
    source = tmp_path / "folders"
    source.mkdir()
    (source / "Visible").mkdir()
    (source / ".hidden").mkdir()
    (source / "ordinary.txt").write_text("not a folder")
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    response = client.get(
        "/api/filesystem/directories",
        params={"path": str(source)},
        headers=_auth("secret"),
    )
    assert response.status_code == 200, response.text
    listing = response.json()
    assert listing["current_path"] == str(source.resolve())
    assert listing["parent_path"] == str(source.parent.resolve())
    assert {item["name"] for item in listing["directories"]} == {"Visible"}
    assert {item["kind"] for item in listing["roots"]} >= {"home", "root"}

    with_hidden = client.get(
        "/api/filesystem/directories",
        params={"path": str(source), "show_hidden": "true"},
        headers=_auth("secret"),
    )
    assert {item["name"] for item in with_hidden.json()["directories"]} == {
        ".hidden",
        "Visible",
    }


def test_directory_browser_reports_missing_and_unreadable_paths(tmp_path, monkeypatch):
    source = tmp_path / "unreadable"
    source.mkdir()
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    missing = client.get(
        "/api/filesystem/directories",
        params={"path": str(tmp_path / "missing")},
        headers=_auth("secret"),
    )
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["detail"]

    real_access = os.access
    monkeypatch.setattr(
        "disk_space_manager.web.services.os.access",
        lambda path, mode: False
        if Path(path).resolve() == source.resolve()
        else real_access(path, mode),
    )
    unreadable = client.get(
        "/api/filesystem/directories",
        params={"path": str(source)},
        headers=_auth("secret"),
    )
    assert unreadable.status_code == 400
    assert "not readable" in unreadable.json()["detail"]


def test_archive_target_discovery_returns_space_and_recommendation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "disk_space_manager.web.services._detected_archive_drives",
        lambda: [
            {
                "name": "Backup",
                "path": "/Volumes/Backup",
                "available_space": 123456,
                "device": "/dev/disk9",
                "fs_type": "ExFAT",
            }
        ],
    )
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    assert client.get("/api/archive-targets").status_code == 401
    response = client.get("/api/archive-targets", headers=_auth("secret"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended_path"] == "/Volumes/Backup"
    assert payload["drives"] == [
        {
            "name": "Backup",
            "path": "/Volumes/Backup",
            "available_space": 123456,
            "device": "/dev/disk9",
            "filesystem": "ExFAT",
        }
    ]


def test_protection_preflight_explains_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr("disk_space_manager.web.app.platform_module.system", lambda: "Linux")
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    response = client.get("/api/protection/preflight", headers=_auth("secret"))
    assert response.status_code == 200
    assert response.json() == {
        "platform": "Linux",
        "supported": False,
        "ready": False,
        "minimum_restic_version": "0.19.1",
        "restic_version": None,
        "message": "Protected folders currently require macOS.",
    }


def test_report_job_and_dry_run_clean_action(tmp_path, monkeypatch):
    monkeypatch.setattr("disk_space_manager.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    source = tmp_path / "source"
    source.mkdir()
    (source / "cache.tmp").write_text("cache")
    _create_old_large_file(source / "old.bin")

    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    response = client.post(
        "/api/jobs",
        headers=_auth("secret"),
        json={
            "path": str(source),
            "age_months": 1,
            "include_duplicates": False,
            "include_near_duplicates": False,
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]

    job = _wait_for_job(client, "secret", job_id)
    assert job["status"] == "completed"

    report_response = client.get(f"/api/jobs/{job_id}/report", headers=_auth("secret"))
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["candidate_counts"]["cache"] >= 1
    assert report["candidate_counts"]["old"] == 1

    candidate_id = next(
        candidate["id"]
        for candidate in report["cache_candidates"]
        if candidate["path"].endswith("cache.tmp")
    )
    blocked = client.post(
        f"/api/jobs/{job_id}/actions/clean",
        headers=_auth("secret"),
        json={"candidate_ids": [candidate_id], "dry_run": False},
    )
    assert blocked.status_code == 400

    dry_run = client.post(
        f"/api/jobs/{job_id}/actions/clean",
        headers=_auth("secret"),
        json={"candidate_ids": [candidate_id], "dry_run": True},
    )
    assert dry_run.status_code == 200, dry_run.text
    result = dry_run.json()
    assert result["result"]["deleted"] == 1
    assert result["target_path"] is None
    assert (source / "cache.tmp").exists()

    history = client.get(
        f"/api/jobs/{job_id}/actions", headers=_auth("secret")
    )
    assert history.status_code == 200
    assert history.json()["actions"] == [result]


def test_dry_run_archive_action_uses_candidate_ids(tmp_path, monkeypatch):
    monkeypatch.setattr("disk_space_manager.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    source = tmp_path / "source"
    archive_target = tmp_path / "archive"
    source.mkdir()
    _create_old_large_file(source / "old.bin")

    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret")
    client = TestClient(app)

    response = client.post(
        "/api/jobs",
        headers=_auth("secret"),
        json={
            "path": str(source),
            "age_months": 1,
            "include_duplicates": False,
            "include_near_duplicates": False,
        },
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    assert _wait_for_job(client, "secret", job_id)["status"] == "completed"

    report = client.get(f"/api/jobs/{job_id}/report", headers=_auth("secret")).json()
    old_id = report["old_candidates"][0]["id"]
    response = client.post(
        f"/api/jobs/{job_id}/actions/archive",
        headers=_auth("secret"),
        json={
            "candidate_ids": [old_id],
            "dry_run": True,
            "target_path": str(archive_target),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["moved"] == 1
    assert response.json()["target_path"] == str(archive_target)
    assert (source / "old.bin").exists()
    assert not archive_target.exists()

    history = client.get(
        f"/api/jobs/{job_id}/actions", headers=_auth("secret")
    ).json()["actions"]
    assert history[0]["target_path"] == str(archive_target)
    assert history[0]["dry_run"] is True


def test_web_legacy_action_rejects_protected_candidate(tmp_path):
    source = tmp_path / "protected"
    source.mkdir()
    (source / "cache.tmp").write_text("protected")
    web_repository = WebRepository(tmp_path / "web.sqlite3")
    protection_repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    protection_repository.create_folder("folder", "Protected", source, 2)
    app = create_app(
        repository=web_repository,
        protection_repository=protection_repository,
        token="secret",
    )
    client = TestClient(app)

    response = client.post(
        "/api/jobs",
        headers=_auth("secret"),
        json={
            "path": str(source),
            "age_months": 1,
            "include_duplicates": False,
            "include_near_duplicates": False,
        },
    )
    job_id = response.json()["id"]
    assert _wait_for_job(client, "secret", job_id)["status"] == "completed"
    report = client.get(
        f"/api/jobs/{job_id}/report", headers=_auth("secret")
    ).json()
    candidate_id = report["cache_candidates"][0]["id"]
    blocked = client.post(
        f"/api/jobs/{job_id}/actions/clean",
        headers=_auth("secret"),
        json={"candidate_ids": [candidate_id], "dry_run": True},
    )
    assert blocked.status_code == 400
    assert (source / "cache.tmp").exists()
