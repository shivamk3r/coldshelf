"""Docker storage checks use an in-memory Engine transport, never a real daemon."""

import copy
import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from coldshelf.cli import cli
from coldshelf.docker_storage import DockerConflict, DockerStorage, DockerUnavailable, _socket_path
from coldshelf.web.app import create_app
from coldshelf.web.repository import WebRepository


class FakeDocker(DockerStorage):
    def __init__(self):
        super().__init__(socket_path="fake", cache_seconds=0)
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        self.payload = {
            "ImageUsage": {"TotalCount": 1, "ActiveCount": 0, "TotalSize": 200, "Reclaimable": 90,
                           "Items": [{"Id": "sha256:abc", "RepoTags": ["demo:latest"], "Size": 200,
                                      "SharedSize": 110, "Containers": 0}]},
            "ContainerUsage": {"TotalCount": 1, "TotalSize": 40,
                               "Items": [{"Id": "container-1", "Names": ["demo"], "Image": "demo:latest",
                                          "State": "exited", "SizeRw": 40}]},
            "VolumeUsage": {"TotalCount": 2, "ActiveCount": 1, "TotalSize": 70,
                            "Items": [{"Name": "loose", "Driver": "local", "UsageData": {"Size": 30, "RefCount": 0}},
                                      {"Name": "linked", "Driver": "local", "UsageData": {"Size": 40, "RefCount": 1}}]},
            "BuildCacheUsage": {"TotalCount": 1, "TotalSize": 60, "Reclaimable": 60,
                                "Items": [{"ID": "cache-1", "Size": 60, "InUse": False,
                                           "LastUsedAt": old}]},
        }
        self.calls = []
        self.prune_result = {"CachesDeleted": ["cache-1"], "SpaceReclaimed": 55}

    def _request(self, method, path):
        self.calls.append((method, path))
        if path == "/version":
            return {"ApiVersion": "1.52"}
        if path == "/v1.52/info":
            return {"ID": "daemon-1", "DockerRootDir": "/unavailable/docker"}
        if path == "/v1.52/system/df?verbose=1":
            return copy.deepcopy(self.payload)
        if method == "DELETE":
            return {}
        if method == "POST" and path.startswith("/v1.52/build/prune?"):
            filters = json.loads(parse_qs(urlsplit(path).query)["filters"][0])
            if len(filters.get("id", [])) != 1:
                raise DockerConflict("filters expect only one value")
            return copy.deepcopy(self.prune_result)
        raise AssertionError((method, path))


class LegacyDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        modern = self.payload
        self.payload = {
            "LayersSize": 190,
            "Images": modern["ImageUsage"]["Items"],
            "Containers": modern["ContainerUsage"]["Items"],
            "Volumes": modern["VolumeUsage"]["Items"],
            "BuildCache": modern["BuildCacheUsage"]["Items"],
        }

    def _request(self, method, path):
        if path == "/version":
            return {"ApiVersion": "1.51"}
        if path.startswith("/v1.51/"):
            path = path.replace("/v1.51/", "/v1.52/", 1)
        return super()._request(method, path)


def test_docker_usage_and_exact_removals():
    adapter = FakeDocker()
    usage = adapter.public_usage()
    assert usage["groups"]["images"]["size_bytes"] == 200
    assert usage["groups"]["images"]["reclaimable_bytes"] == 90
    assert usage["groups"]["volumes"]["reclaimable_bytes"] == 30
    assert adapter.list_items("volumes")["items"][0]["eligible"] is False

    with pytest.raises(DockerConflict):
        adapter.preview("volumes", ["linked"])
    volume = adapter.preview("volumes", ["loose"])
    assert adapter.execute(volume)[0]["status"] == "deleted"
    assert ("DELETE", "/v1.52/volumes/loose?force=false") in adapter.calls

    container = adapter.preview("containers", ["container-1"])
    adapter.execute(container)
    assert ("DELETE", "/v1.52/containers/container-1?force=false&v=false") in adapter.calls

    image = adapter.preview("images", ["sha256:abc"])
    adapter.execute(image)
    assert ("DELETE", "/v1.52/images/sha256:abc?force=false&noprune=true") in adapter.calls


def test_legacy_disk_usage_fields_are_normalized():
    usage = LegacyDocker().public_usage()
    assert usage["groups"]["images"]["size_bytes"] == 190
    assert usage["groups"]["images"]["reclaimable_bytes"] == 90
    assert usage["groups"]["containers"]["size_bytes"] == 40


def test_build_cache_fixed_cutoff_and_changed_selection():
    adapter = FakeDocker()
    preview = adapter.preview("cache", [], 30)
    assert preview["ids"] == ["cache-1"]
    adapter.payload["BuildCacheUsage"]["Items"].append({
        "ID": "cache-2", "Size": 10, "InUse": False,
        "LastUsedAt": (datetime.now(timezone.utc) - timedelta(days=50)).isoformat(),
    })
    with pytest.raises(DockerConflict, match="changed"):
        adapter.execute(preview)
    assert not any(method == "POST" for method, _ in adapter.calls)


def test_build_cache_prune_reports_daemon_reclaimed_bytes():
    adapter = FakeDocker()
    preview = adapter.preview("cache", [], 30)
    outcome = adapter.execute(preview)
    assert outcome == [{"id": "build-cache", "status": "deleted", "reclaimed_bytes": 55}]
    prune = next(path for method, path in adapter.calls if method == "POST")
    assert parse_qs(urlsplit(prune).query)["all"] == ["1"]
    filters = json.loads(parse_qs(urlsplit(prune).query)["filters"][0])
    assert filters["id"] == ["^(?:cache-1)$"]
    assert filters["until"][0].endswith("s")


def test_build_cache_prune_matches_only_multiple_reviewed_ids():
    adapter = FakeDocker()
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    adapter.payload["BuildCacheUsage"]["Items"].extend([
        {"ID": "cache.2", "Size": 20, "InUse": False, "LastUsedAt": old},
        {"ID": "cache-1-extra", "Size": 20, "InUse": True, "LastUsedAt": old},
    ])
    preview = adapter.preview("cache", [], 7)
    assert preview["ids"] == ["cache-1", "cache.2"]
    assert adapter.execute(preview)[0]["status"] == "deleted"
    prune = next(path for method, path in adapter.calls if method == "POST")
    filters = json.loads(parse_qs(urlsplit(prune).query)["filters"][0])
    assert len(filters["id"]) == 1
    pattern = filters["id"][0]
    assert all(re.fullmatch(pattern, item_id) for item_id in preview["ids"])
    assert not re.fullmatch(pattern, "cache-1-extra")
    assert not re.fullmatch(pattern, "cacheX2")
    assert not re.fullmatch(pattern, "prefix-cache-1")


def test_build_cache_noop_is_not_reported_as_removed():
    adapter = FakeDocker()
    adapter.prune_result = {"CachesDeleted": [], "SpaceReclaimed": 0}
    outcome = adapter.execute(adapter.preview("cache", [], 7))
    assert outcome == [{"id": "build-cache", "status": "skipped", "reclaimed_bytes": 0,
                        "error": "Docker found no removable cache among the reviewed items."}]


def test_missing_reference_counts_fail_closed_and_changed_item_is_skipped():
    adapter = FakeDocker()
    image = adapter.preview("images", ["sha256:abc"])
    adapter.payload["ImageUsage"]["Items"][0]["Containers"] = 1
    assert adapter.execute(image) == [{"id": "sha256:abc", "status": "skipped", "error": "Item changed after preview"}]
    assert not any(method == "DELETE" for method, _ in adapter.calls)
    adapter.payload["ImageUsage"]["Items"][0].pop("Containers")
    adapter.payload["VolumeUsage"]["Items"][0]["UsageData"].pop("RefCount")
    adapter.invalidate()
    assert not adapter.list_items("images")["items"][0]["eligible"]
    assert not next(item for item in adapter.list_items("volumes")["items"] if item["id"] == "loose")["eligible"]


def test_all_eligible_never_applies_to_volumes():
    adapter = FakeDocker()
    with pytest.raises(DockerConflict, match="Volumes require exact names"):
        adapter.preview("volumes", [], all_eligible=True)
    assert adapter.preview("containers", [], all_eligible=True)["ids"] == ["container-1"]


def test_only_local_unix_socket_is_accepted(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote.example")
    with pytest.raises(DockerUnavailable, match="remote"):
        _socket_path()


def test_selected_context_overrides_docker_host_and_cannot_fall_back(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/local.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "production")
    monkeypatch.setattr("coldshelf.docker_storage.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("coldshelf.docker_storage.subprocess.run",
                        lambda command, **kwargs: SimpleNamespace(stdout="ssh://production\n"))
    with pytest.raises(DockerUnavailable, match="remote"):
        _socket_path()


def test_web_preview_requires_auth_phrase_and_can_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    adapter = FakeDocker()
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret",
                     docker_storage=adapter, docker_action_delay_seconds=1)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        assert client.get("/api/docker/usage").status_code == 401
        assert client.get("/api/docker/usage", headers=auth).json()["available"] is True
        preview = client.post("/api/docker/previews", json={"kind": "volumes", "ids": ["loose"]}, headers=auth).json()
        assert preview["confirmation_phrase"] == "DELETE 1 DOCKER VOLUMES"
        assert client.post("/api/docker/actions", json={"preview_id": preview["id"],
                                                    "confirmation_phrase": "wrong"}, headers=auth).status_code == 409
        response = client.post("/api/docker/actions", json={"preview_id": preview["id"],
                                                     "confirmation_phrase": preview["confirmation_phrase"]}, headers=auth)
        assert response.status_code == 200, response.text
        action_id = response.json()["id"]
        assert client.post(f"/api/docker/actions/{action_id}/undo", headers=auth).json()["status"] == "cancelled"
        recent = client.get("/api/docker/actions/recent", headers=auth).json()["actions"]
        assert recent[0]["status"] == "cancelled"
        assert not any(method == "DELETE" for method, _ in adapter.calls)


def test_web_action_persists_and_executes_after_delay(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    adapter = FakeDocker()
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret",
                     docker_storage=adapter, docker_action_delay_seconds=0.05)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        preview = client.post("/api/docker/previews", json={"kind": "containers", "ids": ["container-1"]}, headers=auth).json()
        action = client.post("/api/docker/actions", json={"preview_id": preview["id"],
                                                       "confirmation_phrase": preview["confirmation_phrase"]}, headers=auth).json()
        for _ in range(50):
            result = client.get(f"/api/docker/actions/{action['id']}", headers=auth).json()
            if result["status"] == "completed":
                break
            time.sleep(0.05)
        assert result["status"] == "completed"
        assert result["outcomes"][0]["status"] == "deleted"
        assert (tmp_path / "actions.log").exists()


def test_web_seven_day_cache_cleanup_accepts_multiple_ids(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    adapter = FakeDocker()
    adapter.payload["BuildCacheUsage"]["Items"].append({
        "ID": "cache-2", "Size": 10, "InUse": False,
        "LastUsedAt": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
    })
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret",
                     docker_storage=adapter, docker_action_delay_seconds=0.05)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        preview = client.post("/api/docker/previews", json={"kind": "cache", "older_than_days": 7}, headers=auth).json()
        assert preview["confirmation_phrase"] == "REMOVE 2 DOCKER CACHE"
        action = client.post("/api/docker/actions", json={
            "preview_id": preview["id"], "confirmation_phrase": preview["confirmation_phrase"],
        }, headers=auth).json()
        for _ in range(50):
            result = client.get(f"/api/docker/actions/{action['id']}", headers=auth).json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert result["status"] == "completed", result.get("error")
        assert result["outcomes"][0]["reclaimed_bytes"] == 55
        assert len([call for call in adapter.calls if call[0] == "POST"]) == 1


def test_scheduled_web_action_is_interrupted_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    adapter = FakeDocker()
    repository = WebRepository(tmp_path / "web.sqlite3")
    auth = {"Authorization": "Bearer secret"}
    first = create_app(repository=repository, token="secret", docker_storage=adapter,
                       docker_action_delay_seconds=30)
    with TestClient(first) as client:
        preview = client.post("/api/docker/previews", json={"kind": "volumes", "ids": ["loose"]}, headers=auth).json()
        action = client.post("/api/docker/actions", json={
            "preview_id": preview["id"], "confirmation_phrase": preview["confirmation_phrase"]
        }, headers=auth).json()
        assert action["status"] == "scheduled"
    second = create_app(repository=repository, token="secret", docker_storage=adapter,
                        docker_action_delay_seconds=30)
    with TestClient(second) as client:
        result = client.get(f"/api/docker/actions/{action['id']}", headers=auth).json()
        assert result["status"] == "interrupted"
        assert client.get("/api/docker/actions/active", headers=auth).json()["action"] is None
    assert not any(method == "DELETE" for method, _ in adapter.calls)


def test_cli_dry_run_never_removes_docker_data(monkeypatch, tmp_path):
    adapter = FakeDocker()
    monkeypatch.setattr("coldshelf.docker_cli.DockerStorage", lambda: adapter)
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    result = CliRunner().invoke(cli, ["--dry-run", "docker", "clean", "volumes", "--id", "loose"])
    assert result.exit_code == 0, result.output
    assert "Preview: 1 volumes" in result.output
    assert not any(method == "DELETE" for method, _ in adapter.calls)


def test_cli_requires_exact_phrase_before_removal(monkeypatch, tmp_path):
    adapter = FakeDocker()
    monkeypatch.setattr("coldshelf.docker_cli.DockerStorage", lambda: adapter)
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    args = ["docker", "clean", "volumes", "--id", "loose"]
    rejected = CliRunner().invoke(cli, args, input="DELETE 1 DOCKER IMAGES\n")
    assert rejected.exit_code != 0
    assert not any(method == "DELETE" for method, _ in adapter.calls)
    approved = CliRunner().invoke(cli, args, input="DELETE 1 DOCKER VOLUMES\n")
    assert approved.exit_code == 0, approved.output
    assert any(method == "DELETE" for method, _ in adapter.calls)
