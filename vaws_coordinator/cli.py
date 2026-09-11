"""Console entry for the local VAWS coordinator."""

from __future__ import annotations

import argparse
import getpass
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vaws-coordinator",
        description="Local VAWS coordinator: persistent environment, task isolation and resource placement.",
    )
    sub = parser.add_subparsers(dest="command")
    daemon = sub.add_parser("daemon", help="Run the persistent coordinator for this user/state-dir")
    daemon.add_argument("--state-dir", default="")
    daemon.add_argument("--action", choices=("serve", "status", "restart-if-idle"), default="serve")
    task = sub.add_parser("task-server", help="Serve the four VAWS task tools over stdio MCP")
    task.add_argument("--describe", action="store_true",
                      help="print the capability declaration and tool list as JSON, then exit")
    provision = sub.add_parser(
        "provision",
        help="Prepare this user's container vaws-<user> from a named image/recipe",
    )
    provision.add_argument("--host", required=True)
    provision.add_argument("--image", required=True, help="local-latest, rc, main, stable, or a full image reference")
    provision.add_argument("--user", default=getpass.getuser())
    provision.add_argument("--host-user", default="root")
    provision.add_argument("--host-port", type=int, default=22)
    provision.add_argument("--ssh-port", type=int)
    provision.add_argument("--machine-type", choices=("A2", "A3", "A5", "310P"))
    provision.add_argument("--password-env")
    register = sub.add_parser(
        "runtime-register",
        help="Adopt a prepared work root in this user's container (root==cwd)",
    )
    register.add_argument("--runtime-id", required=True)
    register.add_argument("--user", default=getpass.getuser())
    register.add_argument("--host", required=True)
    register.add_argument("--ssh-port", type=int, required=True)
    register.add_argument("--root", required=True, help="Absolute prepared work root inside the container")
    register.add_argument("--python", required=True)
    register.add_argument("--ssh-user", default="root")
    register.add_argument("--host-port", type=int, default=22)
    register.add_argument("--host-user", default="root")
    register.add_argument("--service-ports", default="")
    args = parser.parse_args(argv)
    if args.command == "daemon":
        from vaws_coordinator.service import main as daemon_main
        return daemon_main(["--state-dir", args.state_dir, "--action", args.action])
    if args.command == "task-server":
        from vaws_coordinator.task_server import main as task_main
        return task_main(["--describe"] if args.describe else [])
    if args.command == "provision":
        from vaws_coordinator.provision import provision_user_container
        row = provision_user_container(
            host=args.host, image=args.image, user=args.user,
            host_user=args.host_user, host_port=args.host_port,
            ssh_port=args.ssh_port, machine_type=args.machine_type,
            password_env=args.password_env,
        )
        print(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "runtime-register":
        from vaws_coordinator.backend import RemoteBackend
        from vaws_coordinator.ready_runtime import RuntimePool
        from vaws_coordinator.state_paths import coordinator_state_dir

        ports = [int(item) for item in args.service_ports.split(",") if item.strip()]
        spec = {
            "user": args.user,
            "python": args.python,
            "host_endpoint": {"host": args.host, "port": args.host_port, "user": args.host_user},
            "endpoint": {"host": args.host, "port": args.ssh_port, "user": args.ssh_user, "root": args.root},
            "service_ports": ports,
        }
        row = RuntimePool(coordinator_state_dir(), RemoteBackend()).register(args.runtime_id, spec)
        print(json.dumps({"runtime_id": row["id"], "user": row["user"], "container_name": row["container_name"],
                          "python": row["python"], "endpoint": row["endpoint"], "state": row["state"]},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
