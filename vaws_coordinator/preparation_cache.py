"""Same-container preparation reuse, executed inside a fresh execution view.

Published roots are never modified. Venvs stay at their original absolute path;
native reuse shares that interpreter and overlays fully copied package outputs.
Native changes copy only dependency packages into a fresh venv before building.
"""
from __future__ import annotations

import importlib.metadata
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path


def safe_destination(root: Path, relative: str) -> Path:
    """Published artifacts may never be redirected through a source symlink."""
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe execution artifact destination')
    for part in (root, *(root / Path(*path.parts[:index]) for index in range(1, len(path.parts) + 1))):
        if part.is_symlink():
            raise ValueError('symlink in execution artifact destination: ' + str(part))
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('execution artifact destination escaped its root')
    return target


def copy_dependencies(source_site: Path, destination_site: Path) -> list[str]:
    """Copy dependencies without reusing editable finders or business packages."""
    copied = []
    destination_site.mkdir(parents=True, exist_ok=True)
    for source in source_site.iterdir():
        normalized = source.name.lower().replace('-', '_')
        if normalized.startswith(('vllm', '__editable__', 'pip', 'setuptools', '_distutils_hack')):
            continue
        if source.suffix == '.pth' and ('editable' in source.name or 'vllm' in source.read_text(errors='replace').lower()):
            continue
        target = safe_destination(destination_site, source.name)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        copied.append(source.name)
    return copied


def write_source_metadata(root: Path, versions: dict) -> None:
    """Generate source version files and distribution overlays from real SCM."""
    metadata_root = safe_destination(root, '.vaws-runtime/metadata')
    metadata_root.mkdir(parents=True, exist_ok=True)
    for name, row in versions.items():
        version = row.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9.!+_-]+', version):
            raise ValueError('missing captured SCM version: ' + name)
        if name not in {'vllm', 'vllm-ascend'}:
            raise ValueError('unsupported native source metadata: ' + name)
        package = name.replace('-', '_')
        dist = importlib.metadata.distribution(name)
        installed = dist.version
        # vLLM's supported empty-device recipe adds this package suffix.
        package_version = version + ('.empty' if '+' in version else '+empty') if name == 'vllm' and installed.endswith('empty') else version
        metadata_relative = '.vaws-runtime/metadata/' + package + '-' + package_version + '.dist-info'
        destination = safe_destination(root, metadata_relative)
        destination.mkdir(exist_ok=True)
        metadata = dist.read_text('METADATA')
        if not metadata:
            raise ValueError('installed distribution has no metadata: ' + name)
        metadata = re.sub(r'^Version: .*$', 'Version: ' + package_version, metadata, count=1, flags=re.MULTILINE)
        safe_destination(root, metadata_relative + '/METADATA').write_text(metadata, encoding='utf-8')
        for filename in ('entry_points.txt', 'top_level.txt', 'WHEEL'):
            value = dist.read_text(filename)
            output = safe_destination(root, metadata_relative + '/' + filename)
            if value is not None:
                output.write_text(value, encoding='utf-8')
            else:
                output.unlink(missing_ok=True)
        from setuptools_scm import dump_version
        version_file = safe_destination(root, name + '/' + package + '/_version.py')
        # Use the recipe's own version writer, including prerelease/local tuple
        # semantics; preserve the real HEAD rather than synthetic ancestry.
        dump_version(str(root / name), version, package + '/_version.py')
        with version_file.open('a', encoding='utf-8') as stream:
            stream.write('\n__commit_id__ = commit_id = ' + repr(row.get('source_head')) + '\n')


def copy_native_view(root: Path, source_root: Path, manifest: dict, versions: dict, *, verify_donor=True) -> dict:
    # `verify`, `checked_file` and `capture` are injected from runtime_profile
    # into the remote preparation script; this module has no remote imports.
    if verify_donor:
        verify(source_root, manifest)
    if root.resolve() == source_root.resolve():
        raise ValueError('native view destination must be a new execution root')
    build_info = 'vllm-ascend/vllm_ascend/_build_info.py'
    if (source_root / build_info).is_file() and build_info not in manifest['files']:
        raise ValueError('generated build metadata has no verified donor hash: ' + build_info)
    for name, expected in manifest['files'].items():
        target = safe_destination(root, name)
        if target.exists():
            raise ValueError('native output already exists in fresh view: ' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checked_file(source_root, name), target)
        # A bound donor may change between verification and copying. The new
        # view must retain the previously verified bytes, never re-attest them.
        if file_digest(checked_file(root, name)) != expected['sha256']:
            raise ValueError('copied artifact differs from verified donor hash: ' + name)
    write_source_metadata(root, versions)
    toolchain = build_toolchain_from_logs(source_root)
    return {'kind': 'native', 'source_root': str(source_root), 'build_key': manifest['build_key'],
            'soc': manifest['profile']['soc'], 'compiler': manifest['profile']['compiler'],
            'toolchain': toolchain}


def native_view_launch_environment(manifest: dict, root: Path) -> dict:
    """Move only this view's source/metadata and native loader paths."""
    source = manifest['runtime_root'].rstrip('/')
    destination = root.as_posix().rstrip('/')
    overlays = {source + suffix for suffix in ('/.vaws-runtime/metadata', '/vllm', '/vllm-ascend')}
    native = source + '/vllm-ascend/vllm_ascend'
    result = dict(manifest['profile']['launch_env'])
    for key in ('PYTHONPATH', 'LD_LIBRARY_PATH', 'ASCEND_CUSTOM_OPP_PATH'):
        parts = []
        for part in result.get(key, '').split(':'):
            if key == 'PYTHONPATH' and part in overlays:
                part = destination + part[len(source):]
            elif key != 'PYTHONPATH' and (part in {native, native + '/_cann_ops_custom'}
                                         or part.startswith(native + '/_cann_ops_custom/')):
                part = destination + part[len(source):]
            if part and part not in parts:
                parts.append(part)
        if key == 'PYTHONPATH':
            current = [destination + suffix for suffix in ('/.vaws-runtime/metadata', '/vllm', '/vllm-ascend')]
            parts = list(dict.fromkeys([*current, *parts]))
        if parts:
            result[key] = ':'.join(parts)
    return result


def prepare_native_view(root: Path, source_root: Path, donor: dict, args: dict) -> dict:
    """Publish a Python source view from the existing native proof in one step.

    Copying checks every output against the recorded hash once. The environment
    and original import evidence are reused, not re-captured or marked passed.
    The caller has already materialized and checked the fixed Git snapshot.
    """
    if digest(donor) != args['donor_manifest_digest']:
        raise ValueError('native donor manifest changed; inspect or repair that environment')
    verify_environment(source_root, donor)
    compatibility = native_compatibility_receipt(source_root, donor)
    if compatibility is None:
        raise ValueError('native donor needs an original environment compatibility proof')
    for name in ('vllm', 'vllm-ascend'):
        for field in ('native', 'dependencies'):
            if args['build_inputs'][name][field] != donor['build_inputs'][name][field]:
                raise ValueError('native reuse inputs changed: ' + name + '/' + field)
    if args['build_env'] != donor['profile']['build_env']:
        raise ValueError('native reuse build environment changed')
    reuse = copy_native_view(root, source_root, donor, args['versions'], verify_donor=False)
    current = copy.deepcopy(donor)
    current['runtime_root'] = str(root.resolve())
    profile = current['profile']
    profile['source_versions'] = args['versions']
    profile['compatibility_evidence'] = '.vaws-runtime/profile-evidence/smoke.json'
    profile['launch_env'] = native_view_launch_environment(donor, root)
    old_overlays = {str(source_root / suffix) for suffix in ('.vaws-runtime/metadata', 'vllm', 'vllm-ascend')}
    sys.path[:] = [str(root / suffix) for suffix in ('.vaws-runtime/metadata', 'vllm', 'vllm-ascend')] + [
        path for path in sys.path if path not in old_overlays]
    mapping = native_source_mapping(root)
    profile.update(vllm=mapping['vllm_version'], vllm_ascend=mapping['vllm-ascend_version'])
    current['profile_key'] = profile_key(profile)
    environment = {**profile['build_env'], 'VAWS_ENVIRONMENT_FINGERPRINT': current['profile_key']}
    environment = {key: environment[key] for key in BUILD_INPUT_ENV_KEYS if key in environment}
    fingerprint = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    current['build_inputs'] = {name: {**row, 'build_env': fingerprint} for name, row in args['build_inputs'].items()}
    current['build_key'] = build_key(profile, current['build_inputs'])
    current['preparation'] = verified_preparation(args['preparation'], profile)
    current['execution_view'] = {'source_id': args['source_id'], 'native_from': donor['build_key']}
    smoke = {'kind': 'native-compatibility-reuse', 'python_import_executed': False,
             'profile_key': current['profile_key'], 'build_inputs': current['build_inputs'],
             'compatibility': compatibility, 'source_mapping': mapping}
    verify_native_compatibility(root, current, smoke, check_environment=False)
    evidence = {}
    for name in ('cann', 'driver'):
        row = donor['evidence'][name]
        data = checked_file(source_root, row['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('native donor environment evidence changed: ' + name)
        target = safe_destination(root, row['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        evidence[name] = dict(row)
    for name, relative, value in (
        ('smoke', '.vaws-runtime/profile-evidence/smoke.json', smoke),
        ('reuse', '.vaws-runtime/reuse.json', reuse),
    ):
        target = safe_destination(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
        target.write_bytes(data)
        evidence[name] = {'path': relative, 'sha256': hashlib.sha256(data).hexdigest()}
    current['evidence'] = evidence
    build_source = safe_destination(root, '.vaws-runtime/build-source.json')
    build_source.write_text(json.dumps({'versions': args['versions'], 'build_env': args['build_env']}) + '\n')
    marker = safe_destination(root, '.vaws-runtime/ready-profile.json')
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(current, sort_keys=True, indent=2) + '\n')
    os.replace(temporary, marker)
    # Files have not changed: the caller already owns their complete identity.
    # Return only the new view facts rather than another 300 KB native manifest.
    return {'manifest': {key: value for key, value in current.items() if key != 'files'},
            'manifest_digest': digest(current)}


REMOTE_NATIVE_VIEW_SUFFIX = r'''
args = json.loads(sys.argv[1])
source_root = Path(args['source_root'])
donor = json.loads((source_root / '.vaws-runtime/ready-profile.json').read_text())
print(json.dumps(prepare_native_view(Path(args['root']), source_root, donor, args)))
'''


REMOTE_REUSE_SUFFIX = r'''
import subprocess, sys, sysconfig
args = json.loads(sys.argv[1])
root = Path(args['root'])
source_root = Path(args['source_root'])
manifest = json.loads((source_root / '.vaws-runtime/ready-profile.json').read_text())
if args['kind'] == 'native':
    result = copy_native_view(root, source_root, manifest, args['versions'])
    compatibility = native_compatibility_receipt(source_root, manifest)
    if compatibility is not None:
        relative = '.vaws-runtime/profile-evidence/native-compatibility.json'
        proof = safe_destination(root, relative)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(json.dumps(compatibility, sort_keys=True) + '\n')
        result['compatibility_evidence'] = relative
else:
    verify(source_root, manifest)
    source_site = Path(sysconfig.get_paths()['purelib'])
    destination_site = Path(subprocess.check_output([args['python'], '-c', 'import sysconfig; print(sysconfig.get_paths()["purelib"])'], text=True).strip())
    result = {'kind': 'dependencies', 'source_root': str(source_root),
              'copied_packages': copy_dependencies(source_site, destination_site)}
receipt = root / '.vaws-runtime/reuse.json'
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
'''
