import json
import subprocess
from types import SimpleNamespace

import pytest

from vaws_coordinator import parity
from vaws_coordinator.preparation_process import PreparationCancelled, PreparationUncertain
from vaws_coordinator.shared_source_objects import export_existing_objects


def container(identifier='a'):
    return {'Id': identifier * 64, 'Name': '/vaws-example', 'Image': 'sha256:' + 'b' * 64,
            'Config': {'Labels': {'com.vaws.managed': 'true'}}, 'State': {'Running': True},
            'Mounts': [{'Type': 'bind', 'Source': '/tmp', 'Destination': '/tmp', 'RW': True}]}


def request():
    return {'records': [{'commit': 'c' * 40, 'tree': 'd' * 40, 'repo_id': 'project',
                         'shared_mirror': '/tmp/shared/project.git'}],
            'export_source': 'bounded export program', 'legacy_cache': '/private-cache'}


def test_export_uses_fixed_docker_id_and_only_requested_objects():
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == 'ps':
            return SimpleNamespace(stdout='aaaa\n')
        if args[1] == 'inspect':
            return SimpleNamespace(stdout=json.dumps([container()]))
        assert args[:5] == ['docker', 'exec', '-i', 'a' * 64, 'python3']
        assert json.loads(kwargs['input']) == {'records': request()['records'], 'legacy_cache': '/private-cache'}
        return SimpleNamespace(returncode=0, stdout=json.dumps({'copied': ['c' * 40]}), stderr='')
    assert export_existing_objects(request(), run) == {'status': 'copied', 'copied': ['c' * 40]}
    assert len(calls) == 3


@pytest.mark.parametrize('damage', ['label', 'mount', 'id', 'stopped', 'image'])
def test_unqualified_container_is_not_read(damage):
    info = container()
    if damage == 'label':
        info['Config']['Labels'] = {}
    elif damage == 'mount':
        info['Mounts'] = []
    elif damage == 'id':
        info['Id'] = 'reused-name'
    elif damage == 'stopped':
        info['State']['Running'] = False
    else:
        info['Image'] = 'mutable-tag'
    def run(args, **kwargs):
        assert args[1] != 'exec'
        return SimpleNamespace(stdout='aaaa\n' if args[1] == 'ps' else json.dumps([info]))
    assert export_existing_objects(request(), run) == {'status': 'miss', 'copied': []}


@pytest.mark.parametrize('outcome', ['failed', 'timeout'])
def test_export_failure_is_not_replayed_on_another_donor(outcome):
    calls = []
    def run(args, **kwargs):
        if args[1] == 'ps':
            return SimpleNamespace(stdout='aaaa\nbbbb\n')
        if args[1] == 'inspect':
            return SimpleNamespace(stdout=json.dumps([container(), container('e')]))
        calls.append(args)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        return SimpleNamespace(returncode=1, stdout='', stderr='copy incomplete')
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        export_existing_objects(request(), run)
    assert len(calls) == 1


@pytest.mark.parametrize('reply, error', [
    ({'status': 'cancelled'}, PreparationCancelled),
    ({'status': 'failed', 'remote_outcome': 'unknown'}, PreparationUncertain),
    ({'status': 'failed', 'exit_code': 1, 'stderr_tail': 'bad object'}, parity.ParityUnavailable),
    ({'status': 'timeout'}, PreparationUncertain),
    ({'status': 'copied', 'remote_outcome': 'unknown'}, PreparationUncertain),
])
def test_transport_failure_never_becomes_cache_miss(monkeypatch, reply, error):
    import remote_dev.core.ssh_transport as transport
    monkeypatch.setattr(transport, 'run_remote_python', lambda *args, **kwargs: reply)
    with pytest.raises(error):
        parity._export_existing_source_objects({'host': 'fixture', 'user': 'test', 'port': 22},
                                               request()['records'], '/private-cache')
