"""Shared outputs remain copies; execution roots, interpreters and users stay separate."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest

from vaws_coordinator import preparation_cache as cache, runtime_profile as profile
from vaws_coordinator.backend import RemoteBackend
from vaws_coordinator.provision.task_environment import reusable_preparation


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    source, target, shared = tmp_path / 'alice-build', tmp_path / 'bob-execution', tmp_path / 'host-cache'
    system = tmp_path / 'image-system'
    system.mkdir()
    settings = {field: '1.0' for field in profile.PROFILE_FIELDS}
    settings.update(image_digest='sha256:same-image', python_abi=sysconfig.get_config_var('SOABI'),
                    build_env={}, launch_env={}, compatibility_evidence='smoke.json', system_files={})
    for name in ('cann', 'driver'):
        path = system / name
        path.write_text('same image ' + name)
        settings['system_files'][name] = {'path': str(path), 'sha256': profile.file_digest(path)}
    for root in (source, target):
        for name in ('vllm', 'vllm-ascend'):
            package = root / name / name.replace('-', '_')
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('value = ' + repr(root.name))
    extension = Path(importlib.util.find_spec('_ctypes_test').origin)
    relative = 'vllm-ascend/vllm_ascend/' + extension.name
    shutil.copy2(extension, source / relative)
    metadata = 'vllm-ascend/vllm_ascend/_build_info.py'
    (source / metadata).write_text('SOC_VERSION = "1.0"\n')
    for name in ('cann', 'driver'):
        (source / (name + '.txt')).write_text(name)
    (source / 'smoke.json').write_text(json.dumps({'passed': True}))
    request = reusable_preparation({'id': 'bob-source', 'records': [
        {'relpath': name, 'build_inputs': {'native': 'native-' + name, 'dependencies': 'deps-' + name}}
        for name in ('vllm', 'vllm-ascend')]}, {'recipe': 'test'}, {})
    manifest = profile.capture(source, settings, {'vllm': 'native-vllm', 'vllm-ascend': 'native-ascend'},
                               {relative: 'library', metadata: 'metadata'},
                               {'cann': 'cann.txt', 'driver': 'driver.txt', 'smoke': 'smoke.json'})
    manifest['preparation'] = profile.verified_preparation(request, settings)
    marker = source / '.vaws-runtime/ready-profile.json'
    marker.parent.mkdir()
    marker.write_text(json.dumps(manifest))
    class Distribution:
        version = '1.0'
        def __init__(self, name):
            self.name = name
        def read_text(self, name):
            return {'METADATA': f'Name: {self.name}\nVersion: 1.0\n',
                    'entry_points.txt': '[vllm.platform_plugins]\nnpu=vllm_ascend:register\n'}.get(name)
    monkeypatch.setattr(cache.importlib.metadata, 'distribution', Distribution)
    monkeypatch.setattr(cache.importlib.metadata, 'version', lambda _: '1.0')
    for name in ('digest', 'publish', 'verify', 'file_digest', 'checked_file', 'verified_preparation'):
        monkeypatch.setattr(cache, name, getattr(profile, name), raising=False)
    versions = {name: {'version': '2.0.dev1', 'source_head': 'bob-head'} for name in ('vllm', 'vllm-ascend')}
    return source, target, shared, request, manifest, relative, versions


def test_cross_user_reuse_loads_real_extension_without_donor_runtime(bundle):
    source, target, shared, request, manifest, relative, versions = bundle
    assert cache.store_shared_native(source, shared)['status'] == 'stored'
    shutil.rmtree(source)  # No donor interpreter, editable source or runtime is reused.
    assert cache.restore_shared_native(target, shared, request, 'sha256:same-image', versions)['status'] == 'hit'
    command = "import json,vllm,vllm_ascend._ctypes_test as extension;from vllm._version import __commit_id__;print(json.dumps({'source':vllm.value,'extension':extension.__file__,'head':__commit_id__}))"
    result = subprocess.run([sys.executable, '-c', command], capture_output=True, text=True,
                            env={**os.environ, 'PYTHONPATH': os.pathsep.join(map(str, [target / '.vaws-runtime/metadata',
                                                                                target / 'vllm', target / 'vllm-ascend']))})
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded['source'] == 'bob-execution' and loaded['head'] == 'bob-head'
    assert Path(loaded['extension']).is_relative_to(target)
    original = next((shared / 'bundles').iterdir()) / relative
    expected = original.read_bytes()
    (target / relative).write_bytes(b'local user edit')
    assert original.read_bytes() == expected


@pytest.mark.parametrize('change', ['native', 'dependencies', 'image', 'abi', 'cann', 'corrupt'])
def test_mismatches_do_not_copy_artifacts(bundle, monkeypatch, change):
    source, target, shared, request, manifest, relative, versions = bundle
    cache.store_shared_native(source, shared)
    image = 'sha256:same-image'
    if change in ('native', 'dependencies'):
        request = copy.deepcopy(request)
        request[change]['vllm'] = 'different-input'
    elif change == 'image':
        image = 'sha256:another-image'
    elif change == 'abi':
        monkeypatch.setattr(cache.sysconfig, 'get_config_var', lambda _: 'different-abi')
    elif change == 'cann':
        Path(manifest['profile']['system_files']['cann']['path']).write_text('CANN changed')
    else:
        (next((shared / 'bundles').iterdir()) / relative).write_bytes(b'changed output')
    try:
        result = cache.restore_shared_native(target, shared, request, image, versions)
    except ValueError:
        pass
    else:
        assert result['status'] == 'miss'
    assert not (target / relative).exists()


def test_cached_outputs_can_be_discarded_before_normal_build(bundle):
    source, target, shared, request, manifest, relative, versions = bundle
    cache.store_shared_native(source, shared)
    cache.restore_shared_native(target, shared, request, 'sha256:same-image', versions)
    cache.discard_shared_native(target)
    assert not (target / relative).exists()
    assert not (target / '.vaws-runtime/metadata').exists()
    assert (target / 'vllm/vllm/__init__.py').read_text() == "value = 'bob-execution'"
    assert (next((shared / 'bundles').iterdir()) / relative).is_file()


@pytest.mark.parametrize('failure', [None, 'verify-imports', 'profile', 'cache-miss',
                                    'cancel-imports', 'uncertain-imports', 'cancel-profile', 'uncertain-profile'])
def test_normal_preparation_uses_cache_and_rebuilds_failed_hit(tmp_path, monkeypatch, failure):
    import vaws_coordinator.parity as parity
    import vaws_coordinator.parity_support as transport
    from remote_dev.core.ssh_transport import RemoteCompleted
    from vaws_coordinator.preparation_process import PreparationCancelled, PreparationUncertain
    backend = RemoteBackend()
    spec = {'user': 'bob', 'container_name': 'vaws-bob', 'python': '/bob/.venv/bin/python',
            'endpoint': {'host': 'host', 'port': 2202, 'user': 'root', 'root': '/bob'},
            'host_endpoint': {'host': 'host', 'port': 22, 'user': 'root'}}
    snapshot = {'id': 'source', 'records': [{'relpath': name, 'scm_version': '1.0', 'source_head': 'head'}
                                          for name in ('vllm', 'vllm-ascend')]}
    monkeypatch.setattr(parity, 'materialize_fixed_sources', lambda **kwargs: None)
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script',
                        lambda *a, **k: RemoteCompleted(0, '', ''))
    monkeypatch.setattr(transport, 'ssh_exec_stream', lambda *a, **k: None)
    monkeypatch.setattr(backend, 'bash', lambda *a: '')
    monkeypatch.setattr(backend, 'inspect', lambda *a, **k: pytest.fail('registration owns the full attestation'))
    events, installed, operations = [], [], []
    failed = False
    def install(**kwargs):
        nonlocal failed
        step = kwargs['step']
        installed.append(step)
        if step == 'verify-imports' and failure in ('cancel-imports', 'uncertain-imports'):
            raise (PreparationCancelled if failure.startswith('cancel') else PreparationUncertain)('owned process interrupted')
        if failure == step and not failed:
            failed = True
            raise RuntimeError('cached import does not load')
    def write_profile(*args, **kwargs):
        nonlocal failed
        if failure in ('cancel-profile', 'uncertain-profile'):
            raise (PreparationCancelled if failure.startswith('cancel') else PreparationUncertain)('owned process interrupted')
        if failure == 'profile' and not failed:
            failed = True
            raise RuntimeError('cached profile import does not load')
    def operation(spec, action, versions, **kwargs):
        operations.append(action)
        return {'status': {'restore': 'miss' if failure == 'cache-miss' else 'hit',
                           'store': 'miss', 'discard': 'discarded'}[action], 'reason': 'fixture cache unavailable'}
    monkeypatch.setattr(parity, 'run_runtime_install_step', install)
    monkeypatch.setattr(backend, '_write_ready_profile', write_profile)
    monkeypatch.setattr(backend, '_shared_native', operation)
    if failure and failure.startswith(('cancel-', 'uncertain-')):
        with pytest.raises(PreparationCancelled if failure.startswith('cancel') else PreparationUncertain):
            backend.prepare_task_root(spec, sources={'vllm': '/a', 'vllm-ascend': '/b'}, environment={},
                                      source_snapshot=snapshot, on_progress=events.append)
        assert operations == ['restore']
        assert 'install-vllm-ascend' not in installed
        return
    assert backend.prepare_task_root(spec, sources={'vllm': '/a', 'vllm-ascend': '/b'}, environment={},
                                     source_snapshot=snapshot, on_progress=events.append) is None
    assert installed.count('install-vllm-ascend') == (0 if failure is None else 1)
    assert installed.count('install-vllm-ascend-requirements') == 1
    assert 'verify-imports' in installed and 'verify-deps' in installed
    assert operations == (['restore'] if failure is None else ['restore', 'discard', 'store']
                          if failure in ('verify-imports', 'profile') else ['restore', 'store'])
    assert spec['python'] == '/bob/.venv/bin/python'


def test_known_host_weight_mounts_keep_original_paths():
    from vaws_coordinator.provision import host_ops
    probe = host_ops.render_host_probe_script()
    bootstrap = host_ops.render_bootstrap_host_script()
    assert '["/home", "/tmp", "/weight", "/weights", "/models", "/data", "/mnt"]' in probe
    assert 'for optional in /home /tmp /weight /weights /models /data /mnt; do' in bootstrap
    assert 'mount_args+=("-v" "$optional:$optional")' in bootstrap
    assert 'if [ -e "$optional" ]; then' in bootstrap


def test_actual_verification_payload_reads_execution_source_and_metadata(tmp_path):
    from vaws_coordinator.parity import runtime_install_step_script
    root, image = tmp_path / 'execution', tmp_path / 'image-python'
    image.mkdir()
    for name, body in {'torch': '__version__ = "1.0"', 'torch_npu': '',
                       'vllm': 'raise RuntimeError("loaded old image vllm")',
                       'vllm_ascend': 'raise RuntimeError("loaded old image ascend")'}.items():
        (image / (name + '.py')).write_text(body)
    for name in ('vllm', 'vllm-ascend'):
        package = root / name / name.replace('-', '_')
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('__version__ = "current-source"\n')
    marker = root / '.vaws-runtime/shared-native.json'
    marker.parent.mkdir()
    marker.write_text('{}')
    for base, requires in ((root / '.vaws-runtime/metadata', ''),
                           (image, 'Requires-Dist: definitely-missing-fixture-package>=999\n')):
        metadata = base / 'vllm_ascend-1.0.dist-info/METADATA'
        metadata.parent.mkdir(parents=True)
        metadata.write_text('Name: vllm-ascend\nVersion: 1.0\n' + requires)
    for step in ('verify-imports', 'verify-deps'):
        script = runtime_install_step_script(runtime_root=str(root), marker_dirname='.runtime',
                                              container_identity='vaws-bob', step=step, python=sys.executable)
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(image)}, timeout=30)
        assert result.returncode == 0, result.stderr
        assert ('current-source' if step == 'verify-imports' else 'dependency-check=ok') in result.stdout
