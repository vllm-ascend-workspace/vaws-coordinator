# Handoff: coordinator as a local installable package

This repository is the VAWS coordinator extracted from
`vllm-ascend-workspace`. It is a **local process**: it coordinates the current
user's own remote containers and NPU allocation. It does not host a manager
for other people to connect to.

Install as `vaws-coordinator` (import `vaws_coordinator`). The scaffold
consumes the host authority with:

```python
from vaws_coordinator.host_queue import ...
```

## Ownership

| Piece | Owner | Notes |
| --- | --- | --- |
| Host NPU authority | this package, `vaws_coordinator.host` | Shipped to the host and executed there. One implementation. |
| Host queue client | `vaws_coordinator.host_queue` | Public API. |
| Task tools / stdio MCP | `vaws_coordinator.task_server` | `vaws-coordinator task-server` |
| Runtime pool / managed jobs | `vaws_coordinator.ready_runtime` | In-process, this user. |
| Execution supervisor | `vaws_coordinator.workers.managed_jobs` | Source text shipped into a container. |
| Result envelope | `remote_dev.result` | `schema_version: remote-dev.result.v1` |
| Remote shell | `remote_dev.endpoint` / `remote_dev.shell_ops` | Pip package `vaws-remote-dev`. |
| Run Manifest v1 | scaffold; copy in `vaws_coordinator/vendor/` | Identity is the upstream git ref, not a hash. |

## What this package expects from remote-dev

Import the installed `remote_dev` package. Do not locate a checkout by path.

1. `remote_dev.result.make_result` — `remote-dev.result.v1`.
2. `remote_dev.endpoint.resolve_endpoint(mapping)` from an explicit
   `host` + `port` (this package refuses calls without both).
3. `remote_dev.shell_ops.remote_bash(endpoint, *, command, timeout_ms, runtime_env)`
   returning `{"result": {...}}` with `outcome` and `refs.stdout` / `refs.stderr`.

Until `vllm-ascend-workspace/remote-dev` ships a `pyproject.toml`, installs
use the stub at `tests/fakes/vaws-remote-dev`.

## Scaffold follow-ups

1. Import `vaws_coordinator.host_queue` instead of a checkout path or
   `sys.path` shim.
2. Point task MCP at `vaws-coordinator task-server` (or `uvx --from git+...`).
3. Do not start or document an HTTP manager, bearer tokens, or
   `vaws_client_setup`.
