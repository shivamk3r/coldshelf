# Code Architecture

## Overview

The repository is a small Python CLI organized around a pipeline:

```text
Click command -> DiskScanner -> FileAnalyzer -> Rich output
                                |
                                v
                         ActionExecutor
```

Most modules are intentionally flat and dependency-light. `main.py` coordinates
the user workflows, while scanning, analysis, execution, drive detection, and
progress estimation live in separate modules.

## Runtime Modules

### `main.py`

`main.py` is the CLI entry point. It defines the Click command group, the global
`--dry-run` option, and the commands `analyze`, `clean`, `archive`, and
`full-report`.

Primary responsibilities:

- Resolve command options and defaults.
- Create `DiskScanner`, `FileAnalyzer`, and `ActionExecutor` instances.
- Render Rich tables, summaries, and progress bars.
- Prompt for confirmation before destructive actions.
- Select archive targets using the precedence `--target-path`, then
  `--ssd-path`, then auto-detected external SSD.
- Exclude archive targets from scans when archiving.

The helper display functions in this file keep command bodies readable:
`show_disk_usage_analysis`, `show_cache_analysis`, and
`show_old_files_analysis`.

### `scanner.py`

`scanner.py` owns filesystem traversal. `DiskScanner.scan()` returns:

- `files`: list of file dictionaries with `path`, `size`, `atime`, `mtime`, and
  `ctime`.
- `directories`: mapping of directory path strings to byte totals for files
  directly encountered in that directory.
- `total_scanned`: total number of files in the result.
- `errors`: non-fatal scan errors.

Important implementation details:

- Root entries are inspected with `os.scandir`.
- Top-level subdirectories are scanned in parallel worker threads.
- `_scan_recursive` avoids following directory symlinks.
- Exclusion prefixes are computed once from configured system exclusions, user
  home exclusions, and caller-supplied `exclude_paths`.
- Progress can be reported through a simple file-count callback or a detailed
  `ScanProgress` callback.
- Worker threads batch `_ScanProgressDelta` events through a queue.

`get_largest_directories()` and `get_largest_files()` use `heapq.nlargest` over
the scanner's in-memory results.

### `analyzer.py`

`analyzer.py` contains `FileAnalyzer`, which operates on scanner file
dictionaries.

Primary responsibilities:

- Detect cache candidates using configured path patterns, extension checks, and
  filename markers.
- Detect old files using access time, configured age threshold, and minimum file
  size.
- Add result-specific metadata such as cache `reason`, old-file `days_old`,
  `age_category`, and display-ready `accessed` datetime.
- Summarize total size, file count, average file size, and top extensions.
- Calculate potential space savings for cache deletion and old-file archiving.

Performance-oriented constants are prepared at import time: compiled cache
directory regexes, quick substring markers, frozen extension sets, and filename
substrings.

### `executor.py`

`executor.py` contains `ActionExecutor`, the only module that performs
destructive user-file operations.

Primary responsibilities:

- Track dry-run state.
- Log every intended or completed action in memory and in
  `~/.mac-disk-cleaner-actions.log`.
- Delete files through `utils.safe_delete`.
- Archive files into a target base directory while preserving paths relative to
  the scan root.
- Copy file metadata with `shutil.copy2`, unlink the original file, and create a
  symlink at the original path that points to the archived copy.

The `confirm` parameters on executor methods are currently caller-facing API
shape; confirmation is handled in `main.py` before the executor is called.

### `ssd_detector.py`

`ssd_detector.py` detects writable external volumes for archive targets.

Primary responsibilities:

- List mounted volumes using `diskutil list` and `diskutil info`.
- Fall back to `/Volumes` iteration if `diskutil` is unavailable.
- Identify external drives using `/Volumes` paths or device differences from
  the root filesystem.
- Filter to writable volumes.
- Return the first detected external drive when no manual path is supplied.

### `progress_estimator.py`

`progress_estimator.py` converts `ScanProgress` snapshots into `ScanEstimate`
objects suitable for Rich's determinate progress bars.

The estimator starts with a placeholder total while there is too little
directory information, then estimates total work from the ratio of completed to
discovered directories. It smooths increases and decreases, keeps a small amount
of visible remaining work while directories remain, and snaps to the actual
total when the scan finishes.

### `config.py`

`config.py` contains repository-wide constants:

- Default old-file age threshold.
- Cache directory patterns.
- Cache-like file extensions.
- System and user-home scan exclusions.
- Minimum file size for archive candidates.
- Action log path.

Changes to these constants can materially affect safety, scan scope, and user
trust, so they should be paired with focused tests.

### `utils.py`

`utils.py` contains shared helpers for formatting sizes, reading file metadata,
checking excluded paths, calculating directory sizes, checking available space,
creating symlinks or copies, safe deletion, and computing archive target paths
that preserve source-relative structure.

Some helpers support current command paths directly, while others are available
for older or future workflows.

## Command Flows

### `analyze`

1. Resolve scan path, defaulting to `Path.home()`.
2. Scan the filesystem.
3. Analyze disk usage.
4. Render summary tables, largest directories, and largest files.

This command is read-only.

### `clean`

1. Resolve scan path and age threshold.
2. Scan the filesystem.
3. Find cache candidates.
4. Render preview and savings summary.
5. Confirm deletion unless dry-run mode is active.
6. Delete candidates through `ActionExecutor.delete_files`.
7. Render execution summary and action log path when actions were logged.

### `archive`

1. Resolve scan path and archive target.
2. Create a local `--target-path` if needed.
3. Build the archive base as `<target>/archived_files`.
4. Scan with `exclude_paths=[archive_target]`.
5. Remove symlink paths from old-file candidates.
6. Find old files above the minimum size threshold.
7. Render preview and savings summary.
8. Confirm move unless dry-run mode is active.
9. Archive candidates through `ActionExecutor.move_files_to_ssd`.

Target precedence is deliberate: `--target-path` wins over `--ssd-path`, and
manual paths win over auto-detection.

### `full-report`

1. Resolve scan path and age threshold.
2. Scan with detailed progress and ETA estimation.
3. Find cache candidates with progress.
4. Find old-file candidates with progress.
5. Render usage, largest paths, cache, old-file, and savings sections.

This command is read-only.

## Tests and Profiling

The test suite uses pytest and Click's `CliRunner`. It focuses on:

- Archive behavior for local folders.
- Archive target precedence.
- Excluding archive targets inside scanned trees.
- Repeated archive runs.
- Direct executor archive behavior.
- Scanner simple and detailed progress callbacks.
- Analyzer progress callbacks.
- Ensuring progress callbacks do not change analysis results.
- `full-report` command smoke coverage.
- Profiling helper safety and cleanup behavior.

`scripts/profile_report_generation.py` is the performance harness. It owns and
recreates `downloads/benchmark`, generates deterministic sparse-file datasets,
runs `full-report`, and removes the benchmark tree unless `--keep-benchmark` is
specified.

## Extension Points

When adding features, preserve the existing module boundaries:

- Add new CLI orchestration and presentation in `main.py`.
- Keep filesystem traversal concerns in `scanner.py`.
- Put classification and summary logic in `analyzer.py`.
- Put file mutations only in `executor.py`.
- Update `config.py` for default thresholds, patterns, and exclusions.
- Add tests using temporary paths and `CliRunner`; avoid broad real filesystem
  scans in automated validation.
