import json
import time

from fastapi.testclient import TestClient

from disk_space_manager.protection.repository import ProtectionRepository
from disk_space_manager.protection.service import ProtectionService
from disk_space_manager.web.app import create_app
from disk_space_manager.web.repository import WebRepository

from .protection_fakes import FakeRestic, FakeVolumes, MemorySecretStore, fake_volume


def _auth():
    return {"Authorization": "Bearer secret"}


def _wait_for_operation(client, operation_id):
    for _ in range(100):
        response = client.get(
            f"/api/protection/operations/{operation_id}", headers=_auth()
        )
        assert response.status_code == 200
        operation = response.json()
        if operation["status"] in {"completed", "failed", "cancelled"}:
            return operation
        time.sleep(0.02)
    raise AssertionError("protection operation did not finish")


def _app(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "catalog.bin").write_bytes(b"catalog data")
    volume = fake_volume(tmp_path / "HDD1", "volume-1", "HDD1")
    volumes = FakeVolumes([volume])
    restic = FakeRestic()
    secrets = MemorySecretStore()
    protected_repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    service = ProtectionService(
        protected_repository, restic, secrets, volumes
    )
    app = create_app(
        repository=WebRepository(tmp_path / "web.sqlite3"),
        protection_repository=protected_repository,
        protection_service=service,
        token="secret",
    )
    return app, source, volume, restic, volumes


def test_authenticated_protected_folder_recovery_confirmation_catalog_and_job(tmp_path):
    app, source, volume, _, _ = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/protection/folders").status_code == 401
        response = client.post(
            "/api/protection/folders",
            headers=_auth(),
            json={
                "name": "Large files",
                "source_path": str(source),
                "required_copies": 2,
            },
        )
        assert response.status_code == 200, response.text
        created = response.json()
        assert created["recovery_key"] == "test-recovery-key"

        wrong = client.post(
            f"/api/protection/folders/{created['id']}/confirm-recovery",
            headers=_auth(),
            json={"recovery_key": "wrong"},
        )
        assert wrong.status_code == 400
        confirmed = client.post(
            f"/api/protection/folders/{created['id']}/confirm-recovery",
            headers=_auth(),
            json={"recovery_key": created["recovery_key"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["recovery_confirmed"] is True

        volumes_response = client.get("/api/protection/volumes", headers=_auth())
        assert volumes_response.status_code == 200
        assert volumes_response.json()["volumes"][0]["volume_uuid"] == "volume-1"
        enrolled = client.post(
            f"/api/protection/folders/{created['id']}/drives",
            headers=_auth(),
            json={"mount_path": volume.mount_path},
        )
        assert enrolled.status_code == 200, enrolled.text

        operation_response = client.post(
            f"/api/protection/folders/{created['id']}/operations",
            headers=_auth(),
            json={"kind": "index"},
        )
        assert operation_response.status_code == 200, operation_response.text
        operation = _wait_for_operation(client, operation_response.json()["id"])
        assert operation["status"] == "completed", operation
        assert operation["result"]["file_count"] == 1

        detail = client.get(
            f"/api/protection/folders/{created['id']}", headers=_auth()
        ).json()
        assert detail["files"][0]["relative_path"] == "catalog.bin"
        assert detail["drives"][0]["mounted"] is True

        invalid_selection = client.post(
            f"/api/protection/folders/{created['id']}/operations",
            headers=_auth(),
            json={
                "kind": "evict",
                "all_files": True,
                "file_ids": [detail["files"][0]["id"]],
                "dry_run": True,
            },
        )
        assert invalid_selection.status_code == 422

        lowering = client.patch(
            f"/api/protection/folders/{created['id']}",
            headers=_auth(),
            json={"name": "Large files", "required_copies": 1},
        )
        assert lowering.status_code == 400
        updated = client.patch(
            f"/api/protection/folders/{created['id']}",
            headers=_auth(),
            json={
                "name": "Large files renamed",
                "required_copies": 1,
                "confirmation_phrase": "UPDATE COPIES Large files 1",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["required_copies"] == 1

        deleted = client.request(
            "DELETE",
            f"/api/protection/folders/{created['id']}",
            headers=_auth(),
            json={"confirmation_phrase": "DELETE PROTECTED Large files renamed"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["repositories_left_intact"] == 1


def test_fresh_catalog_import_from_one_disk(tmp_path):
    app, source, volume, restic, volumes = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/protection/folders",
            headers=_auth(),
            json={"name": "Original", "source_path": str(source)},
        ).json()
        client.post(
            f"/api/protection/folders/{created['id']}/confirm-recovery",
            headers=_auth(),
            json={"recovery_key": created["recovery_key"]},
        )
        drive = client.post(
            f"/api/protection/folders/{created['id']}/drives",
            headers=_auth(),
            json={"mount_path": volume.mount_path},
        ).json()
        index_operation = client.post(
            f"/api/protection/folders/{created['id']}/operations",
            headers=_auth(),
            json={"kind": "index"},
        ).json()
        assert _wait_for_operation(client, index_operation["id"])["status"] == "completed"
        backup_operation = client.post(
            f"/api/protection/folders/{created['id']}/operations",
            headers=_auth(),
            json={"kind": "backup", "drive_target_id": drive["id"]},
        ).json()
        assert _wait_for_operation(client, backup_operation["id"])["status"] == "completed"

    fresh_repository = ProtectionRepository(tmp_path / "fresh-protected.sqlite3")
    fresh_service = ProtectionService(
        fresh_repository, restic, MemorySecretStore(), volumes
    )
    fresh_app = create_app(
        repository=WebRepository(tmp_path / "fresh-web.sqlite3"),
        protection_repository=fresh_repository,
        protection_service=fresh_service,
        token="secret",
    )
    with TestClient(fresh_app) as client:
        extra_marker = (
            tmp_path
            / "HDD1"
            / "DiskSpaceManager"
            / "protected"
            / "another-set"
            / "repository.json"
        )
        extra_marker.parent.mkdir(parents=True)
        extra_marker.write_text(
            json.dumps(
                {
                    "protected_folder_id": "another-set",
                    "repo_relative_path": "DiskSpaceManager/protected/another-set/restic",
                    "repository_id": "unrelated-repository",
                }
            )
        )
        ambiguous = client.post(
            "/api/protection/import",
            headers=_auth(),
            json={
                "name": "Recovered",
                "source_path": str(tmp_path / "fresh-source"),
                "mount_path": volume.mount_path,
                "recovery_key": "test-recovery-key",
            },
        )
        assert ambiguous.status_code == 400
        assert "multiple protected repositories" in ambiguous.json()["detail"]

        response = client.post(
            "/api/protection/import",
            headers=_auth(),
            json={
                "name": "Recovered",
                "source_path": str(tmp_path / "fresh-source"),
                "mount_path": volume.mount_path,
                "recovery_key": "test-recovery-key",
                "required_copies": 2,
                "protected_folder_id": created["id"],
            },
        )
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["folder"]["id"] == created["id"]
        assert detail["files"][0]["relative_path"] == "catalog.bin"
        assert detail["files"][0]["local_state"] == "evicted"
        assert detail["files"][0]["verified_replica_count"] == 1

        restore_operation = client.post(
            f"/api/protection/folders/{created['id']}/operations",
            headers=_auth(),
            json={
                "kind": "restore",
                "file_ids": [detail["files"][0]["id"]],
                "drive_target_id": detail["drives"][0]["id"],
            },
        ).json()
        restored = _wait_for_operation(client, restore_operation["id"])
        assert restored["status"] == "completed", restored
        assert (tmp_path / "fresh-source" / "catalog.bin").read_bytes() == b"catalog data"
