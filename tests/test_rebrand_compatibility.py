"""The coldshelf name must not orphan existing catalogs, keys, or snapshots."""

from pathlib import Path

from coldshelf.config import ACTION_LOG_FILE
from coldshelf.protection.keychain import KeychainSecretStore
from coldshelf.protection.repository import DEFAULT_PROTECTION_DB_PATH
from coldshelf.protection.restic import ResticRunner, generation_id_from_tags
from coldshelf.protection.service import CONTROL_DIRECTORY, REPOSITORY_ROOT
from coldshelf.web.repository import DEFAULT_DB_PATH
from coldshelf.web.security import DEFAULT_TOKEN_FILE, get_or_create_token


def test_persistent_storage_identifiers_are_not_renamed():
    assert DEFAULT_PROTECTION_DB_PATH == Path.home() / ".disk-space-manager-protected.sqlite3"
    assert DEFAULT_DB_PATH == Path.home() / ".disk-space-manager-web.sqlite3"
    assert DEFAULT_TOKEN_FILE == Path.home() / ".disk-space-manager-web-token"
    assert ACTION_LOG_FILE == Path.home() / ".disk-space-manager-actions.log"
    assert CONTROL_DIRECTORY == ".disk-space-manager"
    assert REPOSITORY_ROOT == Path("DiskSpaceManager/protected")
    assert generation_id_from_tags(["dsm-set-folder", "dsm-generation-existing"]) == "existing"


def test_default_secret_store_reads_pre_rebrand_key_without_rewriting_it(monkeypatch):
    existing = {("disk-space-manager.restic", "existing-folder"): "saved-recovery-key"}
    monkeypatch.setattr(
        "coldshelf.protection.keychain.keyring.get_password",
        lambda service, folder: existing.get((service, folder)),
    )

    def unexpected_write(*args):
        raise AssertionError("The rebrand must not replace existing keys")

    monkeypatch.setattr("coldshelf.protection.keychain.keyring.set_password", unexpected_write)
    assert KeychainSecretStore().get("existing-folder") == "saved-recovery-key"
    env = ResticRunner()._environment(Path("/repo"), "existing-folder")
    assert "-m coldshelf.protection.password_command existing-folder" in env["RESTIC_PASSWORD_COMMAND"]
    assert "saved-recovery-key" not in repr(env)


def test_web_token_new_environment_legacy_environment_and_existing_file(tmp_path, monkeypatch):
    token_file = tmp_path / ".disk-space-manager-web-token"
    token_file.write_text("existing-file-token\n")
    monkeypatch.delenv("COLDSHELF_WEB_TOKEN", raising=False)
    monkeypatch.delenv("DISK_SPACE_MANAGER_WEB_TOKEN", raising=False)
    assert get_or_create_token(token_file=token_file) == "existing-file-token"
    monkeypatch.setenv("DISK_SPACE_MANAGER_WEB_TOKEN", "legacy-env-token")
    assert get_or_create_token(token_file=token_file) == "legacy-env-token"
    monkeypatch.setenv("COLDSHELF_WEB_TOKEN", "new-env-token")
    assert get_or_create_token(token_file=token_file) == "new-env-token"
    assert get_or_create_token("explicit-token", token_file) == "explicit-token"
    assert token_file.read_text() == "existing-file-token\n"
