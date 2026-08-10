"""Print a Restic secret from Keychain for RESTIC_PASSWORD_COMMAND."""

import sys

from .keychain import KeychainSecretStore, SecretStoreError


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: password_command <protected-folder-id>", file=sys.stderr)
        return 2
    try:
        secret = KeychainSecretStore().get(sys.argv[1])
    except SecretStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
