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
        wait=True,
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

    def preflight(self, binding, command, env):
        from vaws_coordinator.managed_execution import ExecutionRequestError, task_preamble
        script = "set -e\n" + "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items())
        script += "\n" + task_preamble(binding) + "\nexport VAWS_SERVICE_PORT=0\n" + command
        result = self.shell.run(binding["endpoint"], script, timeout_ms=120000)
        if result["outcome"] != "success":
            refs = result.get("refs") or {}
            detail = Path(refs["stderr"]).read_text(errors="replace") if refs.get("stderr") else str(result)
            cause = detail.strip().splitlines()[-1][-300:] if detail.strip() else result.get("summary", "no stderr")
            raise ExecutionRequestError(f"preflight failed before NPU allocation: {cause}\nlog refs: {refs}")

    def job_host_pid(self, runtime, receipt):
        code = '''
import json, subprocess
from pathlib import Path
request = json.loads(__import__('sys').argv[1])
info = json.loads(subprocess.check_output(['docker','inspect','--format','{{json .}}',request['container_name']], text=True, encoding="utf-8"))
if info['Id'] != request['container_id']:
    raise RuntimeError('container identity changed before activation')
receipt = request['receipt']
if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != receipt['boot_id']:
    raise RuntimeError('host boot identity changed')
rows = subprocess.check_output(['docker','top',request['container_name'],'-eo','pid'], text=True, encoding="utf-8").splitlines()[1:]
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
            from vaws_coordinator.parity_support import RemoteCommandError
            refs = result.get("refs") or {}
            stderr = Path(refs["stderr"]).read_text(errors="replace")[-300:].strip() if refs.get("stderr") else ""
            code = result.get("exit_code")
            message = (f"runtime probe failed ({result['outcome']}/{result.get('status')}, "
                       f"exit {code}): {stderr or 'no stderr'}")
            if type(code) is int and code > 0:
                raise RemoteCommandError(code, message)
            raise RuntimeError(message)
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
        if runtime.get('reuse_only'):
            observed = self.qualify_prepared_inputs(runtime, runtime['source_snapshot'], include_manifest=True)
            if not observed.get('qualified'):
                raise ValueError('artifact donor does not match fixed input: ' + observed.get('reason', 'unknown'))
            manifest = observed['manifest']
            return {**manifest, 'container_id': info['Id'],
                    'launch_preamble': launch_preamble(manifest['profile'], python=runtime['python'])}
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
if manifest['profile'].get('kind') != 'command' and _build_namespace["runtime_build_inputs"](root, manifest["profile"], manifest["profile_key"]) != manifest["build_inputs"]:
    raise ValueError("cache miss: installed native artifacts do not match current source inputs")
for name, expected in args["snapshots"].items():
    repo = root / name
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
    dirty = subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--name-only", "--ignore-submodules=dirty"], text=True, encoding="utf-8")
    untracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"], text=True, encoding="utf-8")
    private = {".vaws-runtime", ".remote-code-parity", "Mooncake"} if name == "." else set()
    extras = [path for path in untracked.split("\\0") if path and path.split("/", 1)[0] not in private]
    if head != expected or dirty.strip() or extras:
        raise ValueError("runtime source differs from pinned snapshot: " + name)
print(json.dumps(manifest))
'''
        view = runtime['endpoint'].get('cwd') or runtime['endpoint']['root']
        # Interpreter metadata must resolve the execution view's overlay too.
        prefix = 'export PYTHONPATH=' + shlex.quote(':'.join([view + '/.vaws-runtime/metadata', view + '/vllm', view + '/vllm-ascend'])) + '"${PYTHONPATH:+:$PYTHONPATH}"\n'
        command = (prefix + shlex.quote(python) + " - " + shlex.quote(request)
                   + " <<'VAWS_READY_PROBE'\n" + module + runner + "\nVAWS_READY_PROBE\n")
        manifest = json.loads(self.bash(runtime["endpoint"], command))
        return {**manifest, "container_id": info["Id"],
                "launch_preamble": launch_preamble(manifest["profile"], python=runtime.get("python"))}

    def qualify_prepared_inputs(self, runtime, source_snapshot, *, include_manifest=False):
        """Read-only qualification of a historical verified exact-source donor.

        Old registration is evidence, never a new execution. Its complete
        artifact hashes, environment and clean Git trees must still match.
        A differing source tree is not adopted by guessing build scope.
        """
        from vaws_coordinator.parity import validate_relative_posix_path
        records = source_snapshot.get('records', [])
        for record in records:
            validate_relative_posix_path(record['relpath'], label='artifact donor source')
        request = {'root': runtime['endpoint']['root'], 'records': records, 'include_manifest': include_manifest}
        module = _package_file('runtime_profile.py').read_text()
        build_source = _package_file('build_inputs.py').read_text()
        runner = "\n_build_namespace = {}\nexec(" + repr(build_source) + ", _build_namespace)\n" + r'''
import subprocess, sys
args = json.loads(sys.argv[1])
root = Path(args['root'])
manifest = json.loads((root / '.vaws-runtime/ready-profile.json').read_text())
verify(root, manifest)
if _build_namespace['runtime_build_inputs'](root, manifest['profile'], manifest['profile_key']) != manifest['build_inputs']:
    print(json.dumps({'qualified': False, 'reason': 'installed native artifacts do not match current source inputs'}))
    raise SystemExit(0)
build_info = 'vllm-ascend/vllm_ascend/_build_info.py'
if (root / build_info).is_file() and build_info not in manifest['files']:
    print(json.dumps({'qualified': False, 'reason': 'generated build metadata has no verified donor hash'}))
    raise SystemExit(0)
for row in args['records']:
    repo = root / row['relpath']
    tree = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(repo), 'diff', 'HEAD', '--name-only', '--ignore-submodules=dirty'], text=True).strip()
    untracked = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--others', '--exclude-standard'], text=True).strip()
    if tree != row['tree'] or dirty or untracked:
        print(json.dumps({'qualified': False, 'reason': 'source tree differs', 'source': row['relpath']}))
        raise SystemExit(0)
print(json.dumps({'qualified': True, 'build_key': manifest['build_key'], **({'manifest': manifest} if args.get('include_manifest') else {})}))
'''
        root = runtime['endpoint']['root']
        prefix = 'export PYTHONPATH=' + shlex.quote(':'.join([root + '/.vaws-runtime/metadata', root + '/vllm', root + '/vllm-ascend'])) + '"${PYTHONPATH:+:$PYTHONPATH}"'
        script = prefix + '\n' + shlex.quote(runtime['python']) + ' - ' + shlex.quote(json.dumps(request))
        script += " <<'VAWS_QUALIFY'\n" + module + runner + '\nVAWS_QUALIFY\n'
        return json.loads(self.bash(runtime['endpoint'], script))

    def command_environment(self, donor):
        """Resolve the real image interpreter without building an environment."""
        from vaws_coordinator.runtime_profile import command_launch_environment
        endpoint = donor.get('endpoint') or {}
        profile = donor.get('profile') or donor.get('attestation', {}).get('profile') or {}
        roots = [donor.get('root'), endpoint.get('root'), endpoint.get('cwd')]
        environment = command_launch_environment(profile.get('launch_env', {}), roots=roots)
        candidate = donor.get('python')
        preamble = '\n'.join('export ' + key + '=' + shlex.quote(value) for key, value in environment.items())
        if candidate:
            command = shlex.quote(candidate)
        else:
            preamble += '\nIMAGE_PYTHON="$(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V | tail -n 1)"\n'
            preamble += 'if [ -z "$IMAGE_PYTHON" ]; then IMAGE_PYTHON="$(command -v python3)"; fi\n'
            command = '"$IMAGE_PYTHON"'
        code = 'import os,sys; print(os.path.realpath(getattr(sys,"_base_executable",sys.executable)))'
        python = self.bash(endpoint, preamble + '\n' + command + ' -c ' + shlex.quote(code)).strip()
        if not python.startswith('/') or '\n' in python:
            raise ValueError('image interpreter discovery did not return an absolute path')
        return {'python': python, 'launch_env': environment}

    def prepare_task_root(self, spec, *, sources, environment, donor_python=None, workspace_root=None,
                          source_snapshot=None, reuse=None,
                          on_progress=None, log_dir=None, on_preparation_job=None, cancel_requested=None):
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
        from vaws_coordinator.parity_support import SshEndpoint, ssh_exec_stream
        from vaws_coordinator.provision.task_environment import INSTALL_STEPS, create_venv_script

        endpoint = spec["endpoint"]
        root = endpoint["root"]
        python = spec["python"]
        native_recipe = {"vllm", "vllm-ascend"}.issubset(sources or {})
        if native_recipe and donor_python and python == donor_python and not reuse:
            raise ValueError("task-owned interpreter must not be the donor interpreter")
        if source_snapshot is None:
            raise ValueError('preparation requires fixed execution source inputs')
        container = SshEndpoint(host=endpoint["host"], port=int(endpoint["port"]), user=endpoint["user"])
        def owned_process(step):
            if on_preparation_job is None:
                return None
            from vaws_coordinator.preparation_process import PreparationProcess
            return PreparationProcess(endpoint, step, on_preparation_job, cancel_requested or (lambda: False))

        def check_cancel():
            if cancel_requested is not None and cancel_requested():
                from vaws_coordinator.preparation_process import PreparationCancelled
                raise PreparationCancelled("preparation cancelled between steps")

        def progress(step, event=None):
            value = {"step": step, **(event or {})}
            if log_dir is not None:
                value["log_ref"] = str(Path(log_dir) / (step + ".log"))
            if on_progress is not None:
                on_progress(value)
            return value.get("log_ref")

        if log_dir is not None:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
        scripts = [("prepare-root", prepare_isolated_root_script(root))]
        if (native_recipe and (not reuse or reuse['kind'] != 'native')) or (not native_recipe and not donor_python):
            scripts.append(("create-venv", create_venv_script(root, python, donor_python)))
        for step, script in scripts:
            check_cancel()
            log = progress(step)
            ssh_exec_stream(container, script, stream_progress=False, log_path=log,
                            process=owned_process(step) if step != "prepare-root" else None)
        identity = spec.get("container_name") or ("vaws-" + spec["user"])
        args = materialize_command(
            workspace_id=identity,
            runtime_id=identity,
            endpoint=endpoint,
            sources=sources,
            workspace_root=workspace_root,
            source_snapshot=source_snapshot,
        ) if sources else None
        env = {key: value for key, value in os.environ.items()}
        log = progress("materialize")
        if args is None:
            result = subprocess.CompletedProcess([], 0)
        elif log:
            with Path(log).open("w") as stream:
                result = subprocess.run(args, env=env, timeout=3600, check=False, stdout=stream, stderr=stream)
        else:
            result = subprocess.run(args, env=env, timeout=3600, check=False, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            detail = f"inspect {log}" if log else (result.stderr or result.stdout or "")
            raise RuntimeError(f"source materialization failed: {detail}")
        check_cancel()
        versions = {record['relpath']: {'version': record.get('scm_version'), 'source_head': record.get('source_head')}
                    for record in source_snapshot.get('records', []) if record['relpath'] in ('vllm', 'vllm-ascend')}
        if native_recipe:
            if any(not row['version'] for row in versions.values()):
                raise ValueError('native preparation requires captured source SCM versions')
            preparation_data = {'versions': versions, 'build_env': source_snapshot.get('build_env', {})}
            write = 'mkdir -p ' + shlex.quote(root + '/.vaws-runtime') + '\nprintf %s ' + shlex.quote(json.dumps(preparation_data)) + ' > ' + shlex.quote(root + '/.vaws-runtime/build-source.json')
            self.bash(endpoint, write)
        if reuse:
            from vaws_coordinator.preparation_cache import REMOTE_REUSE_SUFFIX
            previous = reuse['runtime']
            request = {'kind': reuse['kind'], 'root': root, 'source_root': previous['endpoint']['root'],
                       'python': python, 'versions': versions}
            module = _package_file('runtime_profile.py').read_text()
            cache_source = _package_file('preparation_cache.py').read_text()
            previous_env = launch_preamble(previous['attestation']['profile'], python=previous['python'])
            script = (previous_env + '\n' + shlex.quote(previous['python']) + ' - ' + shlex.quote(json.dumps(request))
                      + " <<'VAWS_REUSE'\n" + module + '\nexec(' + repr(cache_source) + ', globals())\n' + REMOTE_REUSE_SUFFIX + '\nVAWS_REUSE\n')
            log = progress('reuse-' + reuse['kind'])
            ssh_exec_stream(container, script, stream_progress=False, log_path=log,
                            process=owned_process('reuse-' + reuse['kind']))
        steps = () if not native_recipe or (reuse and reuse['kind'] == 'native') else INSTALL_STEPS
        if reuse and reuse['kind'] == 'dependencies':
            steps = tuple(step for step in steps if step != 'install-vllm-ascend-requirements')
        for step in steps:
            log = progress(step)
            run_runtime_install_step(
                container=container,
                runtime_root=root,
                marker_dirname=DEFAULT_MARKER_DIRNAME,
                container_identity=identity,
                step=step,
                stream_progress=False,
                python=python,
                on_progress=lambda event, step=step: progress(step, event),
                log_path=log,
                process=owned_process(step),
            )
        check_cancel()
        log = progress("verify-profile")
        self._write_ready_profile(spec, environment, native_recipe=native_recipe, source_versions=versions,
                                  process=owned_process("verify-profile"), log_path=log)
        # The caller immediately registers this root, which performs the full
        # container/profile/source attestation. No caller consumes a second
        # copy of that probe here.

    def _write_ready_profile(self, spec, environment, *, native_recipe=True, source_versions=None,
                             process=None, log_path=None):
        from vaws_coordinator.prepare_runtime import (
            CANN_VERSION_CANDIDATES,
            DRIVER_VERSION_CANDIDATES,
            REMOTE_CAPTURE_SUFFIX,
            REMOTE_COMMAND_CAPTURE_SUFFIX,
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
            'preparation': spec.get('preparation', {}),
            'source_id': spec.get('source_snapshot', {}).get('id'),
            'source_versions': source_versions or {},
            'build_env': spec.get('source_snapshot', {}).get('build_env', {}),
            'excluded_roots': spec.get('excluded_launch_roots', []),
        })
        from vaws_coordinator.parity import DEFAULT_ENV_PREAMBLE, task_python_exports

        suffix = REMOTE_CAPTURE_SUFFIX if native_recipe else REMOTE_COMMAND_CAPTURE_SUFFIX
        runner = "\n_build_namespace = {}\nexec(" + repr(build_source) + ", _build_namespace)\n" + suffix
        preamble_lines = ["set -euo pipefail"]
        if native_recipe:
            preamble_lines.extend(['export VAWS_RUNTIME_ROOT=' + shlex.quote(root), *DEFAULT_ENV_PREAMBLE, *task_python_exports(python),
                'export PYTHONPATH=' + shlex.quote(':'.join([root + '/.vaws-runtime/metadata', root + '/vllm', root + '/vllm-ascend'])) + '"${PYTHONPATH:+:$PYTHONPATH}"'])
        else:
            # The image's recorded launch settings can be reused without
            # sourcing CANN/toolchain installers for an ordinary command.
            preamble_lines.append('unset PYTHONPATH ASCEND_CUSTOM_OPP_PATH')
            for key, value in spec.get('donor_launch_env', {}).items():
                preamble_lines.append('export ' + key + '=' + shlex.quote(value))
            preamble_lines.append('export PATH=' + shlex.quote(str(__import__('pathlib').PurePosixPath(python).parent)) + '"${PATH:+:$PATH}"')
        preamble = '\n'.join(preamble_lines)
        command = (preamble + "\n" + shlex.quote(python) + " - " + shlex.quote(request)
                   + " <<'VAWS_CAPTURE_PROBE'\n" + module + runner + "\nVAWS_CAPTURE_PROBE\n")
        if process is None:
            self.bash(spec["endpoint"], command)
        else:
            from vaws_coordinator.parity_support import SshEndpoint, ssh_exec_stream
            endpoint = spec["endpoint"]
            ssh_exec_stream(SshEndpoint(endpoint["host"], int(endpoint["port"]), endpoint["user"]),
                            command, stream_progress=False, process=process, log_path=log_path)
