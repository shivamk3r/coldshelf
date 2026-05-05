#!/usr/bin/env python3
"""Compatibility entry point for running the checkout directly."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from disk_space_manager.cli import cli


if __name__ == "__main__":
    cli()
