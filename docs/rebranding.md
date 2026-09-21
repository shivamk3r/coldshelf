# coldshelf rebrand and recovery compatibility

Disk Space Manager is now coldshelf. The distribution, Python package, CLI,
web dashboard, and GitHub repository use the new name. Run `uv sync` from the
renamed checkout, then `uv run coldshelf web` or `uv run coldshelf --help`.
For access from any directory, run `uv tool install --editable .` in the
checkout, then use `coldshelf` and `coldshelf web`. Python imports use `coldshelf`.

Only `coldshelf` is installed as an executable. Replace `disk-space-manager`
with `coldshelf`, and replace both `coldshelf-web` and
`disk-space-manager-web` with `coldshelf web`. The former executable aliases
have been removed. After updating an existing installation, run
`uv tool install --editable . --reinstall-package coldshelf` from the checkout
to refresh its commands. Use `uv sync` to refresh checkout commands.

## Existing protected folders

This is a branding change, with no catalog schema or archive format migration.
These persistent identifiers stay unchanged on existing and new installations:

| State | Stable identifier | Why it stays |
| --- | --- | --- |
| Protected catalog | `~/.disk-space-manager-protected.sqlite3` | Folder IDs, source paths, offloaded files, snapshots, and replica receipts |
| Recovery keys | Keychain service `disk-space-manager.restic` | Existing encryption passwords remain available |
| External repositories | `DiskSpaceManager/protected/<set-uuid>/restic` | Existing drive markers and fresh-Mac discovery remain valid |
| Snapshot tags | `dsm-set-*`, `dsm-generation-*`, `dsm-protected` | Historical snapshots can still rebuild a catalog |
| Local staging | `.disk-space-manager` under a protected source | Interrupted restore/eviction reconciliation stays compatible |
| Web history | `~/.disk-space-manager-web.sqlite3` | Saved reports and pending deletion/Undo state remain available |
| Audit log | `~/.disk-space-manager-actions.log` | Existing action history continues in one file |
| Web token file | `~/.disk-space-manager-web-token` | Existing dashboard authentication remains valid |

Do not rename these files, the Keychain service, or directories on backup disks.
No new encryption key, disk enrollment, or backup is needed for the rebrand.
The application checkout is independent of protected source paths and external
Restic repositories. Moving the checkout does not relocate those files.

## Restore offloaded files

Connect a healthy enrolled disk, start `uv run coldshelf web`, and use
**Protected folders → Restore**. The same catalog entries and recovery key work.
For the CLI, use:

```bash
uv run coldshelf protect status "<folder-name>"
uv run coldshelf protect restore "<folder-name>" --file-id <FILE-ID> \
  --drive-id <CONNECTED-DRIVE-ID>
```

Restores still refuse to overwrite an existing destination. The full
[protected-folder runbook](protected-folders.md) covers alternate destinations,
fresh-Mac catalog recovery, and standalone Restic recovery. Old saved recovery
instructions remain valid after replacing the command names above because
disk paths and encryption are unchanged.

## Environment and browser compatibility

Use `COLDSHELF_WEB_TOKEN`, `VITE_COLDSHELF_API_BASE`, and
`VITE_COLDSHELF_TOKEN` for new configuration. The former
`DISK_SPACE_MANAGER_WEB_TOKEN`, `VITE_DSM_API_BASE`, and `VITE_DSM_TOKEN`
variables remain fallbacks; the new variables take precedence.
The browser accepts its old `dsm-token` entry and saves subsequent tokens as
`coldshelf-token`. Disconnect removes both entries. Default ports stay 38765
(packaged/backend) and 38766 (development frontend).
