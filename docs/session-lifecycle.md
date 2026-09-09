# Task and reusable runtime lifecycle

Status: current

This is the target contract for the breaking lifecycle change. It guides the
remaining implementation; it does not claim that the candidate already meets
every requirement. Task completion preserves the container and its prepared
environments. Reuse the existing ready-runtime pool and managed executions.

## Ownership

| Object | Owns | Lifetime |
|---|---|---|
| Local task | Worktree references, run history, evidence | Independent of remote execution |
| Prepared runtime | Container identity, SSH endpoint, installed environment directories | Reused across tasks |
| Runtime binding | One task's permission to use the runtime | Checkout through return |
| Execution | Managed process family, NPU lease, service-port use | Admission through confirmed cleanup |

A container's existence and device mounts do not reserve NPUs. The host
coordinator is the only NPU and host-port authority. SSH endpoint reservations
belong to the runtime; they survive task completion. Service-port use and NPU
leases belong to executions. Port ownership must prevent overlap with runtime
SSH endpoints. The scaffold stores receipts and workflow inputs, not a second
allocation table.

Keep the pool's existing exclusive runtime checkout rule for this change.
Different task environments can coexist on disk. Concurrent bindings within
one container are a separate scheduling extension and are not required for
container reuse.

## Environment selection

Each launch records an explicit worktree, Python executable, CANN installation
directory, build artifacts, environment variables, and owned output/cache
directories. A new process receives this environment before importing native
libraries. Do not mutate global shell configuration, shared latest symlinks,
or an environment/build directory that an active execution uses.

- vLLM and vLLM-Ascend use separate source/build locations. Python source
  selection alone does not select or rebuild their native extensions.
- Python environments can contain different compatible torch/torch_npu pairs.
  Select the interpreter explicitly; isolate its packages from global installs.
- CANN user-space versions can live in versioned installation directories.
  Select the matching toolkit, operator packages and dynamic-library paths as
  one launch environment. Driver/firmware and kernel facilities remain host
  constraints and cannot be switched by a per-process environment variable.
- Prepared environments may be reused. Build caches are reused only for matching
  inputs; logs and mutable run outputs remain execution-owned.

Retain the existing profile/build verification and refresh entrypoints. A
different source tree or environment does not inherently require a new container.
Re-verify prepared artifacts before their next use. Runtime provisioning,
maintenance and explicit deletion are separate from task completion.

## Operations

**Open task.** Create local task identity and bind actual business worktrees.
This does not create a container or reserve NPUs.

**Prepare and borrow.** Select a compatible prepared runtime through the existing
pool. If preparation is necessary, install into a separate environment/build
directory while no execution uses that directory, then verify it. A cache miss
is reported as such; it is not permission to overwrite an active environment.

**Execute or serve.** Use the existing managed execution path. Persist the
request and process identity, obtain the host lease and service-port use, then
launch with the selected environment. Validate the current binding and fencing
information at admission. A long-running model service keeps its execution
lease for its entire process lifetime. All managed device use follows this
path; an interactive NPU command also needs an execution lease.

**Stop or finish.** Stop only this execution's managed process family, including
children. Confirm it is terminal, its assigned devices are observable and free,
and its service ports are no longer listening before releasing its resources.
Return and verify the runtime for reuse through the existing pool path. Keep
the container, SSH endpoint, prepared environments, worktrees and evidence.
Thus stopping a model releases that model execution's NPUs; an idle local task
does not keep cards reserved.

**Close task.** Close admission under the existing binding lock, finish/cancel
its admitted executions, release their leases, then return the binding. Close
must serialize with launch: admitted work remains owned until cleanup completes,
and a returned binding cannot launch new work. Use the existing pool/run states
and managed supervisor; do not add a parallel session lease state machine.

**Failure and retry.** Unknown process/device state or cleanup failure keeps the
execution's ownership. A failed runtime verification leaves it unavailable for
checkout. Retry the existing stop/finish operation after the condition clears.
GC reports unresolved ownership; age, local PID death and missing local metadata
are not release evidence. Request retries are idempotent; new executions use
new request identities. No container deletion is required to prove completion.

## Breaking cutover and deletion

Route managed task workflows through the existing pool/binding/execution APIs.
Remove the old per-session container allocation/release path, local leases.json
authority, its compatibility arguments and tests, and the unmerged candidate's
parallel durable session_reserved allocation path. Keep ordinary explicit
remote endpoints for remote-dev operations. Runtime maintenance can explicitly
remove a container after its bindings and executions are gone.

No old-record migration, auto-adoption, shared host database reset, or automatic
workload removal is part of this change. Update clients together. Retain one
CI execution per suite and distinct package installation checks.

## Minimum acceptance

1. Two successive tasks reuse the same container with their selected source and
   environment; the first leaves no managed workers, NPU lease or service port.
2. A live service retains its resources; stopping it releases them while the
   container and SSH endpoint remain available.
3. Wrong/stale ownership and close-versus-launch cannot mutate another run or
   release ahead of admitted work.
4. Unknown probes or failed cleanup retain ownership; retry completes when the
   condition clears. A failed runtime check prevents reuse.
5. Existing host allocation prevents device/port conflicts. Environment switching
   does not overwrite an active environment or reuse mismatched native artifacts.

Adapt the existing focused tests and delete tests of removed behavior. Run one
full CI pass after integration. Actual NPU environment switching still needs
remote evidence; documentation and mocked tests do not establish ABI support.

## References

- [Python virtual environments](https://docs.python.org/3/library/venv.html)
- [TorchNPU version matching](https://github.com/Ascend/pytorch/blob/master/COMPATIBILITY.en.md)
- [CANN package environment variables](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/82RC1alpha001/maintenref/envvar/envref_07_0003.html)

The existing implementation anchors are ready_runtime.py (checkout,
return_runtime, refresh) and managed_execution.py (_finish_managed).
