# coldshelf

A Python disk maintenance application for Unix-like systems. It can be used as
a terminal CLI or as an optional FastAPI + React web dashboard for visual disk
reports, one-level folder sizing, interactive candidate review, and confirmed
clean/archive actions.

The tool is designed for macOS and Linux. It keeps destructive actions
explicit: clean and archive workflows support dry-run previews, require
confirmation before changing files, and record action logs. The web dashboard
uses the same core scanner, analyzer, duplicate detector, and executor as the
CLI.

## Features

- **Disk Usage Analysis**: Scan and visualize disk usage with detailed
  breakdowns.
- **Whole-Disk and Docker Storage**: Compare filesystem capacity with a
  same-filesystem folder scan and local Docker images, containers, volumes,
  and build cache. Review eligible Docker cleanup with explicit confirmation.
- **Cache File Detection**: Identify common cache and temporary files that can
  be reviewed and removed.
- **Old File Archiving**: Find files not accessed in 6+ months and archive them
  to an external drive or local folder.
- **Duplicate Detection**: Report exact duplicate files and advisory
  near-duplicates for text, images, videos, and audio during full reports.
- **High Performance**: Handles large trees efficiently using `os.scandir`,
  parallel scanning, and optimized analysis.
- **Safety First**: Requires explicit confirmation before destructive actions.
- **Rich CLI**: Terminal UI with progress bars, tables, and color-coded output.
- **Browse by Size**: Inspect one filesystem level at a time with recursively
  calculated folder sizes, direct-file sizes, hidden-content totals, live
  progress, cancellation, session caching, and guarded bulk deletion with a
  server-owned 10-second Undo window.
- **Guided Web Dashboard**: Task-based Home, Browse, Reports, Protected folders,
  and searchable Help views for the complete local workflow, including live
  progress, persisted report history, and guarded actions.
- **Protected Folders**: macOS-first, encrypted Restic archives replicated to
  independently usable disks, with an offline file catalog and guarded local
  eviction only after the configured copy count is verified.
- **External Drive Detection**: Auto-detects writable external drives on macOS
  and common Linux mount locations.
- **Action Logging**: Logs delete and archive actions for review and audit.

## Upgrading from Disk Space Manager

The project is now **coldshelf**, with one installed command: `coldshelf`.
Use `coldshelf web` for the dashboard. The former `disk-space-manager`
command is replaced by `coldshelf`; `coldshelf-web` and
`disk-space-manager-web` are replaced by `coldshelf web`. Those executable
aliases are no longer installed. Python imports use `coldshelf`.

Existing protected folders and offloaded files require no migration or new
backup. The local catalog, Keychain service, disk repository paths, snapshot
tags, and staging identifiers deliberately retain their original names. Do
not rename or delete them to match the new branding. The same recovery key
and either healthy enrolled disk still work with **Restore** and **Recover**.
Report history, the action log, and dashboard authentication are preserved too.
See [rebrand and recovery compatibility](docs/rebranding.md) for exact details.

## Installation

If you do not have `uv` yet, install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

1. Clone this repository:

```bash
git clone https://github.com/shivamk3r/coldshelf.git
cd coldshelf
```

2. Install the command for your user account:

```bash
uv tool install --editable .
```

You can now run `coldshelf` from any directory, without activating a virtual
environment or prefixing it with `uv run`. uv installs the tool and its
dependencies in an isolated environment and exposes only the `coldshelf`
command. This is an editable installation: Python source changes take effect
on the next invocation. Keep the checkout at its installed location; reinstall
from the new location if you move it. Frontend changes require a rebuild.

If your shell cannot find `coldshelf`, run:

```bash
uv tool update-shell
```

Then restart your terminal. `uv tool dir --bin` shows the executable directory,
and `uv tool list` shows installed tools. See [uv's tool installation
guide](https://docs.astral.sh/uv/guides/tools/#installing-tools) for details.

After dependency or command entry-point changes, reinstall from the checkout:

```bash
uv tool install --editable . --reinstall-package coldshelf
```

To remove the global command:

```bash
uv tool uninstall coldshelf
```

For repository development and tests, use the project's separate environment:

```bash
uv sync
uv run coldshelf --help
uv run coldshelf web --dev
```

## Usage

### CLI

Run the installed command from any directory:

```bash
coldshelf --help
```

All examples below use the global installation. From a checkout, you can also
use `uv run coldshelf ...` or `uv run python -m coldshelf ...`.
`uv run python main.py ...` remains a checkout compatibility shim.

### Web Dashboard

Start the packaged FastAPI and React application with one command:

```bash
coldshelf web
```

The server binds to `127.0.0.1` by default, prints its authenticated URL, waits
for Uvicorn to be ready, and opens the dashboard in the default browser. The
frontend immediately removes the token from the visible URL and keeps it in
local browser storage until **Disconnect** is selected. The server stores job
history in `~/.disk-space-manager-web.sqlite3` and serves the built React
frontend from the Python package.

Useful web server options:

```bash
coldshelf web --host 127.0.0.1 --port 38765
coldshelf web --db-path ~/.disk-space-manager-web.sqlite3
coldshelf web --token <token>
coldshelf web --no-open
```

Packaged mode uses only port `38765` by default: FastAPI serves both the API
and the built React frontend from the same listener. Use `--port` to override
it if that port is unavailable.

If no token is provided, the server reads `COLDSHELF_WEB_TOKEN` (or the legacy
`DISK_SPACE_MANAGER_WEB_TOKEN`), then
falls back to `~/.disk-space-manager-web-token`, creating that file when
needed. API requests require the token through the frontend URL, `Authorization:
Bearer <token>`, `X-API-Token`, or the `token` query parameter used by the
Server-Sent Events stream.

For frontend/backend development, run:

```bash
coldshelf web --dev
```

Development mode starts FastAPI and the Vite frontend together, using backend
port `38765` and `--frontend-port 38766` by default. Both remain configurable
with `--port` and `--frontend-port`. Use `--host 0.0.0.0` only when you
intentionally want network access, and keep the token private.

The CLI's `--dry-run` flag is not supported with `web`: it does not make the
dashboard read-only. Use the dashboard's operation previews and confirmations.

The dashboard opens on **Home**, where a primary-storage summary shows total,
used, and available space for the filesystem containing the user's home folder.
Open **Storage** from Home or the navigation to compare that capacity with an
on-demand top-level filesystem scan and local Docker usage. The scan reports
permission errors and skipped mounts. Docker sizes can overlap with each other
and with folder totals, so its reclaimable figures are estimates rather than
guaranteed free space. Docker Desktop usage on macOS may not directly match the
host filesystem total.

The Docker panel lists images, containers, and volumes, and summarizes build
cache. Cleanup is manual and one category at a time: only stopped containers,
unreferenced images, and unlinked local volumes can be selected. Build cache
uses a 7, 30, or 90-day cutoff. A five-minute server-owned preview and typed
confirmation are required. Web actions wait 10 seconds before execution and
can be cancelled during that period; Docker removals cannot be undone after
execution. Volumes may contain persistent application data. No remote Docker
context is used, and the dashboard never performs automatic cleanup.

For an illustrated explanation of images, containers, mounts, and cleanup
decisions, see [How Docker works and where its disk space goes](docs/learn/docker.md).

The same information is available in the CLI:

```bash
coldshelf storage
coldshelf storage --scan-root
coldshelf docker usage
coldshelf docker list images --page 1
coldshelf --dry-run docker clean containers --all-eligible
coldshelf docker clean volumes --id exact-volume-name
coldshelf docker clean cache --older-than-days 30
```

The global `--dry-run` option prints a Docker cleanup preview without deleting
anything. Real CLI cleanup always prints its preview and requires the displayed
phrase. Access to the local Docker socket is required for Docker features.

The Home capacity summary can be refreshed on demand and links directly to
Analyze storage and Archive old files. Task cards also lead to Clean caches,
Review duplicates, Protect folders, Restore files, and Recover a catalog. The
searchable **Help** section documents each operation, validated previews,
explicit confirmations, archive symlinks, protected folder prerequisites,
recovery, terminology, and
troubleshooting.

The **Browse** view opens the user's home folder and lists immediate folders and
files. Its sizing pass is metadata-only. Folder rows are sized recursively using allocation metadata;
the primary **On disk** value uses `st_blocks × 512` when available, while the
secondary **Logical** value uses file content lengths. Hidden rows can be shown
or hidden without changing totals. Browse never follows symbolic links or
crosses into a nested mounted filesystem, and permission failures produce
partial totals with capped error details rather than aborting the whole scan.

Use Home, Computer, or detected-drive shortcuts, breadcrumbs, the parent
button, or a direct path to navigate. Results are sorted by on-disk size by
default and can be searched, re-sorted, paged, refreshed, or cancelled. Up to
32 completed paths are cached for the current frontend session; successful
clean, archive, Browse deletion, protected backup/enrollment, eviction, or
restore operations clear that cache.

After a Browse scan completes, eligible rows can be selected individually or
in bulk for the current 50-row page. **Delete Permanently** asks the server to
validate the exact immediate-child selection and opens a review modal with its
item and byte totals. The second red confirmation schedules one persistent
deletion batch. A global toast offers **Undo** for exactly 10 seconds according
to server time, survives navigation or refresh, and then shows queued/running
progress while deletion waits for the shared disk-I/O worker. Pending rows stay
visible but cannot be opened, protected, or selected again.

Deletion is limited to strict descendants of Home or a detected writable
external-drive root. Home and drive roots, symbolic links, special files,
mount boundaries, incomplete or unreadable folders, protected-folder overlaps,
application state, outside paths, and changed filesystem identities are
blocked. The executor revalidates every top-level item, never follows nested
symbolic links, never crosses devices, and records each independent result as
deleted, skipped, or failed. Only one Browse deletion batch can be active.

Eligible local folders offer **Protect & offload**. This only prefills the
existing Protected folders creation flow with the folder name/path and two
required copies. Recovery-key confirmation, disk enrollment, backup,
verification, eviction preview, and exact confirmation remain mandatory.
Existing exact protected paths open their protected-folder detail; overlapping,
external, mounted-boundary, symbolic-link, unreadable, and unwritable sources
are blocked with an explanation. Direct files cannot be protected, but eligible
regular files can be included in a reviewed Browse deletion.

The report workflow is:

1. Choose a scan folder with the in-app browser or path field, then review the
   age and duplicate options.
2. Watch live progress while the backend scans and analyzes in a background
   worker.
3. Use the Overview, Large items, Duplicates, Clean, Archive, and Activity tabs.
   Charts include legends and text values, and scan errors are listed by path.
4. Search and page candidate tables, then select a page or all matching rows.
5. Run a dry-run for the exact clean/archive selection and destination. A
   changed selection or destination invalidates that preview.
6. Type the displayed confirmation phrase exactly to enable the real action.
   The completion summary is persisted, and the dashboard prompts for a rescan
   because the prior report is stale.

Duplicate results remain advisory: the dashboard does not expose automatic
duplicate deletion. Archive destinations can be a detected writable external
drive (with free-space information) or a local path. Successful archive
actions preserve the scanned directory structure below `archived_files` and
leave symlinks at the original paths.

The **Protected folders** dashboard is separate from report jobs. Its readiness
panel reports macOS/Restic prerequisites and mounted disks, while a lifecycle
checklist guides recovery-key confirmation, enrollment, indexing, backup,
verification, replica coverage, optional eviction, and restore. The web UI can
rename a protected folder, change its required-copy policy with the existing
lowering guard, delete only the local catalog with the guarded phrase, run
selected full verification, restore from an automatic or chosen replica to the
original or another destination, and choose required copies during recovery
import. Catalog rows are searchable and paginated. Queued/running operations
reconnect after reload and can be cancelled safely. Mounted disks are polled
every five seconds only while that section is open; there is no background
agent.

During a protected-folder backup, the dashboard shows Restic's file-transfer
time estimate when one is available, then switches to an explicit structural
check phase without guessing its duration. Finder's `.DS_Store` files are
excluded from protected catalogs and new snapshots at every directory depth.

### Protected Folders (macOS)

Protected folders use one independent encrypted Restic repository per enrolled
disk. Install official Restic 0.19.1 or newer first:

```bash
brew install restic
restic version --json
```

Create a set, paste the one-time recovery key back when prompted, then enroll,
index, and back up one mounted disk at a time:

```bash
coldshelf protect create --name "Large Files" \
  --path /Users/you/Archives/large-files --required-copies 2
coldshelf protect volumes
coldshelf protect enroll "Large Files" --mount-path /Volumes/HDD1
coldshelf protect index "Large Files"
coldshelf protect status "Large Files"
coldshelf protect backup "Large Files" --drive-id <HDD1-DRIVE-ID>
```

Disconnect HDD1, connect and enroll HDD2, and repeat `backup` with HDD2's drive
ID. The two disks never need to be connected simultaneously. Files remain
searchable in `~/.disk-space-manager-protected.sqlite3` while both are offline.

Eviction always starts with a dry-run and uses catalog file IDs, not arbitrary
paths:

```bash
coldshelf --dry-run protect evict "Large Files" --file-id <FILE-ID>
coldshelf protect evict "Large Files" --file-id <FILE-ID>
coldshelf protect restore "Large Files" --file-id <FILE-ID> \
  --drive-id <CONNECTED-DRIVE-ID>
```

The real eviction requires an `EVICT <set> <count>` phrase. It is blocked until
the unchanged file version has verified receipts from the required number of
distinct repositories and the recovery key was confirmed. Structural Restic
checks qualify by default; `protect verify --full` reads data blocks back. No
symlink or stub remains after eviction, and restore never overwrites a path.

Local source volumes use persistent UUIDs so reboot device renumbering does not
invalidate backup receipts. For an older catalog's identity mismatch, connect an
enrolled backup disk and use **Repair local identity** on selected local files,
or `coldshelf protect repair-identity "Large Files" --file-id <FILE-ID>`.
Repair compares local and archived contents before refreshing identity; it keeps
the file and matching receipts intact. Preview eviction again after repair.

See [the protected-folder runbook](docs/protected-folders.md) before protecting
or evicting real data. Do not use the legacy `archive` command on a protected
folder; registered protected paths are excluded from legacy mutations.

### Analyze Disk Usage

Scan and analyze disk usage without making changes:

```bash
coldshelf analyze
```

Scan a specific directory:

```bash
coldshelf analyze --path /path/to/directory
```

### Clean Cache Files

Identify and remove cache files after confirmation:

```bash
coldshelf clean
```

With a custom age threshold:

```bash
coldshelf clean --age-months 3
```

### Archive Old Files

Move old files to an auto-detected external drive:

```bash
coldshelf archive
```

Archive to a local folder:

```bash
coldshelf archive --target-path /path/to/archive/folder
```

Archive to a specific mounted external drive:

```bash
coldshelf archive --external-path /Volumes/MyDrive
coldshelf archive --external-path /media/$USER/MyDrive
```

With a custom age threshold:

```bash
coldshelf archive --age-months 12 --target-path ./my_archive
```

### Full Report

Generate a comprehensive read-only report:

```bash
coldshelf full-report
```

Duplicate and near-duplicate checks run by default. Skip both duplicate phases,
or keep exact duplicates while skipping slower near-duplicate checks:

```bash
coldshelf full-report --no-duplicates
coldshelf full-report --no-near-duplicates
```

### Dry Run Mode

Preview mutating operations without changing files:

```bash
coldshelf --dry-run clean
coldshelf --dry-run archive
```

## Commands

- `analyze` - Analyze disk usage and show insights.
- `clean` - Identify and remove cache files after confirmation.
- `archive` - Move old files to an external drive or local folder.
- `full-report` - Generate a comprehensive analysis report.
- `protect` - Create, enroll, index, back up, verify, evict, restore, or recover
  a replicated protected folder.
- `web` - Start the web dashboard and API.

## Options

- `--path PATH` - Directory to scan (default: home directory).
- `--target-path PATH` - Local folder to use as archive destination.
- `--external-path PATH` - Mounted external drive path (default:
  auto-detect).
- `--age-months N` - Age threshold in months (default: 6).
- `--no-duplicates` - Skip exact and near-duplicate checks for `full-report`.
- `--no-near-duplicates` - Skip near-duplicate checks but keep exact duplicate
  checks for `full-report`.
- `--dry-run` - Show what would be done without making changes.

## Safety Features

1. **Confirmation Prompts**: Mutating actions require explicit confirmation.
2. **Preview Mode**: Summaries show what will be affected before confirmation.
3. **Action Logging**: Actions are logged to
   `~/.disk-space-manager-actions.log`.
4. **Dry Run**: Mutating workflows can be tested without file changes.
5. **Error Handling**: Permission errors and inaccessible files are handled
   gracefully where possible.
6. **Web Token Auth**: Web API routes require a local token. Mutating web
   actions only accept candidate IDs from completed report jobs and require a
   confirmation phrase unless run in dry-run mode.
7. **Independent Replica Receipts**: Protected-folder eviction requires the
   configured number of structurally checked repositories for the unchanged
   catalog version. Offline drive receipts remain usable.
8. **Recovery Secret Isolation**: Restic keys live in macOS Keychain, never in
   SQLite or process arguments. The recovery key is shown once for storage in a
   password manager.
9. **Historical Retention**: Protected-folder code never invokes Restic
   `forget` or `prune` in v1.

## How It Works

### Scanning and Performance

The scanner is optimized for large filesystems:

- `os.scandir()` is used instead of `Path.iterdir()` to avoid creating Python
  `Path` objects for every file and to leverage cached directory-entry data.
- Top-level subdirectories are scanned concurrently with `ThreadPoolExecutor`.
- Excluded prefixes are precomputed once per scan.
- Raw float timestamps are stored during scanning; conversion to `datetime`
  happens only for displayed result subsets.
- Each file is statted once through `DirEntry.stat()`.

### Cache File Detection

Cache candidates are detected from:

- Common Unix-like cache directory patterns, including macOS
  `Library/Caches`, Linux `.cache`, temp directories, and trash folders.
- Cache-like file extensions such as `.cache`, `.tmp`, `.temp`, `.log`, `.old`,
  and `.bak`.
- Filenames containing cache-related markers.

### Old File Detection

Files are considered old if they:

- Have not been accessed in the specified time period (default: 6 months).
- Are larger than 1 MB, to avoid moving many small files.

### Duplicate Detection

Full reports include read-only duplicate analysis:

- Exact duplicates are found by grouping same-size files and streaming SHA-256
  hashes only for candidate groups.
- Near-duplicate text files use normalized token shingles and SimHash.
- Near-duplicate images use perceptual hashes through Pillow and ImageHash.
- Near-duplicate videos use sampled frame perceptual hashes through OpenCV.
- Near-duplicate audio uses offline waveform and spectral fingerprints through
  SoundFile and NumPy.
- Near-duplicate results are advisory and shown separately from the main
  potential savings total.

### Archiving Process

When archiving files to an external drive or local folder:

1. Files are copied to `<target>/archived_files` while preserving directory
   structure relative to the scan path.
2. Original files are removed after successful copy.
3. Symlinks are created at the original locations pointing to archived files.
4. Archive targets inside the scanned directory are excluded from scanning to
   avoid re-archiving previous output.

## External Drive Detection

Auto-detection is best-effort and only used when `--target-path` and
`--external-path` are omitted.

- macOS uses `diskutil` and falls back to mounted directories under `/Volumes`.
- Linux parses `/proc/self/mountinfo`, ignores pseudo/system filesystems, and
  looks for writable non-root mounts under `/media`, `/mnt`, and `/run/media`.

You can always bypass detection with `--target-path` or `--external-path`.

## Configuration

Default settings live in `src/coldshelf/config.py`:

- `DEFAULT_AGE_THRESHOLD_MONTHS`: Default age for old files.
- `CACHE_DIRECTORY_PATTERNS`: Patterns for cache directories.
- `CACHE_FILE_EXTENSIONS`: File extensions considered cache-like.
- `EXCLUDED_DIRECTORIES`: Directories excluded from scanning.
- `DUPLICATE_*` and `NEAR_DUPLICATE_*`: Duplicate display limits, file-size
  caps, sampling counts, and similarity thresholds.
- `ACTION_LOG_FILE`: Action log path.

## Requirements

- Python 3.9+
- macOS or Linux
- A local folder or mounted external drive for archiving
- FastAPI, Uvicorn, and Pydantic for the web API
- SQLite for web job, report, candidate, and action history
- Restic 0.19.1+ for protected folders on macOS
- `keyring` using the macOS Keychain backend for protected-folder secrets
- Offline media libraries for duplicate analysis: Pillow, ImageHash,
  OpenCV headless, SoundFile, and NumPy
- [uv](https://docs.astral.sh/uv/) for dependency management
- Node.js and npm only when developing or rebuilding the React frontend

## Action Log

All delete previews, schedules, cancellations and results, archive actions,
and protected-folder backup, verification, eviction, restore, and enrollment
actions are logged to
`~/.disk-space-manager-actions.log` with timestamps, paths, sizes, status, and
dry-run state.

The web dashboard also stores job history, report summaries, actionable
candidates, and action results in SQLite. It does not store every scanned file,
which keeps the database bounded for large scans.

Browse scans and listings remain ephemeral: at most 32 jobs are kept in memory
and no scan rows are written to report history. Scheduled Browse deletion
batches and per-item fingerprints/results are persisted in SQLite so countdown,
queued, or interrupted work can recover after a server restart.

Protected-folder operation history in
`~/.disk-space-manager-protected.sqlite3` includes duration, last progress,
failure type, and Restic exit code when available. Backup audit entries include
operation, drive, generation, and snapshot identifiers for correlation. Raw
Restic output, recovery keys, and API tokens are never added to these records.

## Architecture

```text
main.py                                   Checkout compatibility shim
src/coldshelf/cli.py             Click command declarations
src/coldshelf/workflows.py       CLI workflow orchestration
src/coldshelf/ui.py              Rich terminal presentation
src/coldshelf/archive_targets.py Archive target resolution
src/coldshelf/scanner.py         Filesystem scanning
src/coldshelf/directory_sizer.py Metadata-only Browse sizing
src/coldshelf/analyzer.py        File categorization and estimates
src/coldshelf/duplicates.py      Duplicate and near-duplicate analysis
src/coldshelf/executor.py        File operations and logging
src/coldshelf/protection/        Catalog, Keychain, Restic, volumes, safety
src/coldshelf/drive_detector.py  External drive auto-detection
src/coldshelf/web/               FastAPI app, services, jobs, SQLite
src/coldshelf/web/deletion_jobs.py Browse deletion policy and scheduler
src/coldshelf/web/static/        Packaged React production build
frontend/                                 React/Vite frontend source
src/coldshelf/config.py          Configuration constants
src/coldshelf/utils.py           Shared utilities
```

## Agent Context

`AGENTS.md` is the root guide for AI agents working in this repository. Shared
agent skills live in `.agents/skills/`, and shared command prompts live in
`.agents/commands/`.

## Limitations

- Requires appropriate file permissions.
- Some system files may be inaccessible.
- Some near-duplicate media formats may be skipped if local decoders cannot
  read them.
- External drives must be mounted and writable when using `--external-path` or
  auto-detection.
- Scanning speed is bounded by filesystem I/O.
- The web backend serializes report, Browse sizing/deletion, and
  protected-folder disk work through one shared heavy-I/O queue.
- The web token model is suitable for local or carefully controlled server use;
  it is not a multi-user account system.
- Report jobs cannot currently be cancelled once started; Browse sizing and
  Restic-backed protection operations support cooperative cancellation.
- Protected folders currently require macOS stable volume UUID discovery.

## License

[MIT License](LICENSE)

## Contributing

Contributions are welcome. Please feel free to submit a pull request.
