from click.testing import CliRunner

from coldshelf.cli import cli
from coldshelf.protection.repository import ProtectionRepository
from coldshelf.protection.service import ProtectionService

from .protection_fakes import FakeRestic, FakeVolumes, MemorySecretStore, fake_volume


def test_protect_create_requires_recovery_key_pasteback(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    volume = fake_volume(tmp_path / "HDD", "volume", "HDD")
    service = ProtectionService(
        ProtectionRepository(tmp_path / "protected.sqlite3"),
        FakeRestic(),
        MemorySecretStore(),
        FakeVolumes([volume]),
    )
    monkeypatch.setattr(
        "coldshelf.protection.cli._service", lambda: service
    )

    result = CliRunner().invoke(
        cli,
        [
            "protect",
            "create",
            "--name",
            "Large files",
            "--path",
            str(source),
        ],
        input="test-recovery-key\n",
    )
    assert result.exit_code == 0, result.output
    assert "shown once" in result.output
    folder = service.repository.get_folder("Large files")
    assert folder["recovery_confirmed"] is True


def test_legacy_clean_skips_registered_protected_paths(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    protected_cache = source / "protected.tmp"
    protected_cache.write_text("do not delete")
    repository = ProtectionRepository(tmp_path / "protected.sqlite3")
    repository.create_folder("folder", "Protected", source, 2)
    monkeypatch.setattr(
        "coldshelf.workflows.ProtectionRepository", lambda: repository
    )

    result = CliRunner().invoke(
        cli,
        ["clean", "--path", str(source), "--age-months", "1"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "skipped 1 candidate" in result.output
    assert protected_cache.exists()


def test_cli_protection_operation_is_persisted(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "catalog.bin").write_bytes(b"catalog")
    volume = fake_volume(tmp_path / "HDD", "volume", "HDD")
    service = ProtectionService(
        ProtectionRepository(tmp_path / "protected.sqlite3"),
        FakeRestic(),
        MemorySecretStore(),
        FakeVolumes([volume]),
    )
    created = service.create_folder("Large files", source)
    service.confirm_recovery_key(created["id"], created["recovery_key"])
    monkeypatch.setattr("coldshelf.protection.cli._service", lambda: service)

    result = CliRunner().invoke(cli, ["protect", "index", "Large files"])

    assert result.exit_code == 0, result.output
    operations = service.repository.list_operations(created["id"])
    assert len(operations) == 1
    assert operations[0]["kind"] == "index"
    assert operations[0]["status"] == "completed"
