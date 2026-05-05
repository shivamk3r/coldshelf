# System Specifications

## Supported Platform and Dependencies

Mac Disk Space Manager is specified as a local CLI application for macOS.

Runtime requirements:

- Python 3.9 or newer.
- Click for CLI parsing.
- Rich for terminal output.
- macOS filesystem semantics and, for external-drive auto-detection,
  `diskutil` or mounted volumes under `/Volumes`.

Development and verification use `uv` and pytest.

## Commands

### Global Option

`--dry-run`

- Applies to mutating commands.
- Shows intended behavior without deleting, moving, creating archive files, or
  creating symlinks.
- Still records dry-run action log entries when executor methods are reached.

### `analyze`

Options:

- `--path PATH`: directory to scan; defaults to the user's home directory.

Behavior:

- Scans the selected path.
- Displays total files, total size, average file size, top file extensions,
  largest directories, and largest files.
- Does not delete, move, or write user-file changes.

### `clean`

Options:

- `--path PATH`: directory to scan; defaults to the user's home directory.
- `--age-months N`: accepted by the command and used to construct the analyzer's
  threshold, though cache detection itself is pattern-based.

Behavior:

- Scans the selected path.
- Finds cache candidates.
- Displays count, sample candidates, and potential space savings.
- If no candidates are found, exits successfully without executor actions.
- Outside dry-run mode, asks for explicit confirmation before deletion.
- Deletes through `ActionExecutor.delete_files`.
- Logs each intended or completed delete action.

### `archive`

Options:

- `--path PATH`: directory to scan; defaults to the user's home directory.
- `--target-path PATH`: local folder to use as archive destination.
- `--ssd-path PATH`: mounted external SSD path to use as archive destination.
- `--age-months N`: old-file threshold in months; defaults to 6.

Target selection:

1. `--target-path`
2. `--ssd-path`
3. Auto-detected external SSD

Behavior:

- Creates `--target-path` when it does not exist.
- Fails if `--ssd-path` is supplied and does not exist.
- Fails if no target can be resolved.
- Uses `<archive target>/archived_files` as the archive base.
- Excludes the archive target from scanning, including when the archive target
  is inside the scan path.
- Skips symlink paths when building archive candidates.
- Finds old files using access time and the minimum file size threshold.
- Outside dry-run mode, asks for explicit confirmation before moving files.
- Archives by copying each file to the target, unlinking the original, and
  creating a symlink at the original path.
- Logs each intended or completed move action.

### `full-report`

Options:

- `--path PATH`: directory to scan; defaults to the user's home directory.
- `--age-months N`: old-file threshold in months; defaults to 6.

Behavior:

- Scans the selected path with detailed progress and ETA estimation.
- Finds cache candidates.
- Finds old-file candidates.
- Displays disk usage, top extensions, largest directories, largest files,
  cache summary, old-file summary, and total potential savings.
- Does not delete, move, or write user-file changes.

## Scan Specification

`DiskScanner.scan()` returns a dictionary with:

- `files`: list of dictionaries.
- `directories`: dictionary mapping directory path strings to byte totals.
- `total_scanned`: number of scanned files.
- `errors`: list of non-fatal scan error messages.

Each scanned file dictionary contains:

- `path`: path string from the filesystem entry.
- `size`: file size in bytes.
- `atime`: access timestamp as an epoch float.
- `mtime`: modification timestamp as an epoch float.
- `ctime`: creation/change timestamp as an epoch float.

Scanning rules:

- Use `os.scandir`.
- Do not follow directory symlinks.
- Skip configured excluded prefixes.
- When scanning the home directory, also apply user-home exclusions.
- Apply caller-supplied `exclude_paths`.
- Continue past permission and OS errors where possible.

Progress rules:

- The simple progress callback receives monotonically increasing file counts.
- The detailed progress callback receives `ScanProgress` snapshots.
- Final detailed progress must mark `is_finished=True` and reflect actual
  result counts.

## Analysis Specification

Cache candidates are files matching at least one of:

- Configured cache directory patterns.
- Configured cache file extensions.
- Cache-like filename substrings.

Each cache result includes a `reason` string describing why it matched.

Old-file candidates must:

- Have `size >= MIN_FILE_SIZE_TO_MOVE`.
- Have `atime` older than the configured age threshold.

Each old-file result includes:

- `days_old`
- `age_category`
- `accessed` as a display-ready `datetime`

Old-file results are sorted by size descending.

Potential savings are reported separately for cache candidates and old-file
candidates, then combined.

## Execution and Logging Specification

`ActionExecutor` owns file mutations and action logging.

Delete behavior:

- In dry-run mode, count each candidate as deleted and log a dry-run delete.
- In normal mode, call `safe_delete` for each candidate.
- Count and log successes and failures.

Archive behavior:

- In dry-run mode, count each candidate as moved and log a dry-run move.
- In normal mode, ensure the archive base exists.
- Preserve paths relative to the scan root.
- Copy with metadata preservation.
- Unlink the original file.
- Create a symlink from the original path to the archived copy.
- Count and log successes and failures.

Logging behavior:

- Log entries are kept in memory for the executor instance.
- Log entries are appended to `~/.mac-disk-cleaner-actions.log`.
- Log entries include timestamp, action type, source, optional target, size,
  success or failure, optional error text, and dry-run status.
- Failure to write the log file must not abort the underlying operation.

## Safety Requirements

- Destructive `clean` and `archive` command paths must keep explicit
  confirmation outside dry-run mode.
- Dry-run behavior must remain non-mutating for user files and archive output.
- Action logging must remain enabled for executed and dry-run action paths.
- Automated tests and validation must use temporary directories or small
  intentional paths.
- Automated validation must not run destructive commands against home
  directories, system directories, or external drives.
- Archive logic must preserve symlink behavior at original file locations.
- Previously archived output under an archive target inside the scan path must
  not be re-archived.

## Tested Invariants

The current test suite covers these behavioral invariants:

- `archive --target-path` works with local folders.
- Missing local archive target directories are created.
- `--target-path` takes precedence over `--ssd-path`.
- Existing `--ssd-path` behavior continues to work.
- Multiple old files can be archived in one run.
- Directory structure is preserved under `archived_files`.
- Archive targets inside the scan path are excluded from scanning.
- Repeated archive runs do not re-archive prior output.
- Dry-run archive does not move files or create archive output.
- Scanner `exclude_paths` skip selected directories and their descendants.
- Scanner progress counts are monotonic.
- Detailed scan progress finalizes with actual counts.
- Analyzer progress callbacks are monotonic and end at total file count.
- Progress callbacks do not alter analysis results.
- `full-report` runs against empty, nested, cache-containing, and larger test
  directories.
- Profiling helper cleanup is safe and rejects unsafe benchmark paths.

## Verification

Lightweight verification before handoff:

```bash
uv run pytest
```

Useful manual checks:

```bash
uv run python main.py full-report --path tests --age-months 6
uv run python scripts/profile_report_generation.py --file-count 10000 --max-bytes 50000000
```

Run the profiler only for performance-sensitive scanner, analyzer, progress, or
report changes. The profiler owns `downloads/benchmark` and may delete and
recreate it.
