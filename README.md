# vaws-coordinator

Local-process coordinator for one user's remote Ascend containers and host NPU
allocation. It is not a hosted multi-user service.

Install it, run it on the machine you are sitting at, and it talks to *your*
remote containers through the `vaws-remote-dev` package. Code identity is git.

## Status observations

Task MCP/CLI execution status may reuse a managed-job snapshot for up to two
seconds. `vaws execution --refresh` (or tool argument `refresh: true`) requests
a new status observation. Replies include `observation_freshness` with snapshot
completion time, age, freshness, source and whether a busy execution deferred
refresh. Per-role `status_observed_at` preserves individual sampling times;
roles are sampled sequentially. The existing top-level `observed_at` is the
response-generation time, not proof of a new remote query.

`TaskClient.observe()` preserves its fresh-by-default library behavior; pass
`refresh=False` to permit the short status cache. Tail, target, stop, resource
allocation and background progression retain their existing behavior. Cached
observations neither allocate resources nor establish new ownership. A busy
execution returns its stored observation immediately and explicitly marks a
requested refresh as deferred. Stale or missing timestamps trigger a refresh
when the execution is available; state/error/release fields remain visible.

## Install

```bash
uv pip install git+https://github.com/vllm-ascend-workspace/vaws-coordinator@main
```

Or run without a permanent install:

```bash
uvx --from git+https://github.com/vllm-ascend-workspace/vaws-coordinator@main vaws-coordinator task-server
```

Replace `@main` with a commit or tag when you pin. `python -m vaws_coordinator`
is the same entry as `vaws-coordinator`.

The package depends on `vaws-remote-dev>=0.5.1` (import `remote_dev`). It
does not pin that package's git source; the workspace that installs this
library chooses the tag. `uv sync` / `uv lock` are not the developer path
here: a library that named remote-dev's git source in `pyproject.toml`
would pin every consumer to that tag.

## Start the task server

`vaws-coordinator task-server` serves the four task tools over stdio MCP:

`vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish`.

Example Cursor / Claude `.mcp.json` (or `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "vaws-coordinator": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vllm-ascend-workspace/vaws-coordinator@main",
        "vaws-coordinator",
        "task-server"
      ]
    }
  }
}
```

Start `vaws-coordinator daemon` for this user/state-dir. `task-server`, the
`vaws` CLI, and `TaskClient` call that process. They do not each create a
private scheduler.

`vaws_session` exposes the local task identity. `vaws_finish` closes admission
and stops the task's owned executions; the daemon then returns remaining
bindings and marks the task finished without a second finish.
`vaws_run` admits a business command plus environment/resource/topology needs;
the daemon places, prepares, launches and observes it. Pass the `context_file` supplied by the
native session hook; never guess a task from cwd or history. Do not pass
request IDs, profile hashes, or runtime IDs.

Managed launches prepend their verified task-local `vllm` and `vllm-ascend`
source directories to Python's import path. This prevents repository directories
in the task cwd from shadowing editable packages, while preserving the CANN
and other support paths already supplied by the environment.

For serving, `service_port=0` asks the host coordinator to select a free port.
If a task runtime has no declared service ports, automatic selection uses the
host's default serving range (30000–45999). A nonempty declaration restricts
selection to those ports; an explicit port must be declared. Both paths check
listening sockets and existing leases, and release ports with the execution.

## Host NPU queue (Python API)

The scaffold imports the public allocation surface from one place:

```python
from vaws_coordinator.host_queue import (
    HostQueue,
    HostQueueUnavailable,
    SCHEMA_VERSION,
    CoordinationError,
    handle_request,
    host_queue_module_path,
    load_host_protocol,
)
```

`host/vaws_npu_coordination.py` stays stdlib-only. It is shipped over SSH and
executed on the physical host. Durable host state defaults to
`/tmp/vaws-npu-coordinator/v1/` and is overridden by
`VAWS_NPU_COORDINATOR_STATE_DIR` or `request["state_dir"]`.
`VAWS_HOST_QUEUE_MODULE` overrides the bundled file.

## Layout

| Path | Role |
| --- | --- |
| `vaws_coordinator/cli.py` | `vaws-coordinator` / `python -m vaws_coordinator` |
| `vaws_coordinator/task_server.py` | Stdio MCP for the four task tools |
| `vaws_coordinator/host_queue.py` | Public host NPU API |
| `vaws_coordinator/host/` | Host allocation module, shipped to the host |
| `vaws_coordinator/backend.py` | Container/host probes via `remote_dev` |
| `vaws_coordinator/prepare_runtime.py` | In-container attest / publish / restore |
| `vaws_coordinator/workers/` | Linux supervisor source, shipped into a container |
| `vaws_coordinator/run_manifest.py` | Run Manifest v1 (Git identity in `code`) |
| `vaws_coordinator/code_identity.py` | Git snapshot identity for manifests |
| `vaws_coordinator/parity.py` | Working-tree snapshot and remote materialization |
| `vaws_coordinator/machine_directory.py` | Coordinator-owned machine directory |

## Development

`uv sync` is not the setup path. This library declares
`vaws-remote-dev>=0.5.1` without a git source: remote-dev is not on PyPI,
so `uv sync` / `uv lock` fail with an unsatisfiable-dependency error.
That is intentional. A library that pinned remote-dev's git URL would
take the upgrade decision away from every consumer, and
`constraint-dependencies` cannot carry a git URL.

Install the git source first, then this tree without resolving
dependencies from an index:

```bash
uv venv
uv pip install "vaws-remote-dev @ git+https://github.com/vllm-ascend-workspace/remote-dev@9120004fd30967c38b35965af7d2b0cbae9a6809"
uv pip install pytest
uv pip install -e . --no-deps
.venv/bin/python -m pytest
```

`uv venv` is the first command so an unreadable project config fails
before install. Do not add `[tool.uv.sources]` for remote-dev: that table
travels to consumers. Requires Python 3.11+.

On native Windows, run the same setup commands and use
`.venv/Scripts/python.exe -m pytest tests`. The local daemon uses a locked state
directory and token-authenticated IPv4 loopback IPC; its listener is never bound
to an external interface. Native CLI pipes and Git output use UTF-8. Source
publication preserves Linux path syntax independently of the client platform.
The tests that emulate a Linux peer need a working Bash. If the Windows `bash`
alias points at an unconfigured WSL installation, prepend Git for Windows'
`bin` directory to the test process's `PATH`, as the Windows CI job does.

## Progress, records, and loaded versions

Execution observations include the active preparation/sync/preflight step,
its timestamps and log reference. Installation heartbeat events reach status
while compilation is in progress. Full install logs remain in the task root;
role errors, lease state and descendant quietness remain visible after failure.
`resources_released` is separate from execution state.

Task MCP and `python -m vaws_coordinator.vaws` return compact observations by
default, with one local `record_ref` to the full response. MCP text is a summary;
structuredContent holds the observation. Pass `full: true` / `--full` for the
full response. Python TaskClient continues to return complete records. CLI
success and error output are each a single result object, without a text/result
wrapper. Explicit target requests retain launch data exactly or point to the
full record if oversized; truncated shell setup is never returned as executable.

`vaws-coordinator daemon --action status` reads loaded and installed package
identities without starting a daemon. `--action restart-if-idle` asks the daemon
to reject restart while work or unreleased leases remain; after an idle exit it
starts the installed version. MCP tools report their own process identity and
require a native-client MCP restart when stale. Missing commit metadata is unknown.

A run (or each topology role) may supply a `preflight` shell command to validate
the prepared environment before any NPU lease is allocated. It uses the selected
interpreter and a placeholder service port of zero, and must not require devices
or start a service. Failure retains original stderr references and does not launch
the business command. Planned Run Manifests can record early failure/inconclusive
outcomes without inventing a running stage.
