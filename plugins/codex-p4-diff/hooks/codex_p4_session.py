#!/usr/bin/env python3
"""Plugin hook entry point for Codex P4 session changelist tracking."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def plugin_root() -> Path:
    configured = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


scripts = plugin_root() / "skills" / "codex-p4-diff" / "scripts"
sys.path.insert(0, str(scripts))

from codex_p4_audit import hook_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(hook_main())
