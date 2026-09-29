"""Docker storage checks use an in-memory Engine transport, never a real daemon."""

import copy
import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

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
                                "Items": [{"ID": "cache-1", "Size": 60, "InUse": False, "Shared": False,
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
    assert outcome == [{"id": "build-cache", "status": "deleted", "reclaimed_bytes": 55,
                        "deleted_count": 1, "not_removed_count": 0,
                        "deleted_ids": ["cache-1"], "not_removed_ids": []}]
    prune = next(path for method, path in adapter.calls if method == "POST")
    assert parse_qs(urlsplit(prune).query)["all"] == ["1"]
    filters = json.loads(parse_qs(urlsplit(prune).query)["filters"][0])
    assert filters["id"] == ["^(?:cache-1)$"]
    assert "until" not in filters


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
                        "deleted_count": 0, "not_removed_count": 1,
                        "deleted_ids": [], "not_removed_ids": ["cache-1"],
                        "error": "Docker found no removable cache among the reviewed items."}]


def cache_record(item_id, *, days=100, size=60, parents=(), shared=False, in_use=False):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"ID": item_id, "Size": size, "InUse": in_use, "Shared": shared,
            " Parents": list(parents), "CreatedAt": old, "LastUsedAt": old, "UsageCount": 1}


class DependencyDocker(FakeDocker):
    """Model Engine 29's leaf pruning and parent timestamp refresh on release.

    Like BuildKit DiskUsage, the fixture reports idle parents as InUse=false
    even while an idle child still holds them. A duration filter is evaluated
    again for each newly released parent, reproducing the real failure.
    """

    def __init__(self):
        super().__init__()
        self.payload["BuildCacheUsage"]["Items"] = [
            cache_record("root", size=100),
            cache_record("middle", size=50, parents=["root"]),
            cache_record("leaf", size=25, parents=["middle"]),
            cache_record("protected-root", size=1000),
            cache_record("protected-parent", size=2000, parents=["protected-root"]),
            cache_record("recent", size=3000, days=2, parents=["protected-parent"]),
            cache_record("shared", size=500, shared=True),
        ]

    def _request(self, method, path):
        if method != "POST":
            return super()._request(method, path)
        self.calls.append((method, path))
        query = parse_qs(urlsplit(path).query)
        assert query["all"] == ["1"]
        filters = json.loads(query["filters"][0])
        assert len(filters["id"]) == 1
        duration = float(filters.get("until", ["0s"])[0][:-1])
        rows = self.payload["BuildCacheUsage"]["Items"]
        deleted = []
        reclaimed = 0
        while True:
            parents = {parent for row in rows for parent in row.get(" Parents", [])}
            leaves = [row for row in rows if row["ID"] not in parents and not row["InUse"] and
                      re.fullmatch(filters["id"][0], row["ID"]) and
                      (not duration or datetime.fromisoformat(row["LastUsedAt"]).timestamp() <
                       datetime.now(timezone.utc).timestamp() - duration)]
            if not leaves:
                break
            for row in leaves:
                rows.remove(row)
                deleted.append(row["ID"])
                reclaimed += row["Size"]
                for parent in rows:
                    if parent["ID"] in row.get(" Parents", []):
                        parent["LastUsedAt"] = datetime.now(timezone.utc).isoformat()
                        parent["UsageCount"] += 1
        return {"CachesDeleted": deleted, "SpaceReclaimed": reclaimed}


def test_prune_finishes_reviewed_chains_despite_parent_age_refresh():
    adapter = DependencyDocker()
    preview = adapter.preview("cache", [], 7)
    assert preview["ids"] == ["leaf", "middle", "root", "shared"]
    assert preview["estimated_bytes"] == 175
    assert preview["cache_summary"] == {
        "matching_count": 6, "matching_bytes": 3675, "removable_count": 4,
        "removable_bytes": 675, "private_bytes": 175, "shared_bytes": 500,
        "unknown_shared_bytes": 0, "retained_count": 2, "retained_bytes": 3000,
    }
    outcomes = []
    result = adapter.execute(preview, on_outcome=outcomes.append)[0]
    assert outcomes == [result]
    assert result["deleted_ids"] == preview["ids"]
    assert result["deleted_count"] == 4
    assert result["not_removed_count"] == 0
    assert result["reclaimed_bytes"] == 675  # Daemon accounting, not private-byte estimate.
    assert {row["ID"] for row in adapter.payload["BuildCacheUsage"]["Items"]} == {
        "protected-root", "protected-parent", "recent",
    }
    refreshed = adapter.preview("cache", [], 7)
    assert refreshed["items"] == []
    assert refreshed["estimated_bytes"] == 0
    assert refreshed["cache_summary"]["retained_count"] == 2


def test_duration_filter_reproduces_stranded_parent_chain():
    adapter = DependencyDocker()
    result = adapter._api("POST", "/build/prune?" + urlencode({
        "all": "1", "filters": json.dumps({"id": ["^(?:root|middle|leaf|shared)$"], "until": ["604800s"]}),
    }))
    assert set(result["CachesDeleted"]) == {"leaf", "shared"}
    # The refreshed middle now pins root, even though both were reviewed as old.
    preview = adapter.preview("cache", [], 7)
    assert preview["items"] == []
    assert preview["cache_summary"]["retained_count"] == 3


@pytest.mark.parametrize("parent_field", [" Parents", "Parents", "Parent"])
def test_parent_wire_formats_and_transitive_retention(parent_field):
    adapter = FakeDocker()
    rows = [cache_record("root"), cache_record("parent"), cache_record("new", days=1)]
    for row, parent in zip(rows[1:], ["root", "parent"]):
        row.pop(" Parents")
        row[parent_field] = parent if parent_field == "Parent" else [parent]
    adapter.payload["BuildCacheUsage"]["Items"] = rows
    preview = adapter.preview("cache", [], 7)
    assert preview["ids"] == []
    assert preview["cache_summary"]["retained_count"] == 2
    assert preview["estimated_bytes"] == 0
    with pytest.raises(DockerConflict, match="No removable"):
        adapter.execute(preview)
    assert not any(method == "POST" for method, _ in adapter.calls)


def test_multiple_parents_active_children_and_cycles_are_retained():
    adapter = FakeDocker()
    adapter.payload["BuildCacheUsage"]["Items"] = [
        cache_record("a"), cache_record("b"),
        cache_record("merged", parents=["a", "b"], in_use=True),
        cache_record("cycle-a", parents=["cycle-b"]),
        cache_record("cycle-b", parents=["cycle-a"]),
        cache_record("free"),
    ]
    preview = adapter.preview("cache", [], 30)
    assert preview["ids"] == ["free"]
    assert preview["cache_summary"]["retained_count"] == 4


@pytest.mark.parametrize("adapter_class", [FakeDocker, LegacyDocker])
def test_shared_cache_is_not_counted_as_private_reclaimable(adapter_class):
    adapter = adapter_class()
    rows = [cache_record("private", size=100), cache_record("shared", size=200, shared=True),
            cache_record("active", size=400, in_use=True), cache_record("unknown", size=800)]
    rows[-1].pop("Shared")
    if isinstance(adapter, LegacyDocker):
        adapter.payload["BuildCache"] = rows
    else:
        adapter.payload["BuildCacheUsage"] = {"Items": rows}
    assert adapter.public_usage()["groups"]["cache"]["reclaimable_bytes"] == 100
    preview = adapter.preview("cache", [], 90)
    assert preview["cache_summary"]["shared_bytes"] == 200
    assert preview["cache_summary"]["unknown_shared_bytes"] == 800
    assert preview["estimated_bytes"] == 100


@pytest.mark.parametrize("days", [7, 30, 90])
def test_cache_age_choices_creation_fallback_and_fixed_cutoff(days, monkeypatch):
    adapter = FakeDocker()
    rows = [cache_record("old", days=days + 1), cache_record("recent", days=days - 1),
            cache_record("never-used", days=days + 1)]
    rows[-1]["LastUsedAt"] = None
    adapter.payload["BuildCacheUsage"]["Items"] = rows
    preview = adapter.preview("cache", [], days)
    assert preview["ids"] == ["never-used", "old"]
    # A record still newer than the frozen cutoff must not enter the selection.
    rows[1]["LastUsedAt"] = (
        datetime.fromisoformat(preview["cutoff"]) + timedelta(seconds=1)
    ).isoformat()
    later = datetime.now(timezone.utc) + timedelta(days=2)
    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return later
    monkeypatch.setattr("coldshelf.docker_storage.datetime", LaterDateTime)
    adapter.execute(preview)
    path = next(path for method, path in adapter.calls if method == "POST")
    pattern = json.loads(parse_qs(urlsplit(path).query)["filters"][0])["id"][0]
    assert not re.fullmatch(pattern, "recent")


@pytest.mark.parametrize("field,value", [
    ("LastUsedAt", "2020-01-01T00:00:00+00:00"),  # Still old but used since review.
    ("UsageCount", 2), ("Size", 123), ("Shared", True), ("InUse", True),
    (" Parents", ["different-parent"]),
])
def test_cache_fingerprint_changes_require_review_before_prune(field, value):
    adapter = FakeDocker()
    preview = adapter.preview("cache", [], 7)
    adapter.payload["BuildCacheUsage"]["Items"][0][field] = value
    with pytest.raises(DockerConflict, match="changed"):
        adapter.execute(preview)
    assert not any(method == "POST" for method, _ in adapter.calls)


def test_new_descendant_requires_review_before_prune():
    adapter = FakeDocker()
    preview = adapter.preview("cache", [], 7)
    adapter.payload["BuildCacheUsage"]["Items"].append(cache_record("new", days=1, parents=["cache-1"]))
    with pytest.raises(DockerConflict, match="changed"):
        adapter.execute(preview)
    assert not any(method == "POST" for method, _ in adapter.calls)


def test_cache_partial_and_metadata_only_results_report_record_counts():
    adapter = FakeDocker()
    adapter.payload["BuildCacheUsage"]["Items"].append(cache_record("cache-2", shared=True))
    adapter.prune_result = {"CachesDeleted": ["cache-2"], "SpaceReclaimed": 0}
    outcome = adapter.execute(adapter.preview("cache", [], 7))[0]
    assert outcome["status"] == "deleted"
    assert outcome["reclaimed_bytes"] == 0
    assert outcome["deleted_ids"] == ["cache-2"]
    assert outcome["not_removed_ids"] == ["cache-1"]
    assert outcome["deleted_count"] == outcome["not_removed_count"] == 1


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
    adapter = DependencyDocker()
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret",
                     docker_storage=adapter, docker_action_delay_seconds=0.05)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        preview = client.post("/api/docker/previews", json={"kind": "cache", "older_than_days": 7}, headers=auth).json()
        assert preview["confirmation_phrase"] == "REMOVE 4 DOCKER CACHE"
        assert preview["cache_summary"]["retained_count"] == 2
        assert preview["estimated_bytes"] == 175
        action = client.post("/api/docker/actions", json={
            "preview_id": preview["id"], "confirmation_phrase": preview["confirmation_phrase"],
        }, headers=auth).json()
        for _ in range(50):
            result = client.get(f"/api/docker/actions/{action['id']}", headers=auth).json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert result["status"] == "completed", result.get("error")
        assert result["outcomes"][0]["reclaimed_bytes"] == 675
        assert result["outcomes"][0]["deleted_ids"] == preview["ids"]
        assert result["outcomes"][0]["deleted_count"] == 4
        assert result["cache_summary"] == preview["cache_summary"]
        assert client.get("/api/docker/actions/recent", headers=auth).json()["actions"][0]["outcomes"] == result["outcomes"]
        assert len([call for call in adapter.calls if call[0] == "POST"]) == 1
    log = (tmp_path / "actions.log").read_text()
    assert '"ids": ["leaf", "middle", "root", "shared"]' in log
    assert '"deleted_count": 4' in log
    assert '"cutoff":' in log
    assert "DOCKER_PREVIEW" in log and "DOCKER_SCHEDULED" in log and "DOCKER_CLEAN" in log


def test_web_all_retained_preview_cannot_be_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    adapter = FakeDocker()
    adapter.payload["BuildCacheUsage"]["Items"].append(cache_record("new", days=1, parents=["cache-1"]))
    app = create_app(repository=WebRepository(tmp_path / "web.sqlite3"), token="secret",
                     docker_storage=adapter, docker_action_delay_seconds=0.01)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        preview = client.post("/api/docker/previews", json={"kind": "cache", "older_than_days": 7}, headers=auth).json()
        assert preview["items"] == []
        assert preview["cache_summary"]["retained_count"] == 1
        response = client.post("/api/docker/actions", json={
            "preview_id": preview["id"], "confirmation_phrase": preview["confirmation_phrase"],
        }, headers=auth)
        assert response.status_code == 409
        assert "No removable" in response.text
    assert not any(method == "POST" for method, _ in adapter.calls)


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


def test_cli_cache_preview_confirmation_results_and_log(monkeypatch, tmp_path):
    adapter = DependencyDocker()
    monkeypatch.setattr("coldshelf.docker_cli.DockerStorage", lambda: adapter)
    monkeypatch.setattr("coldshelf.executor.ACTION_LOG_FILE", tmp_path / "actions.log")
    args = ["docker", "clean", "cache", "--older-than-days", "7"]
    dry_run = CliRunner().invoke(cli, ["--dry-run", *args])
    assert dry_run.exit_code == 0, dry_run.output
    assert "4 cache records selected" in dry_run.output
    assert "175.00 B private" in dry_run.output
    assert "500.00 B shared with images" in dry_run.output
    assert "Retained by cache outside this selection: 2 records" in dry_run.output
    assert not any(method == "POST" for method, _ in adapter.calls)
    rejected = CliRunner().invoke(cli, args, input="REMOVE 6 DOCKER CACHE\n")
    assert rejected.exit_code != 0
    assert not any(method == "POST" for method, _ in adapter.calls)
    approved = CliRunner().invoke(cli, args, input="REMOVE 4 DOCKER CACHE\n")
    assert approved.exit_code == 0, approved.output
    assert "4 cache records removed; 0 retained by Docker" in approved.output
    assert "Docker-reported reclaimed: 675.00 B" in approved.output
    log = (tmp_path / "actions.log").read_text()
    assert '"deleted_ids": ["leaf", "middle", "root", "shared"]' in log
    empty = CliRunner().invoke(cli, args)
    assert empty.exit_code == 0, empty.output
    assert "No cache records can currently be removed" in empty.output
    assert "Type REMOVE" not in empty.output
    assert len([call for call in adapter.calls if call[0] == "POST"]) == 1
