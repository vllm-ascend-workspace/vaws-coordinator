"""Prepare the per-user container and a task work root from a named recipe.

This package owns bootstrap/image/smoke. Consumers pass host/image/user;
they do not import workspace machine-management scripts.
"""

from __future__ import annotations

import getpass
import json
from typing import Any

from vaws_coordinator.machine_directory import MachineDirectory
from vaws_coordinator.provision import host_ops
from vaws_coordinator.provision.task_environment import (  # noqa: F401
    TaskRootBusy,
    isolated_python,
    isolated_root,
    prepare_task_environment,
    task_runtime_id,
)
from vaws_coordinator.ready_runtime import safe_id, user_container_name

DEFAULT_WORKDIR = host_ops.DEFAULT_WORKDIR


def task_work_root(session_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in session_id)[:80]
    return f"{DEFAULT_WORKDIR}/tasks/{safe}"


def provision_user_container(
    *,
    host: str,
    image: str,
    user: str | None = None,
    host_user: str = host_ops.DEFAULT_HOST_USER,
    host_port: int = host_ops.DEFAULT_HOST_PORT,
    ssh_port: int | None = None,
    machine_type: str | None = None,
    password_env: str | None = None,
    machines: MachineDirectory | None = None,
    reserve_port=None,
) -> dict[str, Any]:
    """Create or reuse `vaws-<user>` on `host` from a named image/recipe."""
    user = safe_id(user or getpass.getuser())
    container = user_container_name(user)
    if not image or image in getattr(host_ops, "LEGACY_IMAGE_SELECTORS", {"auto"}):
        raise ValueError(
            "provision requires an explicit image selector: local-latest, rc, main, stable, "
            "or a full image reference"
        )
    host_target = host_ops.SshTarget(host=host, user=host_user, port=host_port)
    if password_env:
        args = _namespace(
            host=host, user=host_user, host_port=host_port, public_key_file=None,
            password=None, password_env=password_env, password_stdin=False, print_command=False,
        )
        if host_ops.cmd_bootstrap_host_key(args) != 0:
            raise host_ops.MachineManagementError("host key bootstrap failed")
    image_request = host_ops.image_request_payload(image, machine_type=machine_type)
    probe = host_ops.run_remote_script(
        host_target,
        host_ops.render_host_probe_script(),
        args=[json.dumps(image_request, ensure_ascii=False), host_ops.DEFAULT_PORT_RANGE, "vaws-"],
        timeout_seconds=host_ops.DEFAULT_PROBE_TIMEOUT_SECONDS,
        stream_progress=False,
    )
    probe_payload = host_ops.assert_remote_success(probe, require_payload=True)
    chosen_port = int(ssh_port or probe_payload.get("suggested_port") or probe_payload.get("ssh_port")
                      or probe_payload.get("container_ssh_port") or 0)
    if chosen_port <= 0:
        raise host_ops.MachineManagementError("host probe did not offer a container SSH port")
    if reserve_port is not None:
        reserved = reserve_port(user=user, container_name=container, port=chosen_port)
        chosen_port = int(reserved.get("port") or chosen_port)
    key_path = host_ops.find_public_key(None)
    public_key = host_ops.load_public_key(key_path)
    boot = host_ops.run_remote_script(
        host_target,
        host_ops.render_bootstrap_host_script(),
        args=[
            container,
            str(chosen_port),
            json.dumps(image_request, ensure_ascii=False),
            DEFAULT_WORKDIR,
            public_key,
            user,
            "false",
            machine_type or "",
            "",
        ],
        timeout_seconds=host_ops.DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
        stream_progress=False,
    )
    host_ops.assert_remote_success(boot)
    container_target = host_ops.SshTarget(host=host, user="root", port=chosen_port)
    smoke = host_ops.run_remote_script(
        container_target,
        host_ops.render_smoke_script(),
        args=[""],
        timeout_seconds=host_ops.DEFAULT_SMOKE_TIMEOUT_SECONDS,
        stream_progress=False,
    )
    host_ops.assert_remote_success(smoke, require_payload=True)
    record = {
        "alias": host,
        "host": {"ip": host, "port": host_port, "user": host_user, "machine_type": machine_type},
        "container": {"name": container, "ssh_port": chosen_port, "user": "root", "workdir": DEFAULT_WORKDIR},
        "image": {"requested": image},
        "user": user,
    }
    directory = machines or MachineDirectory()
    directory.upsert_machine(record)
    return {
        "user": user,
        "container_name": container,
        "host": host,
        "ssh_port": chosen_port,
        "image": image,
        "workdir": DEFAULT_WORKDIR,
        "state": "ready",
    }


def _namespace(**values):
    return type("Args", (), values)()
