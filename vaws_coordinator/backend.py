"""Adapters to the remote-dev package and host NPU device authority.

remote-dev is imported as the `remote_dev` package. The host queue remains
the single device-allocation authority. Generic process control is
`remote_dev.processes.control`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from remote_dev.core.endpoint import resolve_endpoint
from remote_dev.core.shell_ops import remote_bash

from vaws_coordinator.host_queue import HostQueue
from vaws_coordinator.machine_directory import MachineDirectory
from vaws_coordinator.runtime_profile import launch_preamble


def _package_file(relative: str) -> Path:
    return Path(__file__).resolve().parent / relative


class RemoteDev:
    """Explicit-endpoint shell via the installed `remote_dev` package."""

    def run(self, target: dict, command: str, *, timeout_ms: int = 45000) -> dict:
        return remote_shell(target, command, timeout_ms=timeout_ms)


def remote_shell(target: dict, command: str, *, timeout_ms: int = 45000) -> dict:
    """Run one command through remote-dev's explicit-endpoint shell."""
    if not target.get("host") or not target.get("port"):
        raise ValueError("remote-dev calls require an explicit host and port")
    return remote_bash(
        resolve_endpoint(dict(target)),
        command=command,
        timeout_ms=timeout_ms,
        runtime_env=False,
    )["result"]


class RemoteBackend:
    def __init__(self, *, shell=None, host_queue=None, machines=None, host_queue_module=None):
        self.shell = shell or RemoteDev()
        self.machines = machines or MachineDirectory()
        self.host_queue = host_queue or HostQueue(self.bash, module_path=host_queue_module)

    def job(self, runtime, job_id, action, **parameters):
        from remote_dev.processes import control

        endpoint = dict(runtime["endpoint"])
        return control(resolve_endpoint(endpoint), job_id, action, **parameters)

    def job_host_pid(self, runtime, receipt):
        code = '''
import json, subprocess
from pathlib import Path
request = json.loads(__import__('sys').argv[1])
info = json.loads(subprocess.check_output(['docker','inspect','--format','{{json .}}',request['container_name']], text=True))
if info['Id'] != request['container_id']:
    raise RuntimeError('container identity changed before activation')
receipt = request['receipt']
if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != receipt['boot_id']:
    raise RuntimeError('host boot identity changed')
rows = subprocess.check_output(['docker','top',request['container_name'],'-eo','pid'], text=True).splitlines()[1:]
matches = []
for row in rows:
    pid = int(row.strip())
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()
        status = Path(f'/proc/{pid}/status').read_text().splitlines()
        namespace = next(line.split()[1:] for line in status if line.startswith('NSpid:'))
        if int(namespace[-1]) == receipt['pid'] and fields[19] == receipt['start_ticks'] and fields[0] != 'Z':
            matches.append(pid)
    except FileNotFoundError:
        continue
if len(matches) != 1:
    raise RuntimeError('cannot identify a unique host PID for the waiting supervisor')
print(json.dumps({'pid':matches[0]}))
'''
        request = {"container_name": runtime["container_name"], "container_id": runtime["attestation"]["container_id"], "receipt": receipt}
        command = "python3 - " + shlex.quote(json.dumps(request)) + " <<'VAWS_HOST_PID'\n" + code + "\nVAWS_HOST_PID\n"
        return json.loads(self.bash({**runtime["host_endpoint"], "root": "/", "cwd": "/"}, command))["pid"]

    def catalog(self):
        return self.machines.catalog()

    def resolve_registration(self, spec):
        if "machine" not in spec:
            return spec
        host = self.machines.host(spec["machine"])
        user = spec["user"]
        return {"user": user, "python": spec["python"],
                "host_endpoint": {"host": host["ip"], "port": host.get("port", 22), "user": host.get("user", "root")},
                "endpoint": {"host": host["ip"], "port": spec["port"], "root": spec["root"],
                             "cwd": spec.get("cwd") or spec["root"], "user": spec.get("ssh_user", "root")},
                "container_name": spec.get("container_name") or ("vaws-" + user),
                "service_ports": spec.get("service_ports", [])}

    def host(self, runtime, request):
        return self.host_queue.request(runtime["host_endpoint"], request)

    def bash(self, target, command):
        result = self.shell.run(target, command, timeout_ms=45000)
        if result["outcome"] != "success":
            refs = result.get("refs") or {}
            stderr = Path(refs["stderr"]).read_text(errors="replace")[-300:].strip() if refs.get("stderr") else ""
            raise RuntimeError(f"runtime probe failed ({result['outcome']}/{result.get('status')}, "
                               f"exit {result.get('exit_code')}): {stderr or 'no stderr'}")
        return Path(result["refs"]["stdout"]).read_text()

    def inspect(self, runtime, *, idle=False, snapshots=None):
        # idle remains an inspect of the selected prepared root and container
        # identity. It must not require the whole user container to be empty:
        # sibling roots may have authorized executions.
        del idle
        host = {**runtime["host_endpoint"], "root": "/", "cwd": "/"}
        name = shlex.quote(runtime["container_name"])
        fields = shlex.quote('{"Id":{{json .Id}},"State":{{json .State}}}')
        info = json.loads(self.bash(host, f"docker inspect --format {fields} {name}"))
        if not info["State"]["Running"] or info["State"].get("Paused") or info["State"].get("Restarting"):
            raise RuntimeError("prepared container is not running normally")
        python = runtime.get("python") or "python3"
        module = _package_file("runtime_profile.py").read_text()
        request = json.dumps({"root": runtime["endpoint"].get("cwd") or runtime["endpoint"]["root"],
                              "snapshots": snapshots or {}})
        build_source = _package_file("build_inputs.py").read_text()
        runner = "\n_build_namespace = {}\nexec(" + repr(build_source) + ", _build_namespace)\n" + '''
import subprocess
import sys
args = json.loads(sys.argv[1])
root = Path(args["root"])
manifest = json.loads((root / ".vaws-runtime/ready-profile.json").read_text())
verify(root, manifest)
if _build_namespace["runtime_build_inputs"](root, manifest["profile"], manifest["profile_key"]) != manifest["build_inputs"]:
    raise ValueError("cache miss: installed native artifacts do not match current source inputs")
for name, expected in args["snapshots"].items():
    repo = root / name
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--name-only", "--ignore-submodules=dirty"], text=True)
    untracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"], text=True)
    private = {".vaws-runtime", ".remote-code-parity", "Mooncake"} if name == "." else set()
    extras = [path for path in untracked.split("\\0") if path and path.split("/", 1)[0] not in private]
    if head != expected or dirty.strip() or extras:
        raise ValueError("runtime source differs from pinned snapshot: " + name)
print(json.dumps(manifest))
'''
        command = (shlex.quote(python) + " - " + shlex.quote(request)
                   + " <<'VAWS_READY_PROBE'\n" + module + runner + "\nVAWS_READY_PROBE\n")
        manifest = json.loads(self.bash(runtime["endpoint"], command))
        return {**manifest, "container_id": info["Id"],
                "launch_preamble": launch_preamble(manifest["profile"], python=runtime.get("python"))}

    def prepare_task_root(self, spec, *, sources, environment, donor_python=None, workspace_root=None):
        """First-install isolated sources + task venv + editables + verified profile.

        Does not mutate ``donor_python`` site-packages. Image packages may be
        reused via ``venv --system-site-packages``.
        """
        from vaws_coordinator.parity import (
            DEFAULT_MARKER_DIRNAME,
            materialize_command,
            prepare_isolated_root_script,
            run_runtime_install_step,
        )
        from vaws_coordinator.parity_support import SshEndpoint, ssh_exec
        from vaws_coordinator.provision.task_environment import INSTALL_STEPS, create_venv_script

        endpoint = spec["endpoint"]
        root = endpoint["root"]
        python = spec["python"]
        if donor_python and python == donor_python:
            raise ValueError("task-owned interpreter must not be the donor interpreter")
        if not {"vllm", "vllm-ascend"}.issubset(sources or {}):
            raise ValueError("bind the actual vllm and vllm-ascend worktrees before preparation")
        container = SshEndpoint(host=endpoint["host"], port=int(endpoint["port"]), user=endpoint["user"])
        ssh_exec(container, prepare_isolated_root_script(root))
        ssh_exec(container, create_venv_script(root, python, donor_python))
        identity = spec.get("container_name") or ("vaws-" + spec["user"])
        args = materialize_command(
            workspace_id=identity,
            runtime_id=identity,
            endpoint=endpoint,
            sources={name: sources[name] for name in ("vllm", "vllm-ascend")},
            workspace_root=workspace_root,
        )
        env = {key: value for key, value in os.environ.items()}
        result = subprocess.run(args, env=env, timeout=3600, check=False, capture_output=True, text=True)
        if result.returncode:
            detail = (result.stderr or result.stdout or "")[-500:]
            raise RuntimeError(f"source materialization failed: {detail}")
        for step in INSTALL_STEPS:
            run_runtime_install_step(
                container=container,
                runtime_root=root,
                marker_dirname=DEFAULT_MARKER_DIRNAME,
                container_identity=identity,
                step=step,
                stream_progress=False,
                python=python,
            )
        self._write_ready_profile(spec, environment)
        return self.inspect(spec)

    def _write_ready_profile(self, spec, environment):
        from vaws_coordinator.prepare_runtime import (
            CANN_VERSION_CANDIDATES,
            DRIVER_VERSION_CANDIDATES,
            REMOTE_CAPTURE_SUFFIX,
        )

        python = spec["python"]
        root = spec["endpoint"]["root"]
        host = {**spec["host_endpoint"], "root": "/", "cwd": "/"}
        name = shlex.quote(spec["container_name"])
        digest = self.bash(host, f"docker inspect --format '{{{{json .Image}}}}' {name}").strip().strip('"')
        if not digest:
            raise ValueError("cannot attest image digest")
        recipe = (environment or {}).get("recipe") or (environment or {}).get("image") or spec.get("recipe")
        module = _package_file("runtime_profile.py").read_text()
        build_source = _package_file("build_inputs.py").read_text()
        request = json.dumps({
            "root": root,
            "recipe": recipe,
            "image_digest": digest,
            "machine_type": (environment or {}).get("machine_type") or spec.get("machine_type"),
            "cann_files": list(CANN_VERSION_CANDIDATES),
            "driver_files": list(DRIVER_VERSION_CANDIDATES),
        })
        from vaws_coordinator.parity import DEFAULT_ENV_PREAMBLE, task_python_exports

        runner = "\n_build_namespace = {}\nexec(" + repr(build_source) + ", _build_namespace)\n" + REMOTE_CAPTURE_SUFFIX
        preamble = "\n".join([
            "set -euo pipefail", *DEFAULT_ENV_PREAMBLE, *task_python_exports(python),
        ])
        command = (preamble + "\n" + shlex.quote(python) + " - " + shlex.quote(request)
                   + " <<'VAWS_CAPTURE_PROBE'\n" + module + runner + "\nVAWS_CAPTURE_PROBE\n")
        self.bash(spec["endpoint"], command)
