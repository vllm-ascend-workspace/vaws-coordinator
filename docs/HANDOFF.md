# Handoff: coordinator as a local installable package

This repository is the VAWS coordinator extracted from
`vllm-ascend-workspace`. It is a **local process**: it coordinates the current
user's own remote containers and NPU allocation. It does not host a manager
for other people to connect to.

Install as `vaws-coordinator` (import `vaws_coordinator`). The public TaskClient
contract is described in [session-lifecycle.md](session-lifecycle.md) and
implemented in [task_client.py](../vaws_coordinator/task_client.py).

## Ownership

| Piece | Owner | Notes |
| --- | --- | --- |
| Host NPU authority | this package, `vaws_coordinator.host` | Shipped to the host and executed there. One implementation. Marker: `REMOTE_DEV_JOB_TOKEN`. |
| Host queue client | `vaws_coordinator.host_queue` | Public API. |
| Task tools / stdio MCP | `vaws_coordinator.task_server` | `vaws-coordinator task-server` |
| Runtime pool / managed jobs | `vaws_coordinator.ready_runtime`, `managed_execution` | In-process, this user. Remote process control is `remote_dev.processes.control`. |
| Result envelope | `remote_dev.result` | `schema_version: remote-dev.result.v1` |
| Remote shell | `remote_dev.core.endpoint` / `remote_dev.core.shell_ops` | Pip package `vaws-remote-dev`. |
| Run Manifest v1 | this package, `vaws_coordinator.run_manifest` | `code` is Git identity (`source_head`, `snapshot_commit`). |
| Code identity | this package, `vaws_coordinator.code_identity` | Dirty trees get a parentless snapshot commit. |
| Code parity | this package, `vaws_coordinator.parity` | In-package CLI; no consumer script path. |
| Isolated task prep | `prepare_isolated_root_script` + task venv + explicit source snapshot | No image `pip uninstall`. `.venv`/`build` preserved. Bound vllm + vllm-ascend worktrees, no Git parent. |
| Machine directory | this package, `vaws_coordinator.machine_directory` | Consumers pass a document; store is coordinator-owned. |
| Persistent daemon | `vaws_coordinator.service` | Short `/tmp/vc-<user>-<sha>.sock`; lock/sqlite in state dir. |

## What this package expects from remote-dev

Import the installed `remote_dev` package. Do not locate a checkout by path.

1. `remote_dev.result.make_result` — `RESULT_SCHEMA_VERSION` is `remote-dev.result.v1`.
2. `remote_dev.core.endpoint.resolve_endpoint(mapping)` from an explicit
   `host` + `port` (this package refuses calls without both).
3. `remote_dev.core.shell_ops.remote_bash(endpoint, *, command, timeout_ms, runtime_env)`
   returning `{"result": {...}}` with `outcome` and `refs.stdout` / `refs.stderr`.
4. `remote_dev.processes.control(endpoint, job_id, action, **params)`.
5. Worker marker `REMOTE_DEV_JOB_TOKEN` and reserved prefix `REMOTE_DEV_JOB_`.

## Validation coverage

The source suite covers the control plane, local daemon, and shared transport.
The Linux `/proc` worker/guard test is skipped on macOS. Tests using fake
resources do not establish real NPU or container preparation behavior.

| Item | Production path | Discriminating test |
| --- | --- | --- |
| Isolated prep before register | `provision/task_environment.py` + `RemoteBackend.prepare_task_root`: first_install → task venv → materialize sources → pinned-python install steps → capture `ready-profile.json` → `register`. Fake backend `inspect` raises `missing ready-profile.json` unless the root was prepared with a distinct interpreter. | `test_register_empty_root_is_rejected_when_prepared_boundary_enforced`, `test_prepare_creates_isolated_interpreter_not_donor_python`, `test_remote_prepare_refuses_donor_python_and_requires_sources` |
| Process-guard marker | `host/vaws_npu_coordination.py` `JOB_TOKEN_ENV = "REMOTE_DEV_JOB_TOKEN"`; `managed_execution` / `TaskClient` reject `REMOTE_DEV_JOB_*`. | `test_process_guard_source_scans_public_remote_dev_marker`; `test_process_guard_sees_remote_dev_job_token_not_legacy_name` (skip off Linux) |
| Binding / host-scoped ids | Reuse bound objects; `checkout_identity(runtime_id, role)`; runtime id `t{session}-h{host}-{role}`. `role.host` constrains `can_prepare`. Post-prep `runtime_matches`; missing facts ≠ match. Same-task active root waits, does not re-register the deterministic path. | `test_existing_binding_is_reused_for_the_same_task`, `test_role_host_constrains_auto_preparation`, `test_missing_hardware_facts_are_not_a_match`, `test_same_task_does_not_materialize_concurrently` |
| restart=True / serialized progress | `CoordinatorService.admit` owns named-service reconnect/restart. Same-spec restart replaces after `_stop_and_wait`. `stopping` does not admit overlap. Per-execution and per-root locks; tick dispatches independent threads. | `test_named_service_reconnects_same_spec_and_restart_replaces`, `test_restart_does_not_overlap_while_stop_is_stopping` |
| Daemon on this Mac | `socket_path` → `/tmp/vc-<user>-<16-hex>.sock` (~43 bytes). Session dirs in pool `meta/session_dirs`. Ticker records bounded `failed`/`uncertain` instead of swallowing. Idle client sockets time out and close. | `test_short_socket_ping_start_exit_restart_and_session_dirs_persist` |
| Multi-role aggregate | `aggregate_job_states`: succeeded+failed → failed; all succeeded → succeeded; a succeeded role does not fail the group. `hold_go` still reserves all leases before `go`. | `test_two_role_success_and_mixed_failure_aggregate`, `AggregateStateTests` |
| Admitted finish cleanup | Persist `session["finish"]` (`user`, `force`). Tick/reconcile resume finishing tasks after execution stop drains; return bindings and mark `finished`. No second `vaws_finish`. Restart uses `meta/session_dirs`. | `test_finish_during_delayed_preparation_completes_on_ticker_without_retry`, `test_persisted_finishing_task_completes_after_coordinator_restart` |

No-donor first machine: `_ensure_user_container` calls coordinator-owned
`provision_user_container` with HostQueue `container-ssh-reserve`, then
prepare. Requires a configured `MachineDirectory` row and a supported recipe.

Hardware and model acceptance require separate remote validation.
