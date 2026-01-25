"""Configuration constants and settings for the disk cleaner."""

from pathlib import Path
from datetime import timedelta

# Default age threshold for files to be considered "old" (6 months)
DEFAULT_AGE_THRESHOLD_MONTHS = 6
DEFAULT_AGE_THRESHOLD = timedelta(days=DEFAULT_AGE_THRESHOLD_MONTHS * 30)

# Common macOS cache directory patterns
CACHE_DIRECTORY_PATTERNS = [
    "**/Library/Caches/**",
    "**/Library/Application Support/**/Cache/**",
    "**/.cache/**",
    "**/tmp/**",
    "**/var/tmp/**",
    "**/var/folders/**",
]

# Cache file extensions
CACHE_FILE_EXTENSIONS = [
    ".cache",
    ".tmp",
    ".temp",
    ".log",
    ".old",
    ".bak",
]

# Directories to exclude from scanning
EXCLUDED_DIRECTORIES = [
    "/System",
    "/Library/Application Support/App Store",
    "/Library/Application Support/Apple",
    "/private",
    "/dev",
    "/proc",
    "/Volumes",
    "/.Trash",
    "/.fseventsd",
    "/.Spotlight-V100",
    "/.TemporaryItems",
    "/.DocumentRevisions-V100",
]

# Directories to exclude from user home
USER_EXCLUDED_DIRECTORIES = [
    "Library/Application Support/App Store",
    "Library/Application Support/Apple",
    "Library/Application Support/CallHistoryDB",
    "Library/Application Support/com.apple.TCC",
]

# Minimum file size to consider for moving (1 MB)
MIN_FILE_SIZE_TO_MOVE = 1024 * 1024

# Action log file
ACTION_LOG_FILE = Path.home() / ".mac-disk-cleaner-actions.log"
