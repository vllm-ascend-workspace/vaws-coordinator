# Handoff: extracting the coordinator from the scaffold

This repository is `.agents/coordinator/` from `vllm-ascend-workspace`, plus
the task-identity and pool libraries that belonged to it but lived elsewhere
in that scaffold. This document states exactly what the scaffold must change,
which shared libraries moved versus stayed and why, the interface this
repository now expects from remote-dev, and the discrepancies found on the
way. Nothing here changes behaviour on its own.

## 1. Ownership decisions, file by file

Two failure modes drove every call below. Moving a file the scaffold still
needs strands the scaffold; copying a file whose *state* has one owner
creates two owners of that state, which is worse. Pure functions and schemas
can be pinned; state authorities cannot.

### Moved here (coordinator-owned)

| Scaffold path | New path | Why |
| --- | --- | --- |
| `.agents/coordinator/` | repository root | The component itself. |
| `.agents/lib/vaws_ready_runtime.py` | `lib/vaws_ready_runtime.py` | The pool database, bindings, leases, events. Imported only by the coordinator server and its tests. |
| `.agents/lib/vaws_managed_execution.py` | `lib/vaws_managed_execution.py` | Managed job state machine; `RuntimePool` is its only subclass and only importer. |
| `.agents/lib/vaws_runtime_profile.py` | `lib/vaws_runtime_profile.py` | Profile/bundle identity. Imported only by coordinator code. |
| `.agents/lib/vaws_build_inputs.py` | `lib/vaws_build_inputs.py` | Build-input identity. Shared with parity, but this repository is the **verifier**: a mismatch here is what makes a runtime a cache miss, so the authority belongs with the check. See §2 for what parity must do. |
| `.agents/lib/vaws_agent_session.py` | `lib/vaws_agent_session.py` | The local task/attachment registry. The scaffold's own authority table already assigns "VAWS task identity and native session attachments" to this component. |
| `.agents/lib/vaws_task_client.py` | `lib/vaws_task_client.py` | The client of this coordinator's HTTP MCP; it has no scaffold consumer. |
| `.remote-dev/core/vaws_ops.py` | `lib/vaws_ops.py` | `vaws_session/run/execution/finish` are coordinator semantics. Registering them inside the remote-dev server was a layering mistake: that server owns neither the task registry nor the pool. Tool descriptions and input schemas moved with them. |
| `.agents/scripts/vaws.py` | `scripts/vaws.py` | The attach adapter and CLI form of the same four operations; it writes the task registry. |
| `.agents/scripts/vaws_client_setup.py` | `scripts/vaws_client_setup.py` | Configures the hooks that create task attachments. Keeping it next to the registry keeps one writer of that state. |
| `.agents/hooks/vaws_session.py` | `hooks/vaws_session.py` | Same reason: it is the process that actually creates and resumes attachments. |
| `.remote-dev/core/managed_jobs.py` | `workers/managed_jobs.py` | The child-subreaper execution supervisor. Nothing in remote-dev imports it; `backend.py` is its only consumer and the guarantees it implements are this component's guarantees. It lives in `workers/`, not `lib/`, because it is source text shipped into a container and never imported here — see §3. |
| `.remote-dev/tests/test_managed_jobs.py` | `tests/test_managed_jobs.py` | Moves with its subject. |

### Stayed in the scaffold (consumed through a narrow interface)

| Scaffold path | Why it stayed | How this repository consumes it |
| --- | --- | --- |
| `.agents/lib/vaws_npu_coordination.py` | The host device-allocation authority. Its durable state lives in each host's `/tmp`, and the scaffold's legacy session leases drive the same implementation. Two copies would mean two owners of one host's device state — the exact outcome to avoid. Six scaffold call sites versus one here. | `lib/vaws_host_queue.py` ships the configured module to the host and speaks `handle_request`/`CoordinationError`. `VAWS_HOST_QUEUE_MODULE`. |
| `.agents/lib/vaws_remote_toolbox.py` | The managed VAWS toolbox with ~30 scaffold consumers. This repository used exactly one private symbol from it (`_load_inventory`), which is not an interface. | `lib/vaws_machine_directory.py` reads the inventory JSON directly. `VAWS_MACHINE_INVENTORY`. |
| `.agents/lib/vaws_local_state.py` | Scaffold workspace state: machine profiles, workspace identity, inventory paths, ~24 consumers. Moving it would strand machine management, repo-init and every skill. | `--state-dir` is now explicit, and `lib/vaws_state_paths.py` re-homes only the two path resolvers the task registry needs (`shared_workspace_root`, `agent_sessions_root`), with `VAWS_AGENT_SESSIONS_DIR` as the explicit override. |
| `.agents/lib/vaws_session_id.py` | Legacy session binding/lookup for session worktrees. Not imported by any coordinator file. | Not consumed. |
| `.agents/lib/vaws_session_state.py` | Legacy session registry used by ~20 skill scripts. Not imported by any coordinator file. | Not consumed. |
| `.agents/lib/vaws_validate.py` | Generic scaffold input validation for agent-facing scripts. Not imported by any coordinator file. | Not consumed. |
| `.agents/lib/vaws_run_manifest.py` | Run Manifest v1 is the scaffold's cross-skill evidence schema with ~15 readers. The coordinator is one *producer*; making it the schema authority would invert that. | Byte-pinned copy in `lib/vendor/vaws_run_manifest.py`, recorded in `lib/vendor/UPSTREAM.json` (sha256 `cf0a3199…f104b`) and enforced by a test. A pure schema duplicates no state. |
| `.agents/skills/remote-code-parity/scripts/` | Parity owns source staging and materialization. | `lib/vaws_parity.py` invokes the scaffold CLI. `VAWS_PARITY_SCRIPT`, `VAWS_PARITY_WORKSPACE_ROOT`. |

`prepare_runtime.py` previously imported `discover_repo_tree`/`iter_postorder`
from the parity script. Attestation runs inside a prepared container where
only this repository is deployed, so those three helpers were ported verbatim
into `lib/vaws_git_sources.py`, preserving messages and postorder traversal
(the attestation tests assert on both). The only intentional wording change
is "before attestation" instead of "before remote-code-parity".

## 2. What the scaffold must change

1. Delete `.agents/coordinator/`.
2. Delete the moved libraries: `.agents/lib/vaws_ready_runtime.py`,
   `vaws_managed_execution.py`, `vaws_runtime_profile.py`,
   `vaws_task_client.py`, `vaws_agent_session.py`, `vaws_build_inputs.py`.
3. `.agents/skills/remote-code-parity/scripts/remote_code_parity.py` imports
   `VLLM_REINSTALL_PATTERNS`, `VLLM_ASCEND_REINSTALL_PATTERNS`,
   `DEPENDENCY_INSTALL_PATTERNS`, `BUILD_INPUT_ENV_KEYS` and
   `build_input_fingerprints` from `vaws_build_inputs`. Point it at this
   repository's `lib/vaws_build_inputs.py` (submodule, install, or
   `sys.path`), or keep a copy pinned byte-identical to sha256
   `967adeb699e47de2e281581d576a69f6c85075385975e42916ead1ed198a2e09`.
   Divergence changes build keys and silently turns warm runtimes into cache
   misses — or worse, the reverse.
4. Delete `.remote-dev/core/vaws_ops.py` and remove its surface from the
   remote-dev MCP server:
   - `.remote-dev/mcp/tools.py:24` `from core.vaws_ops import vaws_call`
   - `.remote-dev/mcp/tools.py:61-64` the four `vaws.*` descriptions in
     `list_tools()`
   - `.remote-dev/mcp/tools.py:219-220` the `name.startswith("vaws.")`
     dispatch in `call_tool`
   - `.remote-dev/mcp/schemas.py:99-115` `task_schema` and the
     `TOOL_SCHEMAS.update({...})` block (`ALIASES` is derived from
     `TOOL_SCHEMAS`, so the `vaws_session`/`vaws_run`/`vaws_execution`/
     `vaws_finish` aliases disappear with it)
   - `.remote-dev/tests/test_mcp_schema.py:80` the `name.startswith("vaws.")`
     branch
   - delete `.remote-dev/tests/test_vaws_ops.py`
   To keep serving the tools from that server, import this repository's
   `lib/vaws_ops.py` (`TOOL_DESCRIPTIONS`, `TOOL_SCHEMAS`, `vaws_call`) and
   call `vaws_call(name, args, make_result=core.result.make_result)`.
5. Delete `.agents/scripts/vaws.py`, `.agents/scripts/vaws_client_setup.py`
   and `.agents/hooks/vaws_session.py`.
6. `.agents/skills/session-management/tests/test_agent_sessions.py` imports
   `vaws_agent_session` and `vaws_task_client`: delete it or re-point it at a
   coordinator checkout. The coordinator's own suites cover that behaviour.
7. `vaws_local_state.agent_sessions_root()` stays, and
   `.agents/scripts/workspace_profile.py` still reports
   `agent_sessions_path`. Either set `VAWS_AGENT_SESSIONS_DIR` to that same
   directory or accept that the two components report different registries.
8. Documentation: `AGENTS.md` and `CLAUDE.md` reference
   `.agents/coordinator/README.md`, the `vaws_session/vaws_run/
   vaws_execution/vaws_finish` tool surface and the moved libraries in their
   maintenance list. Repoint them at this repository.
9. `.agents/lib/vaws_npu_coordination.py` is now a published interface, not
   an internal module: `handle_request(request)` plus `CoordinationError`,
   and the `failed`/`needs_input`/`probe_failed` statuses. Its wire framing is
   duplicated in `lib/vaws_host_queue.py` (heredoc delimiter and runner) to
   match `session-management/scripts/npu_coordination.py:build_remote_command`.
   Changing the framing or the request/reply contract is a cross-repository
   change.

## 3. Interface required from remote-dev

The sibling extraction of remote-dev is in flight and its final API is not
published yet, so this is stated as an **assumption**, not a fact:

> This repository assumes remote-dev keeps an explicit-endpoint shell API. It
> does not assume anything about the resolver plugin interface, because it
> never asks remote-dev to resolve an alias, session or machine, and it no
> longer assumes anything about the job supervisor, because it owns it.

Concretely, from `$VAWS_REMOTE_DEV_ROOT`:

1. `core.endpoint.direct_endpoint(mapping)` if it exists, otherwise
   `core.endpoint.resolve_endpoint(mapping)`, building an endpoint object from
   an explicit `{"host", "port", "user", "root", "cwd"}` mapping. Both names
   are accepted; `lib/vaws_remote_dev.py` refuses to call either without both
   `host` and `port`, so the alias/session/machine and cwd-auto-bind branches
   can never be reached from here.
2. `core.shell_ops.remote_bash(endpoint, *, command, timeout_ms, runtime_env)`
   returning `{"result": {...}}`, where the result carries `outcome`,
   `status`, `exit_code` and `refs.stdout` / `refs.stderr` as paths to local
   log files. Timeout stays 45 s; `runtime_env=False`.
3. Optional: `core.result.make_result` for the `remote-dev.result.v1`
   envelope. When it is unavailable, `lib/vaws_result.py` emits a
   field-compatible mirror so offline local task operations keep working. The
   envelope contract stays remote-dev's; the mirror is not the authority.

If remote-dev's shell API changes shape, `lib/vaws_remote_dev.py` is the only
file to update.

### No supervisor requirement — corrected

A previous revision of this section listed a third requirement:
`core/managed_jobs.py` readable as source text from the remote-dev checkout.
**That requirement was wrong and must not be reintroduced.** remote-dev
removed the supervisor in its commit `900ad15` on the reasoning that nothing
there imports it and its only consumer is this repository, and that commit
says the file moves here. Recording it as an external dependency at the same
time left it in neither repository while `backend.py` still read it.

It now lives at `workers/managed_jobs.py`, recovered byte-for-byte from
remote-dev's history, and is read through `backend.worker_source`. The
`{"root", "job_id", "action", ...}` protocol (`prepare`, `go`, `status`,
`tail`, `stop`, answered as one JSON object on stdout with the `receipt` —
`pid`, `start_ticks`, `boot_id`, `marker`, `process_guard` — and the `quiet`
drain flag) is now an internal contract between `backend.py` and that file,
not a cross-repository one. remote-dev supplies the shell transport it rides
on and nothing more; `RemoteDevShell` has no `worker_source` method.

### Host transport change, stated explicitly

`backend.py` used to reach the host queue through the scaffold's
`session-management/scripts/npu_coordination.py` (`LocalEndpoint`,
`ssh_execute`, `build_remote_command`). It now goes through the same injected
remote-dev shell adapter that the container probes already used, with the same
45 s timeout and the same fail-closed reading of the reply. This is a
transport re-homing, not a semantic change, and it is called out here because
it is the one behavioural difference introduced by the move.

## 4. Discrepancies found between the documented guarantees and the code

The move deliberately carried all four over unfixed, because a behaviour
change does not belong in a move commit. They are resolved below, each
verified before it was acted on.

1. **Python floor.** *Confirmed; resolved in the documentation.* The README
   said "Requires Python 3.10+ on the manager". `server.py` catches the
   builtin `TimeoutError` around `asyncio.wait_for(stop.wait(), ...)`, and
   `asyncio.TimeoutError` only became an alias of that builtin in 3.11.
   Reproduced on CPython 3.10.20: `asyncio.TimeoutError is TimeoutError` is
   `False`, its MRO is `TimeoutError -> Exception`, and a loop of
   `asyncio.wait_for` guarded by `except TimeoutError` dies on the first
   interval with `asyncio.exceptions.TimeoutError`. The same script survives
   three intervals on 3.11.13. `import tomllib` also fails on 3.10, so
   `scripts/vaws_client_setup.py` needs 3.11 independently. The real floor is
   3.11 and the README now states it with the reason, instead of promising
   3.10 and shipping a warning about it. No code changed: a 3.10 manager was
   already broken, and is now honestly out of support rather than nominally
   supported.
2. **Access-file mode.** *Confirmed; resolved fail-closed in the code.* The
   README asks for mode `0600`; `server.py` rejected only group and other bits
   (`st_mode & 0o077`), so `0700` — owner-executable — passed. Resolved
   towards the stricter documented requirement: `load_access` now requires
   exactly `0600`. **Consequence:** a manager whose access file is currently
   `0700` stops starting, and reports both the mode it found and the mode it
   needs; `chmod 600` is the entire fix and no token, digest or client
   configuration changes. The check moved out of `main()` into `load_access`
   so a test can reach it, and that function now also turns a missing or
   malformed access file into an argument error instead of a traceback.
3. **Digest validation.** `create_app` checks that each principal's `sha256`
   is 64 characters, not that it is hexadecimal. A malformed digest fails
   closed at comparison time rather than at startup.
4. **`--state-dir` default.** It used to default to the scaffold's primary
   worktree. That derivation is gone, so the flag is now required. This is a
   deliberate fail-closed change: guessing a directory would silently fork one
   runtime pool into two databases, which the README forbids.

Everything else matched: the host queue remains the sole allocator, the
supervisor stays a subreaper whose disappearance yields `unknown` with the
lease retained, reconciliation refuses to release without bounded evidence, a
lost job directory never reads as completion, and a returned runtime is
quarantined until a full re-registration verification passes.

## 5. Cross-repository test dependency

The control-plane suite runs against the actual host protocol rather than a
hand-written double. CI checks out
`maoxx241/vllm-ascend-workspace@161fed1b0fe6b48359be3f0cf33bb7d8befae113`
with a sparse checkout of `.agents/lib/vaws_npu_coordination.py` and exports
`VAWS_HOST_QUEUE_MODULE`. Without that module the suite skips itself and CI
fails the pre-check, so a green run can never mean "tested nothing". When the
scaffold moves, renames, or changes the host protocol, update the pin in
`.github/workflows/ci.yml`.
