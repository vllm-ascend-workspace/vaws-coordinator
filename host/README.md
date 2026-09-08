# Host NPU authority

`vaws_npu_coordination.py` is the sole device-allocation authority.

- This repository owns the module. The scaffold consumes it from this checkout.
- There is exactly one implementation: two copies would mean two owners of one host's device state.
- The file is stdlib-only. It is shipped over SSH and executed on the host.
- Durable state is `/tmp/vaws-npu-coordinator/v1/` (SQLite). A missing database starts a new epoch.
- Interface: `handle_request(request) -> dict` and `CoordinationError`.
- `SCHEMA_VERSION` is the host SQLite schema. Bump it when the on-host tables or their meaning change. Clients read it; they do not hardcode it.
- `VAWS_HOST_QUEUE_MODULE` overrides this file's path. It is not required.
