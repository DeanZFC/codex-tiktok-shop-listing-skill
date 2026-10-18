#!/usr/bin/env python3
"""Backward-compatible wrapper for the unified Windows installer."""
from install_workflow_windows import main


if __name__ == "__main__":
    raise SystemExit(main())
