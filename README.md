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

`client.finish()` closes a task that has never admitted managed work directly
in the local registry, without starting or importing the coordinator service
or remote-dev. Local close and execution admission share one database write
transaction boundary: if close wins, submission is rejected; if admission wins,
finish delegates to the coordinator for owned-execution cleanup.

A configured existing user container can prepare a task without selecting an
image recipe again. Creating a container requires an explicit recipe or a
concrete image tag/digest, supplied in the same run's `environment.image`.
No separate provisioning call is required for a first run at a fixed version.
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
changes defaults for future submissions. It does not affect active executions
or require their bindings to be returned.

`client.run(command, sources={"app": "/actual/worktree"})` captures fixed Git
content and SCM provenance before admission. Omitted `sources` uses explicit
task defaults, or this native attachment's automatic cwd binding when no
explicit defaults were set; `sources={}` runs without source dependencies.
Defaults are replaced by `client.sources(mapping)`, including `{}` to clear
them. All roles consume one accepted snapshot; later edits affect only later
submissions. Replies expose `source_snapshot_id` and the selected source map.
Capture pins Git objects without moving HEAD or changing the user's index.

Resources default to `npu_count=0`. A CPU command or compiler therefore reserves
no NPU; declare a positive `npu_count` or specific `devices` for NPU work. Each
execution has its own work directory, while compatible prepared artifacts can
be reused. Independent host preparations run concurrently, bounded to four
workers and one active preparation per host.

To share one explicitly selected physical NPU with existing external workers,
pass `resources={"devices": [0], "allow_external_busy": True}` to `client.run`
or `vaws_run`. Use `topology={"host": "selected-host"}` to bind the host too.
The option is fixed at admission and requires exactly one explicit device;
omitting it keeps the normal occupancy checks. It permits observed external
process/HBM use, without estimating or reserving free memory. Other coordinator
leases and holds still conflict, and unknown or missing hardware is not usable.
The managed supervisor must retain its own process guard until completion.
Stop/finish only terminates this execution's family and releases its lease once
that family has drained and its ports are clear; existing workers remain running.
The assignment, launch observation and execution target expose the sharing flag.

`run(..., service="model")` ensures identical fixed sources and configuration.
Changed inputs report the differing fields; `restart=True` replaces the service
only after the old execution has stopped and released resources. Connecting
with `observe(service="model")` does not capture the current worktree.

Managed launch injects `VAWS_EXECUTION_OBSERVATION` and retains the same receipt
as `target.launch_observation`. It records the source snapshots, attested
environment/native build identity, physical host, allocated devices and actual
command. User environment values are represented by a digest. The variable is
reserved and cannot be supplied by callers. The receipt survives termination
and later binding refreshes; it proves what was verified at launch, not a later
inspection of runtime mutations. Workload collectors add their actual model,
topology and input parameters; missing facts remain unknown.

## Status observations

`vaws-coordinator runtime-register` sends verification and registration to the
running coordinator, which owns all catalog writes. Register an existing native
artifact donor with `--reuse-only --source vllm=PATH --source vllm-ascend=PATH`;
the source inputs are fixed before verification and the donor work root cannot
be checked out for execution. Library clients can submit the same explicit
specification with `CoordinatorClient.runtime_register(runtime_id, spec)`.

Task MCP/CLI execution status returns the latest persisted managed-job snapshot
immediately. A sample older than two seconds schedules background progression.
`vaws execution --refresh` (or tool argument `refresh: true`) requests
a new status observation. Replies include `observation_freshness` with snapshot
completion time, age, freshness, source and whether a busy execution deferred
refresh. Per-role `status_observed_at` preserves individual sampling times;
roles are sampled concurrently with at most four workers. The top-level `observed_at` is the
response-generation time, not proof of a new remote query.

`TaskClient.observe()` preserves its fresh-by-default library behavior; pass
`refresh=False` to read the nonblocking status cache. `TaskClient.wait()` uses
this path so slow remote probes cannot overrun its observation timeout. Tail, target, stop, resource
allocation and background progression retain their existing behavior. Cached
observations neither allocate resources nor establish new ownership. A busy
execution returns its stored observation immediately and explicitly marks a
requested refresh as deferred. Cached stale or missing timestamps schedule a
refresh on the existing execution worker; state/error/release fields remain visible
and the reply reports its actual sample age rather than claiming fresh remote facts.

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

The package depends on `vaws-remote-dev>=0.7.0` (import `remote_dev`). It
does not pin that package's git source; the workspace that installs this
library chooses the tag. `uv sync` / `uv lock` are not the developer path
here: a library that named remote-dev's git source in `pyproject.toml`
would pin every consumer to that tag.

## Start the task server

`vaws-coordinator task-server` serves task and coordination tools over stdio MCP:

`vaws_session`, `vaws_run`, `vaws_execution`, `vaws_finish`, `vaws_message`.

For a substantive coordination request, pass a `coordination_peers[].reference`
returned while waiting and the message text to `vaws_message`. Reply with the
received `notifications[].reply_reference`. The task supplies the sender;
there are no owner, host endpoint, thread, cursor or ACK parameters to fill in.
Messages never execute commands, release leases or stop another user's task.

Normal run/status calls return locally cached notifications and trigger at most
one short polling worker for the task's already known hosts, with a five-second
minimum interval after each completed poll. They do not wait for remote inbox
I/O or scan the fleet. The existing local coordinator owns the worker; there
is no new daemon, SSE service or background Agent wakeup. A later normal call
delivers newly fetched text. A task without managed hosts remains local.

The host stores messages alongside the existing queue database, so independent
local coordinator/session stores can exchange offline messages. The queue's
default `/tmp` state can disappear on host reset; this is not reboot-durable
storage. Local `mail-message` records preserve fetched text before advancing
the host cursor. Automatic delivery records that the local coordinator returned
the text, not that the native client received it or the Agent accepted it;
there is no exactly-once end-to-end or manual acknowledgement claim. Peer
timestamps are contact observations, never evidence that resources are free.
Custom backends opt in with `supports_task_messages = True`; older backends
receive no new host actions. Embedded service owners call `close_messages()`
before removing their state; the daemon closes its workers on shutdown.

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

Managed launches prepend their selected source directories to Python's import
path. This prevents repository directories
in the task cwd from shadowing editable packages, while preserving the CANN
and other support paths already supplied by the environment.

The first native environment and rebuilt outputs receive a full framework
import smoke. A fresh Python source view can reuse that original evidence when
its dependency/native inputs, loader environment and complete artifact hashes
match; preparation checks the current module and SCM metadata mappings without
importing the changed business code. Its receipt explicitly records
`python_import_executed=false`, keeps the original import result, and makes no
claim that the new Python source passed. The business execution reports its own
result. Incomplete old evidence or cwd-dependent loader paths retain the full
import check.

For a compatible native environment, source materialization is followed by one
native-view publication: outputs are copied and checked against the existing
hashes, source/SCM mappings are updated, and the original import proof is carried
forward. Its completed receipt goes directly into an atomic managed binding.
There is no second profile capture, full registration probe, or SSH reservation.
Before launch, coordinator checks the container, environment version facts,
current source mappings and fixed Git inputs; it does not rehash all native
outputs in its private execution view. Initial builds, changed native or
dependency inputs, and explicit adoption/repair retain complete verification.
A lost, failed or cancelled publication never publishes a successful binding.

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
| `vaws_coordinator/task_server.py` | Stdio MCP for task and coordination tools |
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
`vaws-remote-dev>=0.7.0` without a git source: remote-dev is not on PyPI,
so `uv sync` / `uv lock` fail with an unsatisfiable-dependency error.
That is intentional. A library that pinned remote-dev's git URL would
take the upgrade decision away from every consumer, and
`constraint-dependencies` cannot carry a git URL.

Install the git source first, then this tree without resolving
dependencies from an index:

```bash
uv venv
uv pip install "vaws-remote-dev @ git+https://github.com/vllm-ascend-workspace/remote-dev@2de5cc32c5f3dd517e698cadfb1f9ed23589b1aa"
uv pip install pytest "jsonschema>=4" "setuptools-scm>=8"
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
Long install, native reuse and profile verification commands have persisted
remote-dev job references and receipts. Stop interrupts their owned process
families and requires verified quiet before reporting cancellation or release;
an unknown transport or ownership outcome remains uncertain. A restarted daemon
can observe and stop these retained jobs without replaying preparation or
replacing sources beneath a compiler. Queued preparation can cancel while
another execution holds the host's preparation lock. Tail includes the current
local preparation log. Fixed source materialization runs as an owned preparation
job with bounded upload/command deadlines and cancellation support.
Completed build-compatibility failures end preparation before editable installs
and do not retry automatically. Completed source-sync failures retain their
original cause; lost transport remains uncertain. Remote profile paths are
validated independently of the client operating system.
If a successful editable build removed its CMake cache and the image did not
export SoC/compiler variables, attestation reads those build selections from
the latest completed installer log and hashes that log as profile evidence.
Incomplete or conflicting evidence stays an error.

Managed source materialization consumes the admitted Git snapshot directly in
one remote operation. Existing immutable mirror objects are reused; a completed
missing-object response uploads only those objects before a new owned job.
Small edits use a bounded Git pack in that next owned job over the existing
RPC connection. The complete command, including all encoded packs, is capped
below the remote worker's argument limit; cold or larger transfers retain Git
SSH. Pack contents, prerequisite commit and resulting tree are verified before
atomically publishing snapshot refs or materializing the execution view.
Each execution retains independent working files, a stable per-root lock, and
final HEAD and dirty-state checks across parent repositories and submodules.
Uncertain jobs are observed, never replayed. Runtime compatibility, native
build checks and resource allocation still run. Host coordination uses the
remote-dev Python RPC code cache, sending only the request after the first call.

Short root preparation reuses that endpoint's Python RPC connection; owned
preparation jobs wait for exit within their bounded polling interval instead
of returning early merely because output arrived. First admission persists
the observed host epoch before submitting and acquiring in one host exchange.
New managed runs read their exact task and container facts together, verify the
compact source/environment view, then reuse a newly issued grant's occupancy
sample for host preflight. Queue recovery verifies again; facts are not cached
across queue waits. Startup and release operations record monotonic durations
in the existing run events, without command contents or heartbeat log entries.
Completed managed shared leases retain the host process-guard and port checks;
they need no whole-device visibility scan to release their own ownership.

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

Managed runs validate fixed inputs and binding/resource parameters before queueing;
they verify the complete remote environment and source view after the grant,
before preparing or authorizing the business command. A failed verification retains
its error and drains the owned job before returning resources. Unknown process or
host state keeps cleanup pending. Standalone `RuntimePool.request_run` also retains
its remote verification before queueing.
