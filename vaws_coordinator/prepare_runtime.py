#!/usr/bin/env python3
"""Attest/cache an already built runtime inside a prepared work root.

Use existing machine-management/parity installers BEFORE this command. No
package installation, container creation, card allocation or model loading.
The user container `vaws-<user>` is not created or deleted here.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from vaws_coordinator.build_inputs import runtime_build_inputs
from vaws_coordinator.git_sources import discover_repo_tree, iter_postorder
from vaws_coordinator.runtime_profile import capture, file_digest, profile_key, publish, restore, verify


CANN_VERSION_CANDIDATES = (
    "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
    "/usr/local/Ascend/cann/version.cfg",
    "/usr/local/Ascend/ascend-toolkit/latest/arm64-linux/ascend_toolkit_install.info",
)
DRIVER_VERSION_CANDIDATES = (
    "/usr/local/Ascend/driver/version.info",
    "/usr/local/Ascend/driver/version.cfg",
)


REMOTE_CAPTURE_SUFFIX = r'''
import importlib.metadata
import os
import subprocess
import sys
import sysconfig

args = json.loads(sys.argv[1])
root = Path(args["root"])
recipe = args.get("recipe")
image_digest = args.get("image_digest")
if not image_digest:
    raise ValueError("cannot attest image digest")

def first_existing(paths):
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            return candidate
    return None

def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError(f"cannot attest {name}: not installed") from exc

cann_file = first_existing(args.get("cann_files") or [])
driver_file = first_existing(args.get("driver_files") or [])
if cann_file is None or driver_file is None:
    raise ValueError("cannot attest CANN/driver version files")

soc = os.environ.get("SOC_VERSION") or os.environ.get("VAWS_SOC_VERSION")
compiler = os.environ.get("CXX") or os.environ.get("C_COMPILER") or os.environ.get("CXX_COMPILER")
if not compiler:
    # Use the actual custom-op build selection when no compiler was exported.
    cache = root / "vllm-ascend/csrc/build/CMakeCache.txt"
    if cache.is_file():
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_CXX_COMPILER:FILEPATH="):
                compiler = line.partition("=")[2].strip()
                break
toolchain = build_toolchain_from_logs(root) if not soc or not compiler else {}
soc = soc or toolchain.get("soc")
compiler = compiler or "; ".join(toolchain.get("compilers") or [])
if not soc:
    raise ValueError("cannot attest soc from environment or completed build evidence")
if not compiler:
    raise ValueError("cannot attest compiler from environment or completed build evidence")
python_abi = sysconfig.get_config_var("SOABI")
if not python_abi:
    raise ValueError("cannot attest python_abi")

profile = {
    "image_digest": image_digest,
    "soc": soc,
    "driver": driver_file.read_text(errors="replace").strip()[:200] or "present",
    "cann": cann_file.read_text(errors="replace").strip()[:200] or "present",
    "python_abi": python_abi,
    "torch": package_version("torch"),
    "torch_npu": package_version("torch-npu"),
    "vllm": package_version("vllm"),
    "vllm_ascend": package_version("vllm-ascend"),
    "compiler": compiler,
    "build_env": {},
    "launch_env": {},
    "compatibility_evidence": ".vaws-runtime/profile-evidence/smoke.json",
    "system_files": {
        "cann": {"path": str(cann_file), "sha256": file_digest(cann_file)},
        "driver": {"path": str(driver_file), "sha256": file_digest(driver_file)},
    },
}
if recipe:
    profile["recipe"] = recipe
if args.get("machine_type"):
    profile["machine_type"] = args["machine_type"]
profile["launch_env"] = capture_launch_environment(dict(os.environ))

files = installed_native_files(root)

evidence_dir = root / ".vaws-runtime/profile-evidence"
evidence_dir.mkdir(parents=True, exist_ok=True)
result = subprocess.run(
    [sys.executable, "-c", "import torch_npu, vllm, vllm_ascend, acl; import vllm_ascend.vllm_ascend_C"],
    capture_output=True, text=True, encoding="utf-8", timeout=30,
)
smoke = {"passed": result.returncode == 0, "profile_key": profile_key(profile),
         "stdout": result.stdout[-4000:], "stderr": result.stderr[-8000:]}
(evidence_dir / "smoke.json").write_text(json.dumps(smoke, indent=2) + "\n")
if not smoke["passed"]:
    raise ValueError("installed runtime import smoke failed; inspect profile-evidence/smoke.json")
(evidence_dir / "cann.json").write_text(json.dumps(profile["system_files"]["cann"], sort_keys=True) + "\n")
(evidence_dir / "driver.json").write_text(json.dumps(profile["system_files"]["driver"], sort_keys=True) + "\n")
inputs = _build_namespace["runtime_build_inputs"](root, profile, profile_key(profile))
evidence = {name: ".vaws-runtime/profile-evidence/" + name + ".json" for name in ("cann", "driver", "smoke")}
if toolchain:
    evidence["toolchain_log"] = toolchain["path"]
manifest = capture(root, profile, inputs, files, evidence)
verify(root, manifest)
marker = root / ".vaws-runtime/ready-profile.json"
temp = marker.with_suffix(".tmp")
temp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
os.replace(temp, marker)
print(json.dumps(manifest))
'''


def require_clean_sources(root: Path):
    for name in ("vllm", "vllm-ascend"):
        for node in iter_postorder(discover_repo_tree(root / name, name)):
            dirty = subprocess.check_output(["git", "-C", str(node.repo_path), "status", "--porcelain", "--untracked-files=all"], text=True, encoding="utf-8")
            if dirty.strip():
                raise ValueError("attest a clean materialized parity snapshot, including child submodules")
            for child in node.children:
                path = child.repo_path.relative_to(node.repo_path).as_posix()
                head = subprocess.check_output(["git", "-C", str(child.repo_path), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
                entry = subprocess.check_output(["git", "-C", str(node.repo_path), "ls-tree", "HEAD", "--", path], text=True, encoding="utf-8").strip()
                if entry != f"160000 commit {head}\t{path}":
                    raise ValueError("native submodule must be tracked at its pinned commit: " + child.relpath)


def attest(root: Path, spec: dict):
    profile = spec["profile"]
    key = profile_key(profile)
    require_clean_sources(root)
    inputs = runtime_build_inputs(root, profile, key)
    evidence_dir = root / ".vaws-runtime/profile-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Preserve the prepared image environment when overlaying launch settings.
    environment = os.environ.copy()
    for name, value in profile["launch_env"].items():
        if name in {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"} and not value:
            continue
        environment[name] = value + (":" + environment[name] if name in {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"} and environment.get(name) else "")
    try:
        smoke = subprocess.run([sys.executable, "-c", "import torch_npu, vllm, vllm_ascend, acl; import vllm_ascend.vllm_ascend_C"], env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        (evidence_dir / "smoke.json").write_text(json.dumps({"passed": False,
                                                             "build_inputs": inputs, "profile_key": key,
                                                             "error": "import smoke timed out after 60s",
                                                             "stderr": stderr[-8000:]}, indent=2))
        raise ValueError("import smoke timed out; inspect .vaws-runtime/profile-evidence/smoke.json") from exc
    (evidence_dir / "smoke.json").write_text(json.dumps({"passed": smoke.returncode == 0,
                                                       "build_inputs": inputs, "profile_key": key,
                                                       "stderr": smoke.stderr[-8000:]}, indent=2))
    if smoke.returncode:
        raise ValueError("import smoke failed; inspect .vaws-runtime/profile-evidence/smoke.json")
    for name in ("cann", "driver"):
        row = profile["system_files"][name]
        if file_digest(Path(row["path"])) != row["sha256"]:
            raise ValueError("actual environment differs from requested profile")
        (evidence_dir / (name + ".json")).write_text(json.dumps(row, sort_keys=True))
    evidence = {name: ".vaws-runtime/profile-evidence/" + name + ".json" for name in ("cann", "driver", "smoke")}
    manifest = capture(root, profile, inputs, spec["files"], evidence)
    verify(root, manifest)
    # New attestations replace the marker only after all checks have passed.
    marker = root / ".vaws-runtime/ready-profile.json"
    temp = marker.with_suffix(".tmp")
    temp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.replace(temp, marker)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("attest", "publish", "restore"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--build-key")
    parser.add_argument("--owned-workers-stopped", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("preparation runs inside the owned Linux container")
    if not args.owned_workers_stopped:
        parser.error("first stop and verify only your workers, then pass --owned-workers-stopped")
    root = args.root.resolve()
    if args.action == "attest":
        if args.spec is None:
            parser.error("attest requires --spec")
        result = attest(root, json.loads(args.spec.read_text()))
    elif args.action == "publish":
        if args.cache is None:
            parser.error("publish requires --cache")
        manifest = json.loads((root / ".vaws-runtime/ready-profile.json").read_text())
        result = {"bundle": str(publish(root, args.cache, manifest)), "build_key": manifest["build_key"]}
    else:
        if args.cache is None or not args.build_key:
            parser.error("restore requires --cache and --build-key")
        if len(args.build_key) != 64 or any(c not in "0123456789abcdef" for c in args.build_key):
            parser.error("build key must be a SHA256 digest")
        bundle = args.cache / args.build_key
        manifest = json.loads((bundle / "manifest.json").read_text())
        if runtime_build_inputs(root, manifest["profile"], manifest["profile_key"]) != manifest["build_inputs"]:
            raise ValueError("cache miss: current source inputs differ from the requested bundle")
        restore(root, bundle, args.build_key)
        marker = root / ".vaws-runtime/ready-profile.json"
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, marker)
        result = {"status": "restored", "build_key": args.build_key}
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
