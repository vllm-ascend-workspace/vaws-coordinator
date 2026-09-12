import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vaws_coordinator.shared_native_discovery import DISCOVER, export_verified_donor


@pytest.mark.parametrize('mismatch', [None, 'image', 'mount', 'unmanaged', 'no-profile', 'failed-export'])
def test_discovery_exports_only_compatible_verified_roots(mismatch):
    calls = []
    request = {'image_digest': 'sha256:same', 'preparation': {'native': {'vllm': 'same'}},
               'export_source': 'publish verified outputs only'}
    container = {'Name': '/vaws-alice', 'Image': 'sha256:same',
                 'Mounts': [{'Type': 'bind', 'Source': '/tmp', 'Destination': '/tmp', 'RW': True}]}
    if mismatch == 'image':
        container['Image'] = 'sha256:other'
    elif mismatch == 'mount':
        container['Mounts'] = []
    elif mismatch == 'unmanaged':
        container['Name'] = '/production-unrelated'
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output, code = '', 0
        if argv[1] == 'ps':
            output = 'container-id\n'
        elif argv[1] == 'inspect':
            output = json.dumps([container])
        elif argv[2] != '-i':
            output = json.dumps([] if mismatch == 'no-profile' else [{'root': '/vllm-workspace/executions/alice/host/default',
                                   'profile': {'launch_env': {'PATH': '/donor/.venv/bin:/usr/bin'}}}])
        else:
            assert kwargs['input'] == request['export_source']
            assert 'export PATH=/donor/.venv/bin:/usr/bin' in argv[-1]
            assert 'exec python3 - ' in argv[-1]
            output, code = json.dumps({'status': 'stored'}), int(mismatch == 'failed-export')
        return SimpleNamespace(stdout=output, stderr='fixture failure' if code else '', returncode=code)
    reply = export_verified_donor(request, run)
    assert reply['status'] == ('stored' if mismatch is None else 'miss')
    if mismatch in ('image', 'mount', 'unmanaged'):
        assert len(calls) == 2
    assert all('rm' not in argv and 'stop' not in argv and 'start' not in argv for argv, _ in calls)


@pytest.mark.parametrize('mismatch', [None, 'soc', 'build_env', 'native', 'unqualified'])
def test_actual_metadata_discovery_accepts_equivalent_image_request(tmp_path, mismatch):
    root = tmp_path / 'executions/alice/host/default'
    marker = root / '.vaws-runtime/ready-profile.json'
    marker.parent.mkdir(parents=True)
    prior = {'dependencies': {'vllm': 'deps'}, 'native': {'vllm': 'v', 'vllm-ascend': 'a'},
             'environment': {'environment': {}, 'build_env': {}}}
    manifest = {'runtime_root': str(root.resolve()), 'profile': {'image_digest': 'sha256:same', 'soc': 'A3'},
                'preparation': prior}
    if mismatch == 'unqualified':
        manifest.pop('preparation')
    marker.write_text(json.dumps(manifest))
    request = json.loads(json.dumps(prior))
    request['environment']['environment'] = {'image': 'registry/image@sha256:reference', 'soc': 'A3'}
    if mismatch == 'soc':
        request['environment']['environment']['soc'] = 'A2'
    elif mismatch == 'build_env':
        request['environment']['build_env']['CXXFLAGS'] = '-different'
    elif mismatch == 'native':
        request['native']['vllm'] = 'other-vllm'
    script = DISCOVER.replace("pathlib.Path('/vllm-workspace')", 'pathlib.Path(' + repr(str(tmp_path)) + ')')
    result = subprocess.run([sys.executable, '-c', script, json.dumps({'preparation': request, 'image_digest': 'sha256:same'})],
                            capture_output=True, text=True, check=True)
    found = json.loads(result.stdout)
    assert bool(found) == (mismatch is None)
    if found:
        assert found[0]['root'] == str(root)
