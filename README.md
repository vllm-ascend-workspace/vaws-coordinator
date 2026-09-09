# vaws-coordinator

Local-process coordinator for one user's remote Ascend containers and host NPU
allocation. It is not a hosted multi-user service.

Install it, run it on the machine you are sitting at, and it talks to *your*
remote containers through the `vaws-remote-dev` package. Code identity is git.

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

The package depends on `vaws-remote-dev>=0.4.0` (import `remote_dev`). It
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

`vaws_session` and `vaws_finish` are local. `vaws_run` uses this process's own
runtime pool and the host NPU authority. Pass the `context_file` supplied by
the native session hook; never guess a task from cwd or history.

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
`vaws-remote-dev>=0.4.0` without a git source: remote-dev is not on PyPI,
so `uv sync` / `uv lock` fail with an unsatisfiable-dependency error.
That is intentional. A library that pinned remote-dev's git URL would
take the upgrade decision away from every consumer, and
`constraint-dependencies` cannot carry a git URL.

Install the git source first, then this tree without resolving
dependencies from an index:

```bash
uv venv
uv pip install "vaws-remote-dev @ git+https://github.com/vllm-ascend-workspace/remote-dev@v0.4.0"
uv pip install pytest
uv pip install -e . --no-deps
.venv/bin/python -m pytest
```

`uv venv` is the first command so an unreadable project config fails
before install. Do not add `[tool.uv.sources]` for remote-dev: that table
travels to consumers. Requires Python 3.11+.
