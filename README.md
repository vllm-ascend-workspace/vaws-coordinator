# vaws-coordinator

Local-process coordinator for one user's remote Ascend containers and host NPU
allocation. It is not a hosted multi-user service.

Consumer agents access it on the local machine; it operates the user's remote
containers through `vaws-remote-dev`. Code identity is git. The API owns runtime
preparation, lifecycle transitions and resource release; callers provide the
business command and constraints.

## Agent references and recorded launch facts

`TaskClient()` uses an explicit context or `VAWS_CONTEXT_FILE` first. Local
Codex commands can also resolve their actual `CODEX_THREAD_ID`, creating or
resuming its native attachment locally when the hook did not export a context.
Conflicting native identities require an explicit context. This does not bind
sources, discover machines, or allocate devices; ordinary code review needs no
task client.

A configured existing user container can prepare a task without selecting an
image recipe again. Creating a container still requires an explicit recipe.
Preparation checks source/image build compatibility before installing vLLM.
A completed failing preparation command ends that execution with its diagnostic
log; an unavailable SSH transport remains uncertain. Correct the configuration
and submit a new execution instead of repeating the failed preparation forever.

`vaws_execution` accepts exactly one `execution_id` or task-scoped `service`.
The Python equivalent is `client.observe(service="model", action="status")`.
An absent service returns `state: not_found` without contacting a runtime.
An ambiguous live service requires an execution reference. Lookup never joins
another task or allocates resources.

Library workflows can use `client.wait(execution_id, until="running")` or
`until="released"`, with a bounded `timeout_seconds`. A timeout returns the
last observation with `wait_timed_out: true`; a release wait requires confirmed
termination and resource release. Changing bound business worktree paths
returns idle runtime bindings internally before preparing the next code state;
live execution leases still prevent that transition.

Managed launch injects `VAWS_EXECUTION_OBSERVATION` and retains the same receipt
as `target.launch_observation`. It records the source snapshots, attested
environment/native build identity, physical host, allocated devices and actual
command. User environment values are represented by a digest. The variable is
reserved and cannot be supplied by callers. The receipt survives termination
and later binding refreshes; it proves what was verified at launch, not a later
inspection of runtime mutations. Workload collectors add their actual model,
topology and input parameters; missing facts remain unknown.

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
Git snapshot commands enable long-path support for their own Windows invocation,
including nested submodule refs, without changing repository or global Git config.
The tests that emulate a Linux peer need a working Bash. If the Windows `bash`
alias points at an unconfigured WSL installation, prepend Git for Windows'
`bin` directory to the test process's `PATH`, as the Windows CI job does.

## Progress, records, and loaded versions

Execution observations include the active preparation/sync/preflight step,
its timestamps and log reference. Installation heartbeat events reach status
while compilation is in progress. Full install logs remain in the task root;
role errors, lease state and descendant quietness remain visible after failure.
`resources_released` is separate from execution state.
Completed build-compatibility failures end preparation before editable installs
and do not retry automatically. Completed source-sync failures retain their
original cause; lost transport remains uncertain. Remote profile paths are
validated independently of the client operating system.
If a successful editable build removed its CMake cache and the image did not
export SoC/compiler variables, attestation reads those build selections from
the latest completed installer log and hashes that log as profile evidence.
Incomplete or conflicting evidence stays an error.

Managed source materialization checks current remote HEADs and tracked and
untracked changes under the container lock. When every repository already
matches the newly computed local snapshot, it skips mirror transport and reset.
Runtime compatibility, native build checks and resource allocation still run.

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
