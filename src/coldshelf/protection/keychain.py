"""macOS Keychain-backed Restic recovery-secret management."""

import hmac
import secrets
from typing import Optional

import keyring


# Persistent credential namespace. Renaming it would hide existing recovery keys.
KEYRING_SERVICE = "disk-space-manager.restic"


class SecretStoreError(RuntimeError):
    """Raised when a recovery secret cannot be stored or retrieved."""


class KeychainSecretStore:
    """Store one Restic secret per protected folder in macOS Keychain."""

    def __init__(self, service_name: str = KEYRING_SERVICE):
        self.service_name = service_name

    def generate(self) -> str:
        return secrets.token_urlsafe(48)

    def set(self, folder_id: str, secret: str) -> None:
        if not secret:
            raise SecretStoreError("Recovery key cannot be empty")
        try:
            keyring.set_password(self.service_name, folder_id, secret)
        except Exception as exc:
            raise SecretStoreError(f"Could not write the recovery key to Keychain: {exc}") from exc

    def get(self, folder_id: str) -> str:
        try:
            secret: Optional[str] = keyring.get_password(self.service_name, folder_id)
        except Exception as exc:
            raise SecretStoreError(f"Could not read the recovery key from Keychain: {exc}") from exc
        if not secret:
            raise SecretStoreError(
                "Recovery key is missing from Keychain; import it from the recovery kit"
            )
        return secret

    def delete(self, folder_id: str) -> None:
        try:
            keyring.delete_password(self.service_name, folder_id)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise SecretStoreError(f"Could not delete the Keychain secret: {exc}") from exc

    def matches(self, folder_id: str, candidate: str) -> bool:
        return hmac.compare_digest(self.get(folder_id), candidate)
