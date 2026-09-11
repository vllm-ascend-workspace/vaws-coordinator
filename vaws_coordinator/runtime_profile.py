"""Immutable environment and complete native-bundle attestations (stdlib only).

Preparation runs inside an owned, idle container, outside the checkout path.
This module never installs packages, creates containers, or loads a model.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import sysconfig
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PROFILE_FIELDS = ("image_digest", "soc", "driver", "cann", "python_abi",
                  "torch", "torch_npu", "vllm", "vllm_ascend", "compiler")
PACKAGES = {"torch": "torch", "torch_npu": "torch-npu", "vllm": "vllm",
            "vllm_ascend": "vllm-ascend"}
LAUNCH_PATH_KEYS = (
    "PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "ASCEND_HOME_PATH",
    "ASCEND_OPP_PATH", "ASCEND_AICPU_PATH", "ASCEND_TOOLKIT_HOME",
    "ASCEND_CUSTOM_OPP_PATH", "ATB_HOME_PATH", "TOOLCHAIN_HOME", "SOC_VERSION",
)


def capture_launch_environment(environment: dict[str, str]) -> dict[str, str]:
    """Carry CANN discovery paths into the clean managed-worker environment.

    Device assignment belongs to the host lease. A temporary Python shim is
    removed by the preparation shell and must not become profile identity.
    """
    captured = {key: environment[key] for key in LAUNCH_PATH_KEYS if environment.get(key)}
    shim = environment.get("VAWS_PYTHON_SHIM_DIR")
    if shim and "PATH" in captured:
        # The environment describes the remote Linux container, even when
        # its preparation plan is built by a Windows client.
        captured["PATH"] = ":".join(part for part in captured["PATH"].split(":") if part != shim)
    return captured


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def checked_file(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if name.is_absolute() or not name.parts or ".." in name.parts:
        raise ValueError(f"unsafe bundle path: {relative}")
    path = root / relative
    # Symlinked outputs are not a portable complete bundle. Enumerate their
    # real files during preparation rather than retaining external references.
    if any((root / Path(*name.parts[:i])).is_symlink() for i in range(1, len(name.parts) + 1)):
        raise ValueError(f"symlink in bundle path: {relative}")
    if not path.is_file():
        raise ValueError(f"missing required artifact: {relative}")
    if path.name == "CMakeCache.txt":
        raise ValueError("CMakeCache.txt is not a reusable runtime artifact")
    return path


def installed_native_files(root: Path) -> dict[str, str]:
    """Enumerate the installed editable extension and complete custom-op tree.

    Build intermediates and unrelated venv libraries cannot attest an install.
    Keep every vendor binary/configuration instead of sampling the first file.
    Flatten internal file aliases so the bundle also contains loadable aliases;
    external links and directory links are not portable installed artifacts.
    """
    root = root.resolve()
    for name in ("vllm-ascend", "vllm-ascend/vllm_ascend", "vllm-ascend/vllm_ascend/_cann_ops_custom"):
        if (root / name).is_symlink():
            raise ValueError("symlinked installed bundle directory: " + name)
    package = root / "vllm-ascend/vllm_ascend"
    extensions = sorted(package.glob("*.so"))
    if not any(path.name.startswith("vllm_ascend_C") for path in extensions):
        raise ValueError("cannot attest installed vllm_ascend_C extension")
    vendor = package / "_cann_ops_custom"
    for path in sorted(vendor.rglob("*")):
        if not path.is_symlink():
            continue
        target = path.resolve()
        if not path.is_file() or not target.is_relative_to(vendor.resolve()):
            raise ValueError("external or directory symlink in installed bundle: " + str(path))
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".attest-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copy2(target, temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    outputs = sorted(path for path in vendor.rglob("*") if path.is_file())
    binaries = [path for path in outputs if path.suffix in {".so", ".o"} or ".so." in path.name]
    configs = [path for path in outputs if path.suffix in {".json", ".ini"}]
    if not binaries or not configs:
        raise ValueError("cannot attest complete installed custom-op binaries and metadata")
    files = {}
    for path in [*extensions, *outputs]:
        if path.name == ".gitkeep":
            continue
        relative = path.relative_to(root).as_posix()
        checked_file(root, relative)
        files[relative] = "library" if path in extensions or path in binaries else "metadata"
    return files


def profile_key(profile: dict[str, Any]) -> str:
    if any(not isinstance(profile.get(key), str) or not profile[key].strip() for key in PROFILE_FIELDS):
        raise ValueError("profile requires exact versions: " + ", ".join(PROFILE_FIELDS))
    for key in ("build_env", "launch_env"):
        if not isinstance(profile.get(key), dict):
            raise ValueError(f"profile requires {key}")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in profile[key].items()):
            raise ValueError(f"{key} must contain string values")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) for k in profile[key]):
            raise ValueError("invalid profile environment variable name")
        if any(re.search(r"(?:^|_)(?:API_?KEY|ACCESS_?KEY|AUTH|CREDENTIAL|PASS(?:WORD)?|SECRET|TOKEN)(?:_|$)", k) for k in profile[key]):
            raise ValueError("profile environment must not contain secrets")
    if not profile.get("compatibility_evidence"):
        raise ValueError("profile requires an operator-reviewed compatibility evidence reference")
    if not {"cann", "driver"}.issubset(profile.get("system_files", {})):
        raise ValueError("profile requires actual CANN and driver version-file hashes")
    for row in profile["system_files"].values():
        absolute = PurePosixPath(row["path"]).is_absolute() or PureWindowsPath(row["path"]).is_absolute()
        if not absolute or len(row["sha256"]) != 64:
            raise ValueError("system file identity requires an absolute path and SHA256")
    return digest(profile)


def launch_preamble(profile: dict[str, Any], python: str | None = None) -> str:
    """Keep image-provided acl/native-compat paths when adding scoped paths."""
    profile_key(profile)
    lines = []
    for key, value in sorted(profile["launch_env"].items()):
        if key in {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"} and not value:
            continue
        suffix = '${' + key + ':+:$' + key + '}' if key in {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"} else ""
        lines.append(f"export {key}={shlex.quote(value)}\"{suffix}\"")
    if python:
        bindir = str(PurePosixPath(python).parent)
        lines.append(f'export PATH={shlex.quote(bindir)}"${{PATH:+:$PATH}}"')
        lines.append(f"export VAWS_PYTHON={shlex.quote(python)}")
    return "\n".join(lines)


def build_key(profile: dict[str, Any], inputs: dict[str, Any]) -> str:
    if not inputs.get("vllm") or not inputs.get("vllm-ascend"):
        raise ValueError("native input fingerprints for both repositories are required")
    return digest({"profile": profile_key(profile), "inputs": inputs})


def capture(root: Path, profile: dict[str, Any], inputs: dict[str, Any],
            files: dict[str, str], evidence: dict[str, str]) -> dict[str, Any]:
    """Describe explicitly enumerated outputs and compatibility/version evidence.

    Roles must include libraries AND metadata. Operators must enumerate every
    vendor configuration/binary required by their particular build.
    """
    if not {"library", "metadata"}.issubset(set(files.values())):
        raise ValueError("a complete bundle requires library and metadata roles")
    if not {"cann", "driver", "smoke"}.issubset(evidence):
        raise ValueError("CANN, driver and successful import-smoke evidence files are required")
    root = root.resolve()
    return {
        "schema_version": 1, "profile": profile, "profile_key": profile_key(profile),
        "build_inputs": inputs, "build_key": build_key(profile, inputs),
        # Relocation is deliberately not assumed for editable installs/operators.
        "runtime_root": str(root),
        "files": {name: {"sha256": file_digest(checked_file(root, name)), "role": role}
                  for name, role in sorted(files.items())},
        "evidence": {role: {"path": name, "sha256": file_digest(checked_file(root, name))}
                     for role, name in sorted(evidence.items())},
    }


def verify(root: Path, manifest: dict[str, Any], *, check_environment: bool = True) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported runtime manifest")
    if profile_key(manifest["profile"]) != manifest["profile_key"] or build_key(manifest["profile"], manifest["build_inputs"]) != manifest["build_key"]:
        raise ValueError("profile/build identity mismatch")
    if not {"library", "metadata"}.issubset({row["role"] for row in manifest["files"].values()}):
        raise ValueError("incomplete native bundle")
    if not {"cann", "driver", "smoke"}.issubset(manifest["evidence"]):
        raise ValueError("missing environment evidence")
    for name, row in manifest["files"].items():
        if file_digest(checked_file(root, name)) != row["sha256"]:
            raise ValueError(f"artifact hash mismatch: {name}")
    for row in manifest["evidence"].values():
        if file_digest(checked_file(root, row["path"])) != row["sha256"]:
            raise ValueError("environment evidence changed")
    if check_environment:
        if str(root.resolve()) != manifest["runtime_root"]:
            raise ValueError("runtime relocation needs separate validation")
        profile = manifest["profile"]
        if sysconfig.get_config_var("SOABI") != profile["python_abi"]:
            raise ValueError("Python ABI changed")
        for key, package in PACKAGES.items():
            if importlib.metadata.version(package) != profile[key]:
                raise ValueError(f"installed package changed: {package}")
        for package, version in profile.get("packages", {}).items():
            if importlib.metadata.version(package) != version:
                raise ValueError(f"profile dependency changed: {package}")
        for row in profile["system_files"].values():
            if file_digest(Path(row["path"])) != row["sha256"]:
                raise ValueError("CANN/driver/runtime support file changed")


def publish(root: Path, cache: Path, manifest: dict[str, Any]) -> Path:
    """Atomically publish a complete, immutable bundle; no partial cache hits."""
    verify(root, manifest)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / manifest["build_key"]
    if destination.exists():
        existing = json.loads((destination / "manifest.json").read_text())
        # A new successful smoke log can contain a new timestamp/PID without
        # changing native outputs. Keep the original verified evidence bundle.
        identity_fields = ("schema_version", "profile", "profile_key", "build_inputs", "build_key", "runtime_root", "files")
        if any(existing.get(key) != manifest.get(key) for key in identity_fields):
            raise ValueError("same build inputs produced different artifacts; inspect reproducibility")
        verify(destination, existing, check_environment=False)
        return destination
    temp = Path(tempfile.mkdtemp(prefix=".publish-", dir=cache))
    try:
        names = set(manifest["files"]) | {row["path"] for row in manifest["evidence"].values()}
        if "manifest.json" in names:
            raise ValueError("reserved bundle path: manifest.json")
        for name in names:
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked_file(root, name), target)
        (temp / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        verify(temp, manifest, check_environment=False)
        os.rename(temp, destination)
        return destination
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def restore(root: Path, bundle: Path, expected_build_key: str) -> None:
    """Preparation-only restoration, after owner proves all its workers stopped."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest["build_key"] != expected_build_key or str(root.resolve()) != manifest["runtime_root"]:
        raise ValueError("cache miss: build identity or installation path differs")
    verify(bundle, manifest, check_environment=False)
    names = set(manifest["files"]) | {row["path"] for row in manifest["evidence"].values()}
    for name in names:
        target = root / name
        # Reject symlinked destinations, including existing parent directories.
        for parent in [target, *target.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("unsafe artifact restore destination")
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / name, target)
    verify(root, manifest)
