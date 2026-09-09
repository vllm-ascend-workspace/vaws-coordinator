"""Session reservation client over HostQueue.

Durable container-bound sessions and the prepared-pool acquire path share
the host NPU coordinator database. This client does not allocate locally.
"""

from __future__ import annotations

from typing import Any

from vaws_coordinator.host.vaws_npu_coordination import (
    DEFAULT_CONTAINER_SSH_PORT_RANGE,
    DEFAULT_SERVING_PORT_RANGE,
    resolve_host_state_dir,
)
from vaws_coordinator.host_queue import HostQueue

UNSUPPORTED_RECEIPT = (
    "session has no coordinator receipt; recreate the session or reconcile host reservations"
)


class SessionResourceError(RuntimeError):
    """Raised when a session reservation request cannot be completed."""


def session_task_id(workspace_id: str, session_id: str) -> str:
    return f"session.{workspace_id}.{session_id}"


def remote_dev_host_run(target: dict[str, Any], command: str) -> str:
    """Run one host-queue command through remote-dev ``run_script``."""
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.ssh_transport import run_script

    if not target.get("host") or target.get("port") in (None, ""):
        raise RuntimeError("host coordination requires an explicit host and port")
    completed = run_script(
        resolve_endpoint({**target, "root": "/", "cwd": "/"}),
        command,
        timeout_ms=60000,
    )
    if completed.timed_out:
        raise RuntimeError(completed.stderr or "host coordination timed out")
    stdout = completed.stdout or ""
    if completed.returncode not in (0, None) and not stdout.strip():
        raise RuntimeError(
            f"host coordination failed (rc={completed.returncode}): {completed.stderr}"
        )
    return stdout


def as_host_endpoint(endpoint: Any) -> dict[str, Any]:
    if isinstance(endpoint, dict):
        return {
            "host": endpoint["host"],
            "port": int(endpoint.get("port", 22)),
            "user": str(endpoint.get("user") or "root"),
        }
    return {
        "host": endpoint.host,
        "port": int(endpoint.port),
        "user": str(getattr(endpoint, "user", None) or "root"),
    }


class SessionResourceClient:
    """Reserve, inspect, and release session NPUs and host ports."""

    def __init__(
        self,
        queue: HostQueue,
        host_endpoint: dict[str, Any] | Any,
        *,
        state_dir: str | None = None,
    ) -> None:
        self.queue = queue
        self.host_endpoint = as_host_endpoint(host_endpoint)
        self.state_dir = resolve_host_state_dir(state_dir)

    def _request(self, payload: dict[str, Any], *, allow_retained: bool = False) -> dict[str, Any]:
        request = dict(payload)
        request["state_dir"] = self.state_dir
        result = self.queue.request(self.host_endpoint, request)
        if result.get("status") == "waiting":
            raise SessionResourceError(
                result.get("reason") or result.get("error") or result["status"]
            )
        if result.get("status") == "retained" and not allow_retained:
            raise SessionResourceError(
                result.get("reason") or result.get("error") or "session reservation was retained"
            )
        return result

    def reserve(
        self,
        *,
        workspace_id: str,
        session_id: str,
        container_name: str | None = None,
        devices: list[int] | None = None,
        npu_count: int | None = None,
        container_ssh_port: int | None = None,
        container_ssh_port_range: str = DEFAULT_CONTAINER_SSH_PORT_RANGE,
        agent_id: str | None = None,
        agent_alias: str | None = None,
        task_id: str | None = None,
        coordination_epoch: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "action": "session-reserve",
            "workspace_id": workspace_id,
            "session_id": session_id,
            "agent_id": agent_id or workspace_id,
            "container_name": container_name,
            "task_id": task_id or session_task_id(workspace_id, session_id),
            "container_ssh_port_range": container_ssh_port_range,
        }
        if devices is not None:
            request["devices"] = devices
        if npu_count is not None:
            request["npu_count"] = npu_count
        if container_ssh_port is not None:
            request["container_ssh_port"] = container_ssh_port
        if agent_alias:
            request["agent_alias"] = agent_alias
        if coordination_epoch:
            request["coordination_epoch"] = coordination_epoch
        result = self._request(request)
        if result.get("status") != "reserved":
            raise SessionResourceError(result.get("error") or result.get("reason") or "session reserve failed")
        return result

    def inspect(
        self,
        *,
        task_id: str | None = None,
        coordination_epoch: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"action": "status", "no_probe": True}
        if task_id:
            request["task_id"] = task_id
        if coordination_epoch:
            request["coordination_epoch"] = coordination_epoch
        return self._request(request)

    def reserve_service_port(
        self,
        *,
        task_id: str,
        fence_token: int,
        coordination_epoch: str,
        workspace_id: str,
        session_id: str,
        requested_port: int | None = None,
        serving_port_range: str = DEFAULT_SERVING_PORT_RANGE,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "action": "service-port-reserve",
            "task_id": task_id,
            "fence_token": int(fence_token),
            "coordination_epoch": coordination_epoch,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "serving_port_range": serving_port_range,
        }
        if requested_port is not None:
            request["requested_port"] = requested_port
        result = self._request(request)
        if result.get("status") != "reserved":
            raise SessionResourceError(result.get("error") or "service port reserve failed")
        return result

    def release_service_port(
        self,
        *,
        task_id: str,
        fence_token: int,
        coordination_epoch: str,
        workspace_id: str,
        session_id: str,
        port: int,
    ) -> dict[str, Any]:
        return self._request(
            {
                "action": "service-port-release",
                "task_id": task_id,
                "fence_token": int(fence_token),
                "coordination_epoch": coordination_epoch,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "port": int(port),
            }
        )

    def release(
        self,
        *,
        task_id: str,
        fence_token: int,
        coordination_epoch: str,
        workspace_id: str,
        session_id: str,
        container_name: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "action": "session-release",
                "task_id": task_id,
                "fence_token": int(fence_token),
                "coordination_epoch": coordination_epoch,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "container_name": container_name,
            },
            allow_retained=True,
        )


def receipt_from_reserve(payload: dict[str, Any], *, workspace_id: str, session_id: str) -> dict[str, Any]:
    task = payload.get("task") or {}
    return {
        "task_id": task.get("task_id") or payload.get("task_id"),
        "fence_token": task.get("fence_token"),
        "coordination_epoch": payload.get("coordination_epoch"),
        "workspace_id": workspace_id,
        "session_id": session_id,
        "state_dir": payload.get("state_dir"),
    }
