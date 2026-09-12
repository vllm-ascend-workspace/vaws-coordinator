"""Same-container preparation reuse, executed inside a fresh execution view.

Published roots are never modified. Venvs stay at their original absolute path;
native reuse shares that interpreter and overlays fully copied package outputs.
Native changes copy only dependency packages into a fresh venv before building.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
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


def copy_native_view(root: Path, source_root: Path, manifest: dict, versions: dict) -> dict:
    # `verify`, `checked_file` and `capture` are injected from runtime_profile
    # into the remote preparation script; this module has no remote imports.
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


REMOTE_REUSE_SUFFIX = r'''
import subprocess, sys, sysconfig
args = json.loads(sys.argv[1])
root = Path(args['root'])
source_root = Path(args['source_root'])
manifest = json.loads((source_root / '.vaws-runtime/ready-profile.json').read_text())
if args['kind'] == 'native':
    result = copy_native_view(root, source_root, manifest, args['versions'])
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
