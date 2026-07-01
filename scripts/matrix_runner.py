#!/usr/bin/env python3
"""Production matrix entrypoint for maintained Machine Monitor debug gates."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "tools" / "developer" / "machine-code-monitor" / "monitor_debug_matrix_gate.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run named production matrices")
    parser.add_argument("--host", required=True)
    parser.add_argument("--rest-host", default="")
    parser.add_argument("--matrix", required=True, choices=("mcm-debug-final",))
    parser.add_argument("--require-cell-repetitions", type=int, required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rest_host = args.rest_host or args.host
    cmd = [
        sys.executable,
        str(GATE),
        "--host", args.host,
        "--rest-host", rest_host,
        "--artifact-dir", args.artifact_dir,
        "--memory", "all",
        "--ui", "all",
        "--reps", str(args.require_cell_repetitions),
        "--required-step-into-depth", "32",
        "--strict",
    ]
    if args.fail_fast:
        cmd.append("--fail-fast")
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
