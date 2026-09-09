"""Choose hosts and prepared roots from attested catalog + constraints.

The host NPU coordinator remains the only device/port authority. This module
never relaxes topology, recipe, ABI or hardware constraints, and never starts
a partial group.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_RECIPES = {"rc", "main", "stable", "local-latest"}
RESERVED_ENV = {"ASCEND_RT_VISIBLE_DEVICES", "VAWS_SERVICE_PORT", "VAWS_PYTHON"}


def validate_user_env(env: dict[str, Any] | None) -> dict[str, str]:
    """Literal user env only. Managed device/port/interpreter/job tokens stay reserved."""
    if not env:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env must be a mapping of literal string values")
    cleaned: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            raise ValueError("env keys must be nonempty strings")
        if key in RESERVED_ENV or key.startswith("REMOTE_DEV_JOB_"):
            raise ValueError("cannot override managed device, service-port, interpreter, or job-token ownership")
        if not isinstance(value, str):
            raise ValueError("env values must stay literal strings")
        cleaned[key] = value
    return cleaned


def normalize_resources(resources: dict[str, Any] | None) -> dict[str, Any]:
    resources = dict(resources or {})
    if "devices" in resources and "npu_count" in resources:
        raise ValueError("supply devices or npu_count, not both")
    if "devices" not in resources and "npu_count" not in resources:
        resources["npu_count"] = 1
    if "devices" in resources:
        devices = resources["devices"]
        if not isinstance(devices, list) or any(type(d) is not int or d < 0 for d in devices):
            raise ValueError("resources.devices must be distinct nonnegative integers")
        if len(set(devices)) != len(devices):
            raise ValueError("resources.devices must be distinct nonnegative integers")
    if "npu_count" in resources and int(resources["npu_count"]) < 1:
        raise ValueError("npu_count must be >= 1")
    return resources


def role_plan(topology: dict[str, Any] | None, resources: dict[str, Any], command: str) -> list[dict[str, Any]]:
    if not topology or not topology.get("roles"):
        item = {"name": "default", "command": command}
        item.update({k: resources[k] for k in ("devices", "npu_count", "service_port") if k in resources})
        return [item]
    roles = []
    for role in topology["roles"]:
        if not isinstance(role, dict) or not str(role.get("name") or "").strip():
            raise ValueError("topology.roles entries need a name")
        name = str(role["name"])
        item = {"name": name, "command": role.get("command") or command}
        if role.get("devices"):
            item["devices"] = list(role["devices"])
        else:
            item["npu_count"] = int(role.get("npu_count") or resources.get("npu_count") or 1)
        if role.get("service_port") is not None:
            item["service_port"] = role["service_port"]
        elif resources.get("service_port") is not None and len(roles) == 0:
            item["service_port"] = resources["service_port"]
        if role.get("host"):
            item["host"] = role["host"]
        if role.get("env"):
            item["env"] = validate_user_env(role["env"])
        roles.append(item)
    return roles


def distinct_hosts_required(roles: list[dict[str, Any]], topology: dict[str, Any] | None = None) -> bool:
    """Default allows same-host roles. Distinct hosts only when explicitly required."""
    if topology and topology.get("distinct_hosts"):
        return True
    named = [str(role["host"]) for role in roles if role.get("host")]
    return len(set(named)) > 1


def host_key(row: dict[str, Any]) -> str:
    return str(row.get("host") or (row.get("host_endpoint") or {}).get("host") or "")


def runtime_matches(row: dict[str, Any], environment: dict[str, Any] | None, role: dict[str, Any] | None = None) -> bool:
    environment = environment or {}
    profile = row.get("profile") or {}
    recipe = environment.get("recipe") or environment.get("image")
    if recipe and (row.get("recipe") or profile.get("recipe")) != recipe:
        return False
    if environment.get("python_abi") and (row.get("python_abi") or profile.get("python_abi")) != environment["python_abi"]:
        return False
    if environment.get("cann") and (row.get("cann") or profile.get("cann")) != environment["cann"]:
        return False
    if environment.get("soc") and (row.get("soc") or profile.get("soc")) != environment["soc"]:
        return False
    if environment.get("machine_type") and (row.get("machine_type") or profile.get("machine_type")) != environment["machine_type"]:
        return False
    if role and role.get("host") and host_key(row) != str(role["host"]):
        return False
    port = (role or {}).get("service_port")
    if port not in {None, 0}:
        declared = row.get("service_ports") or []
        if int(port) not in declared:
            return False
    return True


def select_runtimes(
    catalog: list[dict[str, Any]],
    *,
    user: str,
    roles: list[dict[str, Any]],
    environment: dict[str, Any] | None = None,
    busy_runtime_ids: set[str] | None = None,
    topology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick one matching ready root per role.

    Same-host multi-role is allowed unless topology.distinct_hosts or the roles
    name different hosts. Distinct mutable roots are still required.
    """
    busy_runtime_ids = busy_runtime_ids or set()
    ready = [
        row for row in catalog
        if row.get("state") == "ready" and row.get("user") == user and not row.get("draining")
        and row.get("runtime_id") not in busy_runtime_ids
    ]
    chosen: list[str] = []
    seen_hosts: set[str] = set()
    require_distinct_hosts = distinct_hosts_required(roles, topology)
    for role in roles:
        match = None
        for row in ready:
            rid = row["runtime_id"]
            if rid in chosen:
                continue
            if not runtime_matches(row, environment, role):
                continue
            host = host_key(row)
            if require_distinct_hosts and host and host in seen_hosts:
                continue
            match = row
            break
        if match is None:
            return {"runtime_ids": [], "reason": _gap(roles, ready, environment, topology)}
        chosen.append(match["runtime_id"])
        if host_key(match):
            seen_hosts.add(host_key(match))
    return {"runtime_ids": chosen, "reason": None}


def _gap(roles, ready, environment, topology=None) -> str:
    environment = environment or {}
    recipe = environment.get("recipe") or environment.get("image")
    if recipe and recipe not in SUPPORTED_RECIPES and not any(runtime_matches(row, environment) for row in ready):
        return f"unsupported environment recipe {recipe!r}; no matching prepared root"
    if distinct_hosts_required(roles, topology):
        hosts = {host_key(row) for row in ready if runtime_matches(row, environment)}
        if len(hosts) < len(roles):
            return "not enough distinct hosts with a matching prepared environment for this topology"
    return "no verified ready runtime matches the requested environment and hardware constraints"


def can_prepare(environment: dict[str, Any] | None, donor: dict[str, Any] | None,
                role: dict[str, Any] | None = None) -> bool:
    if donor is None:
        return False
    environment = environment or {}
    recipe = environment.get("recipe") or environment.get("image")
    have_recipe = donor.get("recipe") or (donor.get("profile") or {}).get("recipe")
    if recipe and have_recipe and have_recipe != recipe:
        return False
    if recipe and recipe not in SUPPORTED_RECIPES and have_recipe != recipe:
        return False
    if role and role.get("host") and host_key(donor) != str(role["host"]):
        return False
    for key in ("python_abi", "cann", "soc", "machine_type"):
        wanted = environment.get(key)
        if not wanted:
            continue
        have = donor.get(key) or (donor.get("profile") or {}).get(key)
        if have != wanted:
            return False
    return True
