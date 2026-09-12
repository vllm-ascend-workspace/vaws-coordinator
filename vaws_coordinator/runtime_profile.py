"""Immutable environment and complete native-bundle attestations (stdlib only).

Preparation runs inside an owned, idle container, outside the checkout path.
This module never installs packages, creates containers, or loads a model.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
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


def command_launch_environment(environment: dict[str, str], *, roots: list[str] = ()) -> dict[str, str]:
    """Keep image initialization while excluding previous execution overlays."""
    def scoped(value: str) -> bool:
        return (value.startswith('/tmp/vaws-python-shim.') or
                '/vllm-workspace/executions/' in value or '/vllm-workspace/tasks/' in value or
                any(value == root or value.startswith(root.rstrip('/') + '/') for root in roots if root and root != '/'))
    result = {}
    for key, value in capture_launch_environment(environment).items():
        if key in {'PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'ASCEND_CUSTOM_OPP_PATH'}:
            parts = list(dict.fromkeys(part for part in value.split(':') if part and not scoped(part)))
            if parts:
                result[key] = ':'.join(parts)
        elif not scoped(value):
            result[key] = value
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verified_preparation(preparation: dict, profile: dict) -> dict:
    """Resolve cache keys from measured environment facts, not donor guesses."""
    result = dict(preparation)
    requested = preparation.get('environment', {})
    base = {name: profile.get(name) for name in (
        'image_digest', 'soc', 'driver', 'cann', 'python_abi', 'torch', 'torch_npu', 'compiler', 'system_files')}
    base.update(environment=requested.get('environment', {}), build_env=requested.get('build_env', {}))
    result['environment'] = base
    result['dependency_key'] = digest({'environment': base, 'dependencies': result.get('dependencies', {})})
    result['native_key'] = digest({'environment': base, 'dependencies': result.get('dependencies', {}), 'native': result.get('native', {})})
    return result


def build_toolchain_from_logs(root: Path) -> dict[str, Any]:
    """Read the latest completed package-owned install's build selection.

    Editable wheel builds may remove their temporary CMake cache. Retained
    installer output still records the selected SoC and C++ compilers.
    An incomplete newer attempt cannot borrow an older successful log.
    """
    logs = list((root / ".vaws-runtime/prepare-logs").glob("runtime-install-vllm-ascend.*"))
    if not logs:
        return {}
    path = max(logs, key=lambda item: item.stat().st_mtime_ns)
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^Successfully installed .*\bvllm[-_]ascend-", text, re.MULTILINE):
        raise ValueError("latest vllm-ascend install has no successful completion evidence")
    socs = set(re.findall(r"-- Detected SOC version:\s*([A-Za-z0-9_]+)", text))
    if len(socs) > 1:
        raise ValueError("completed build log contains conflicting SoC selections")
    compilers = sorted(set(re.findall(r"-- The CXX compiler identification is ([^\r\n]+)", text)))
    return {"soc": next(iter(socs), None), "compilers": compilers,
            "path": path.relative_to(root).as_posix(), "sha256": file_digest(path)}


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
    generated = [path for path in (package / '_build_info.py',) if path.is_file()]
    for path in [*extensions, *outputs, *generated]:
        if path.name == ".gitkeep":
            continue
        relative = path.relative_to(root).as_posix()
        checked_file(root, relative)
        files[relative] = "library" if path in extensions or path in binaries else "metadata"
    return files


def profile_key(profile: dict[str, Any]) -> str:
    if profile.get('kind') == 'command':
        if not all(isinstance(profile.get(key), str) and profile[key] for key in ('image_digest', 'python_abi')):
            raise ValueError('command profile requires image and Python ABI evidence')
    elif any(not isinstance(profile.get(key), str) or not profile[key].strip() for key in PROFILE_FIELDS):
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
    if profile.get('kind') == 'command':
        return digest(profile)
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


def native_compatibility_key(manifest: dict[str, Any]) -> str:
    """Identify the already tested native environment independently of a view.

    Python business source and its SCM version may change without changing
    dependencies or native inputs. That source is tested by the execution,
    while the environment and every reused output must still match here.
    """
    profile = manifest['profile']
    inputs = manifest['build_inputs']
    source_inputs = {}
    for name in ('vllm', 'vllm-ascend'):
        row = inputs.get(name, {})
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', row[key])
               for key in ('native', 'dependencies')):
            raise ValueError('native compatibility reuse requires complete build input fingerprints')
        source_inputs[name] = {key: row[key] for key in ('native', 'dependencies')}
    # Normalize only the source/metadata overlay and copied native tree. Other
    # execution roots in a loader search path remain part of the identity.
    # PATH is command lookup, not native loading: managed execution selects
    # the shared donor interpreter by its absolute path.
    root = manifest['runtime_root'].replace('\\', '/').rstrip('/')
    overlays = (root + '/.vaws-runtime/metadata', root + '/vllm', root + '/vllm-ascend')
    native_root = root + '/vllm-ascend/vllm_ascend'
    loader_environment = {}
    for name, value in profile['launch_env'].items():
        if name == 'PATH':
            continue
        if name not in {'PYTHONPATH', 'LD_LIBRARY_PATH', 'ASCEND_CUSTOM_OPP_PATH'}:
            loader_environment[name] = value
            continue
        parts = []
        for part in value.split(':'):
            # An empty or relative component makes loading depend on cwd.
            # A fresh execution root therefore needs its own import smoke.
            if not part or not PurePosixPath(part).is_absolute():
                raise ValueError('native compatibility reuse requires absolute loader search paths')
            if name == 'PYTHONPATH' and part in overlays:
                part = '$EXECUTION_ROOT' + part[len(root):]
            elif name != 'PYTHONPATH' and (part in {native_root, native_root + '/_cann_ops_custom'}
                                            or part.startswith(native_root + '/_cann_ops_custom/')):
                part = '$EXECUTION_NATIVE_ROOT' + part[len(native_root):]
            if part and part not in parts:
                parts.append(part)
        loader_environment[name] = ':'.join(parts)
    environment = {name: profile[name] for name in PROFILE_FIELDS if name not in ('vllm', 'vllm_ascend')}
    environment.update(build_env=profile['build_env'], system_files=profile['system_files'],
                       packages=profile.get('packages', {}),
                       launch_env=loader_environment)
    return digest({'environment': environment, 'inputs': source_inputs, 'files': manifest['files']})


def native_source_mapping(root: Path) -> dict[str, str]:
    """Resolve the execution overlay without importing business/native code."""
    root = root.resolve()
    result = {}
    for name, source in (('vllm', 'vllm'), ('vllm_ascend', 'vllm-ascend')):
        spec = importlib.util.find_spec(name)
        expected = root / source / name
        if not spec or not spec.origin or Path(spec.origin).resolve() != expected / '__init__.py':
            raise ValueError('module mapping escaped the execution source view: ' + name)
        result[name] = str(Path(spec.origin).resolve())
        dist = importlib.metadata.distribution(source)
        metadata_root = root / '.vaws-runtime/metadata'
        if Path(dist.locate_file('')).resolve() != metadata_root:
            raise ValueError('distribution metadata escaped the execution source view: ' + source)
        result[source + '_version'] = dist.version
    extension = importlib.machinery.PathFinder.find_spec(
        'vllm_ascend.vllm_ascend_C', [str(root / 'vllm-ascend/vllm_ascend')])
    if (not extension or not extension.origin
            or not isinstance(extension.loader, importlib.machinery.ExtensionFileLoader)
            or Path(extension.origin).resolve().parent != root / 'vllm-ascend/vllm_ascend'):
        raise ValueError('extension mapping escaped the execution source view')
    result['extension'] = str(Path(extension.origin).resolve())
    return result


def native_compatibility_receipt(root: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Copy an already verified donor's original import proof, without chains."""
    try:
        key = native_compatibility_key(manifest)
    except (KeyError, ValueError):
        # Older incomplete attestations still need the ordinary import smoke.
        return None
    evidence = manifest['evidence']['smoke']
    data = checked_file(root, evidence['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != evidence['sha256']:
        raise ValueError('donor import evidence changed after verification')
    smoke = json.loads(data)
    if smoke.get('kind') == 'native-compatibility-reuse':
        return smoke['compatibility']
    if smoke.get('passed') is not True:
        raise ValueError('native compatibility requires a successful original import')
    return {'key': key, 'origin': {'profile_key': manifest['profile_key'],
                                  'build_key': manifest['build_key'], 'build_inputs': manifest['build_inputs'],
                                  'smoke': smoke}}


def verify_native_compatibility(root: Path, manifest: dict[str, Any], smoke: dict[str, Any], *, check_environment: bool):
    certificate = smoke.get('compatibility') or {}
    origin = certificate.get('origin') or {}
    original = origin.get('smoke') or {}
    if (smoke.get('python_import_executed') is not False or 'passed' in smoke
            or smoke.get('profile_key') != manifest['profile_key'] or smoke.get('build_inputs') != manifest['build_inputs']
            or any(not re.fullmatch(r'[0-9a-f]{64}', str(origin.get(key, ''))) for key in ('profile_key', 'build_key'))
            or certificate.get('key') != native_compatibility_key(manifest)
            or original.get('passed') is not True or original.get('kind') == 'native-compatibility-reuse'
            or original.get('profile_key') not in (None, origin.get('profile_key'))
            or original.get('build_inputs') not in (None, origin.get('build_inputs'))):
        raise ValueError('reused native compatibility evidence does not match this environment and bundle')
    if check_environment and native_source_mapping(root) != smoke.get('source_mapping'):
        raise ValueError('execution source or SCM metadata mapping changed')


def verify_environment(root: Path, manifest: dict[str, Any]) -> None:
    """Check mutable environment facts without re-reading native outputs."""
    if str(root.resolve()) != manifest['runtime_root']:
        raise ValueError('runtime relocation needs separate validation')
    profile = manifest['profile']
    if sysconfig.get_config_var('SOABI') != profile['python_abi']:
        raise ValueError('Python ABI changed')
    for key, package in PACKAGES.items():
        if importlib.metadata.version(package) != profile[key]:
            raise ValueError(f'installed package changed: {package}')
    for package, version in profile.get('packages', {}).items():
        if importlib.metadata.version(package) != version:
            raise ValueError(f'profile dependency changed: {package}')
    for row in profile['system_files'].values():
        if file_digest(Path(row['path'])) != row['sha256']:
            raise ValueError('CANN/driver/runtime support file changed')


def verify_execution_view(root: Path, manifest: dict[str, Any]) -> None:
    """Check an owned view at launch, reusing its completed native publication.

    Native bytes were checked as they were copied into this private view. They
    are not mutable inputs of a managed execution. Explicit adoption/repair and
    native builds still use ``verify`` to validate the complete bundle.
    """
    verify_environment(root, manifest)
    row = manifest['evidence']['smoke']
    data = checked_file(root, row['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != row['sha256']:
        raise ValueError('execution view evidence changed')
    smoke = json.loads(data)
    if native_source_mapping(root) != smoke.get('source_mapping'):
        raise ValueError('execution source or SCM metadata mapping changed')


def verify(root: Path, manifest: dict[str, Any], *, check_environment: bool = True) -> None:
    if manifest.get('schema_version') == 2 and manifest.get('profile', {}).get('kind') == 'command':
        if profile_key(manifest['profile']) != manifest['profile_key']:
            raise ValueError('command environment identity mismatch')
        if digest({'profile': manifest['profile'], 'sources': manifest.get('source_id')}) != manifest.get('build_key'):
            raise ValueError('command source identity mismatch')
        if check_environment and (str(root.resolve()) != manifest['runtime_root'] or
                                  sysconfig.get_config_var('SOABI') != manifest['profile']['python_abi']):
            raise ValueError('command interpreter or execution root changed')
        return
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
    try:
        smoke = json.loads(checked_file(root, manifest['evidence']['smoke']['path']).read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('import-smoke evidence must be a successful structured receipt') from exc
    if smoke.get('kind') == 'native-compatibility-reuse':
        verify_native_compatibility(root, manifest, smoke, check_environment=check_environment)
    elif smoke.get('passed') is not True:
        raise ValueError('import-smoke evidence did not pass')
    if smoke.get('profile_key') not in (None, manifest['profile_key']):
        raise ValueError('import-smoke evidence belongs to another profile')
    if smoke.get('build_inputs') not in (None, manifest['build_inputs']):
        raise ValueError('import-smoke evidence belongs to different build inputs')
    if check_environment:
        verify_environment(root, manifest)


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
