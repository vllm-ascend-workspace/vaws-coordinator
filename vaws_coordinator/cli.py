"""Console entry for the local VAWS coordinator."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vaws-coordinator",
        description="Local VAWS coordinator: task tools and host NPU allocation for this user.",
    )
    sub = parser.add_subparsers(dest="command")
    task = sub.add_parser("task-server", help="Serve the four VAWS task tools over stdio MCP")
    task.add_argument(
        "--describe",
        action="store_true",
        help="print the capability declaration and tool list as JSON, then exit",
    )
    args = parser.parse_args(argv)
    if args.command == "task-server":
        from vaws_coordinator.task_server import main as task_main

        return task_main(["--describe"] if args.describe else [])
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
