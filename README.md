# Mac Disk Space Manager

A Python CLI tool to help manage disk space on macOS by analyzing disk usage, identifying removable cache files, and finding old files that can be moved to an external SSD.

## Features

- **Disk Usage Analysis**: Scan and visualize disk usage with detailed breakdowns
- **Cache File Detection**: Automatically identify cache and temporary files that can be safely removed
- **Old File Archiving**: Find files not accessed in 6+ months and move them to an external SSD or a local folder
- **Safety First**: Requires explicit confirmation before any destructive actions
- **Beautiful CLI**: Rich terminal UI with progress bars, tables, and color-coded output
- **Auto-Detection**: Automatically detects external SSDs for archiving
- **Action Logging**: All actions are logged for review and audit

## Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd mac-disk-cleaner
```

2. Sync dependencies (installs Python if needed, creates a virtualenv, and installs packages):
```bash
uv sync
```

That's it. If you don't have `uv` yet, install it with:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Usage

### Analyze Disk Usage

Scan and analyze disk usage without making any changes:

```bash
uv run python main.py analyze
```

Scan a specific directory:

```bash
uv run python main.py analyze --path /path/to/directory
```

### Clean Cache Files

Identify and remove cache files (with confirmation):

```bash
uv run python main.py clean
```

With custom age threshold:

```bash
uv run python main.py clean --age-months 3
```

### Archive Old Files

Move old files (not accessed in 6+ months) to an external SSD or a local folder:

```bash
uv run python main.py archive
```

Archive to a local folder:

```bash
uv run python main.py archive --target-path /path/to/archive/folder
```

Archive to a specific external SSD:

```bash
uv run python main.py archive --ssd-path /Volumes/MySSD
```

With custom age threshold:

```bash
uv run python main.py archive --age-months 12 --target-path ./my_archive
```

### Full Report

Generate a comprehensive report showing all insights:

```bash
uv run python main.py full-report
```

### Dry Run Mode

Preview what would be done without making changes:

```bash
uv run python main.py clean --dry-run
uv run python main.py archive --dry-run
```

## Commands

- `analyze` - Analyze disk usage and show insights
- `clean` - Identify and remove cache files
- `archive` - Move old files to external SSD or local folder
- `full-report` - Generate comprehensive analysis report

## Options

- `--path PATH` - Directory to scan (default: home directory)
- `--target-path PATH` - Local folder to use as archive destination
- `--ssd-path PATH` - Path to external SSD (default: auto-detect)
- `--age-months N` - Age threshold in months (default: 6)
- `--dry-run` - Show what would be done without making changes

## Safety Features

1. **Confirmation Prompts**: All destructive actions require explicit user confirmation
2. **Preview Mode**: See what will be affected before confirming
3. **Action Logging**: All actions are logged to `~/.mac-disk-cleaner-actions.log`
4. **Dry Run**: Test operations without making changes
5. **Error Handling**: Graceful handling of permission errors and inaccessible files

## How It Works

### Cache File Detection

The tool identifies cache files by:
- Common cache directory patterns (Library/Caches, .cache, tmp, etc.)
- Cache file extensions (.cache, .tmp, .temp, .log, etc.)
- Filenames containing cache-related keywords

### Old File Detection

Files are considered "old" if they:
- Haven't been accessed in the specified time period (default: 6 months)
- Are larger than 1 MB (to avoid moving many small files)

### Archiving Process

When archiving files to an external SSD or local folder:
1. Files are moved to the target location preserving directory structure
2. Symlinks are created in the original location pointing to the archived files
3. This allows applications to continue working while freeing up space
4. If the archive target is inside the scanned directory, it is automatically excluded from scanning to prevent re-archiving

## Configuration

Default settings can be modified in `config.py`:
- `DEFAULT_AGE_THRESHOLD_MONTHS`: Default age for old files (6 months)
- `CACHE_DIRECTORY_PATTERNS`: Patterns for cache directories
- `CACHE_FILE_EXTENSIONS`: File extensions considered cache files
- `EXCLUDED_DIRECTORIES`: Directories excluded from scanning

## Requirements

- Python 3.9+
- macOS (uses macOS-specific tools like `diskutil`)
- External SSD or local folder (for archiving feature)
- [uv](https://docs.astral.sh/uv/) (for dependency management)

## Action Log

All actions (deletions, moves) are logged to `~/.mac-disk-cleaner-actions.log` with timestamps, file paths, sizes, and success/failure status.

## Limitations

- Requires appropriate file permissions
- Some system files may be inaccessible
- External SSD must be mounted and writable (when using `--ssd-path`)
- Large scans may take time depending on disk size

## License

MIT License

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
