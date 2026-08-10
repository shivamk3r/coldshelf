# Protected Folders Runbook

This runbook protects `/Users/you/Archives/large-files` in two
independent encrypted Restic repositories. HDD1 and HDD2 may be connected in
separate sessions. Do not run the legacy `archive` command on this folder.

## Safety model

- The catalog is stored separately at
  `~/.disk-space-manager-protected.sqlite3` and remains searchable offline.
- Each disk has an independent repository at
  `DiskSpaceManager/protected/<set-uuid>/restic`.
- A disk is identified by both its macOS volume UUID and Restic repository ID,
  so renaming a disk is safe and replacing one is detected.
- The recovery key is generated once, kept in macOS Keychain, shown once, and
  must be saved separately in a password manager.
- Backup credit requires Restic exit code 0, an exact indexed-manifest match,
  unchanged source stats before and after backup, and a successful `restic
  check`.
- Eviction requires two distinct verified repository receipts for the current
  unchanged file version. Both disks do not need to be connected at eviction
  time.
- Structural checks do not read every stored data block. Run full verification
  when you want bit-for-bit readback confidence.
- No automatic `forget` or `prune` is performed. Eviction leaves no symlink or
  stub. Restore never overwrites an existing path.

Two disks stored beside each other do not protect against theft, fire, or a
shared-location accident. Store them separately when offline.

## 1. Install and verify Restic

```bash
brew install restic
restic version --json
```

The command must report version 0.19.1 or newer. Then prepare the project:

```bash
cd /path/to/disk-space-manager
uv sync
```

## 2. Create “Large Files”

```bash
uv run disk-space-manager protect create \
  --name "Large Files" \
  --path /Users/you/Archives/large-files \
  --required-copies 2
```

Save the displayed recovery key and standalone recovery instructions in your
password manager. Paste the key back at the hidden prompt. Enrollment is
blocked until this confirmation succeeds.

If the prompt was interrupted after the set was created:

```bash
uv run disk-space-manager protect confirm-recovery "Large Files"
```

## 3. Enroll and back up HDD1

Connect HDD1 and confirm the mounted volume identity:

```bash
uv run disk-space-manager protect volumes
uv run disk-space-manager protect enroll "Large Files" \
  --mount-path /Volumes/HDD1
uv run disk-space-manager protect index "Large Files"
uv run disk-space-manager protect status "Large Files"
```

Copy the HDD1 drive ID shown by `status`, then run:

```bash
uv run disk-space-manager protect backup "Large Files" \
  --drive-id <HDD1-DRIVE-ID>
```

Wait for `verification: structural_checked`. Check status again. Files should
show 1/2 coverage. Eject HDD1 normally in Finder before disconnecting it.

## 4. Enroll and back up HDD2 later

Connect HDD2; HDD1 may remain disconnected:

```bash
uv run disk-space-manager protect volumes
uv run disk-space-manager protect enroll "Large Files" \
  --mount-path /Volumes/HDD2
uv run disk-space-manager protect index "Large Files"
uv run disk-space-manager protect status "Large Files"
uv run disk-space-manager protect backup "Large Files" \
  --drive-id <HDD2-DRIVE-ID>
```

If any file changed between the two sessions, it will not inherit HDD1's old
receipt. The backup or eviction gate will report the change. Re-index, back up
the new generation to each disk in turn, and reach 2/2 for that version.

## 5. Optionally perform full verification

Structural verification is the default eviction threshold. To read every data
block on a connected disk:

```bash
uv run disk-space-manager protect verify "Large Files" \
  --drive-id <CONNECTED-DRIVE-ID> --full
```

To read back only selected files, add one or more `--file-id` options.

## 6. Prove independent restoration before eviction

Choose a small sample file ID from `protect status`. Because the original still
exists, restore into a separate test destination.

With only HDD1 connected:

```bash
uv run disk-space-manager protect restore "Large Files" \
  --file-id <SAMPLE-FILE-ID> --drive-id <HDD1-DRIVE-ID> \
  --destination /Users/you/restore-test-hdd1
```

Compare the restored sample with the original using `cmp`, then remove the test
copy manually after inspection. Repeat with only HDD2 connected and a different
test destination:

```bash
uv run disk-space-manager protect restore "Large Files" \
  --file-id <SAMPLE-FILE-ID> --drive-id <HDD2-DRIVE-ID> \
  --destination /Users/you/restore-test-hdd2
```

Do not proceed if either independent restore fails.

## 7. Dry-run, then evict selected Mac files

Preview selected files first. The global `--dry-run` appears before `protect`:

```bash
uv run disk-space-manager --dry-run protect evict "Large Files" \
  --file-id <FILE-ID>
```

Review eligible and blocked entries. The warning explicitly says whether only
structural verification was used. Then run the real command:

```bash
uv run disk-space-manager protect evict "Large Files" \
  --file-id <FILE-ID>
```

Type the exact displayed `EVICT Large Files <count>` phrase. Eviction is
performed file by file through an atomic staging location and is recorded with
the repository/snapshot evidence in SQLite and
`~/.disk-space-manager-actions.log`.

Use `--all` instead of `--file-id` only after reviewing the complete dry-run.

## 8. Restore an evicted file later

Connect either healthy enrolled disk and run:

```bash
uv run disk-space-manager protect restore "Large Files" \
  --file-id <FILE-ID> --drive-id <CONNECTED-DRIVE-ID>
```

The command restores into staging and atomically places the file at its
original path. It refuses to overwrite an existing path. Use `--destination`
to restore a copy elsewhere.

## 9. Dashboard workflow

```bash
uv run disk-space-manager-web
```

Open the printed tokenized URL and choose **Protected folders**. The dashboard
shows coverage totals, mounted/offline drive cards, capacity and repository
identity, structural/full verification dates, a searchable catalog, per-drive
replica columns, progress, cancellation, and the same guarded actions. While
this section is open it checks mounted volumes every five seconds and prompts
for pending work. It installs no daemon or background agent.

## 10. Fresh-Mac recovery from either disk

Install this application and Restic on the new Mac, connect either disk, and
run:

```bash
uv run disk-space-manager protect import \
  --name "Large Files" \
  --source-path /Users/you/Archives/large-files \
  --mount-path /Volumes/HDD1 \
  --required-copies 2
```

Enter the recovery key at the hidden prompt. The catalog is rebuilt from
tagged Restic snapshots even if the original SQLite database is unavailable.
Files are initially cataloged as evicted and can be restored normally. The web
dashboard's **Recover** form performs the same import.

If a disk contains repositories for multiple protected folders, copy the set
UUID shown in its `DiskSpaceManager/protected/<set-uuid>/repository.json`
marker and add `--set-id <set-uuid>` to the import command.

For standalone Restic recovery, retrieve the saved recovery instructions and
key from your password manager, point `RESTIC_REPOSITORY` at the repository on
either disk, supply the password without placing it in a command argument, and
use `restic snapshots`, `restic ls`, or `restic restore`.
