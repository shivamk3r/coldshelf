# Agent Instructions

## Repository Purpose

Disk Space Manager is a Python CLI for Unix-like disk maintenance, with support
for macOS and Linux. It scans disk usage, identifies removable cache and
temporary files, and archives old files to an external drive or local folder
while preserving directory structure and leaving symlinks behind. Safety
matters here: destructive actions must keep explicit confirmation, dry-run
behavior, and action logging intact.

## Default Context / Loading Order

1. Read this `AGENTS.md` file first. It is the canonical cross-agent guide.
2. Read `README.md` for user-facing commands, expected behavior, safety notes,
   and architecture.
3. Read `pyproject.toml`, `uv.lock`, and `requirements.txt` for dependency and
   test setup.
4. Read `src/disk_space_manager/config.py` before changing scan exclusions,
   cache patterns, thresholds, or action-log behavior.
5. Read the relevant implementation module and matching tests before editing:
   `src/disk_space_manager/cli.py`, `workflows.py`, `ui.py`, `scanner.py`,
   `analyzer.py`, `executor.py`, `drive_detector.py`, `utils.py`, `tests/`,
   and `docs/`.
6. Use `.agents/skills/` and `.agents/commands/` for shared agent assets. The
   `.claude`, `.cursor`, and `.codex` folders only expose compatibility links.

## Repo Directory Map

- `main.py` - Compatibility shim for `uv run python main.py ...`.
- `src/disk_space_manager/cli.py` - Click command declarations.
- `src/disk_space_manager/workflows.py` - Command orchestration.
- `src/disk_space_manager/ui.py` - Rich terminal presentation and prompts.
- `src/disk_space_manager/archive_targets.py` - Archive target resolution.
- `src/disk_space_manager/scanner.py` - High-performance filesystem scanning
  with `os.scandir` and a thread pool.
- `src/disk_space_manager/analyzer.py` - Cache detection, old-file detection,
  and disk usage summaries.
- `src/disk_space_manager/executor.py` - Delete/archive operations, symlink
  creation, and action logs.
- `src/disk_space_manager/drive_detector.py` - Unix-like external-drive
  detection using platform tools and mount metadata.
- `src/disk_space_manager/config.py` - Defaults, cache patterns, exclusions,
  thresholds, and log path.
- `src/disk_space_manager/utils.py` - Shared helpers such as size formatting
  and safe file operations.
- `tests/` - Pytest coverage for CLI behavior, archiving, drive detection,
  progress callbacks, and profiling helpers.
- `scripts/profile_report_generation.py` - Optional performance profiler that
  owns and recreates `downloads/benchmark`.
- `docs/profiling.md` - Profiling workflow documentation.
- `downloads/` - Ignored scratch/output area. Do not rely on it for source
  data.
- `.agents/` - Canonical shared skills and commands for AI agents.

## Coding and Development Workflow

- Use `uv sync` to set up dependencies.
- Run CLI commands through `uv run`, for example
  `uv run disk-space-manager full-report --path tests`.
- Keep changes narrow and aligned with the current module boundaries. Avoid new
  dependencies unless they are clearly justified.
- Preserve the CLI's safety model: destructive operations need confirmation,
  dry-run support, clear summaries, and action logging.
- Prefer temporary directories, fixtures, and `click.testing.CliRunner` in
  tests. Do not write tests that scan a real home directory or broad system
  paths.
- When editing scanner or analyzer performance paths, avoid per-file object
  churn and keep the existing large-tree performance assumptions in mind.
- Treat `downloads/benchmark` as profiler-owned scratch space. The profiler may
  delete and recreate it.

## Verification Commands Before Handoff

Run the lightweight project check before handoff:

```bash
uv run pytest
```

Use these as needed:

```bash
uv sync
uv run disk-space-manager full-report --path tests --age-months 6
uv run python scripts/profile_report_generation.py --file-count 10000 --max-bytes 50000000
```

Run the profiler only for performance-sensitive scanner, analyzer, or report
changes. It creates and deletes `downloads/benchmark`.

## Privacy and Safety Rules

- Do not run destructive `clean` or `archive` operations against a user's home
  directory, system directories, or external drives during automated
  validation. Use `--dry-run` and small temporary paths for checks.
- Treat discovered file paths, disk reports, and action logs as private. Avoid
  pasting sensitive local paths into issues, commits, or summaries unless
  needed for the task.
- Do not commit generated benchmark data, local virtual environments, caches,
  or action logs.
- Be careful with symlink behavior when changing archive logic. The application
  intentionally creates symlinks at original locations after moving files.

## Tool-Specific Notes

- `CLAUDE.md` is a symlink to this file.
- Claude and Cursor shared skills and commands are symlinked from `.agents/`.
- Codex shared skills are symlinked from `.agents/`; do not add
  `.codex/commands`, because Codex CLI does not expose slash commands.
- `.codex/environments/`, if present in the future, is Codex-specific setup and
  should stay minimal, such as running `uv sync`.

## Agent Asset Maintenance

Shared skills and commands live under `.agents/`.

- Put portable skills in `.agents/skills/<skill-name>/SKILL.md`.
- Put shared command prompts in `.agents/commands/*.md`.
- Keep `.claude/skills`, `.claude/commands`, `.cursor/skills`,
  `.cursor/commands`, and `.codex/skills` as compatibility symlinks to the
  canonical `.agents/` folders.
- Before replacing a real tool-specific asset directory with a symlink, move or
  merge its useful files into `.agents/` and preserve filenames and content.
- Keep shared assets repo-neutral unless a skill or command explicitly
  documents this repository's workflow.
