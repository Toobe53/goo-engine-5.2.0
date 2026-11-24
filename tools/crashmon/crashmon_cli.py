#!/usr/bin/env python3
"""Launcher for the crashmon prototype.

Examples:
    python tools/crashmon/crashmon_cli.py doctor --json
    python tools/crashmon/crashmon_cli.py run --blender /path/to/blender -- --background file.blend
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from crashmon.cli import main  # noqa: E402  (path setup must happen first)

if __name__ == "__main__":
    raise SystemExit(main())
