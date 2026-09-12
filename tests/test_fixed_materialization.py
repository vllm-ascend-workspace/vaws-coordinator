"""Real local Git peers run the exact remote program without SSH or devices."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from vaws_coordinator import parity
from vaws_coordinator.execution_sources import source_identity


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def make_record(repo, name, *, submodules=()):
    commit = git(repo, 'rev-parse', 'HEAD')
    return parity.SnapshotRecord(name, name.replace('/', '__'), commit, None, commit,
        git(repo, 'rev-parse', 'HEAD^{tree}'), 'refs/inputs/' + commit, [], list(submodules),
        source_path=str(repo))


def snapshot(records):
    result = {'schema_version': 'vaws.execution-sources.v1', 'sources': {},
              'records': [asdict(row) for row in records], 'build_env': {}}
    result['id'] = source_identity(result)
    return result


@pytest.fixture
def peer(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('the remote Linux flock program runs in WSL, not native Windows')
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    (source / 'model.py').write_text('value = 1\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'initial')
    commands, transfers = [], []
    original_git = parity.git

    def stream(endpoint, script, **kwargs):
        commands.append(script)
        completed = subprocess.run(['bash', '-s'], input=script, capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError(completed.stderr)
        return SimpleNamespace(stdout=completed.stdout)

    def transfer(repo, args, **kwargs):
        if args[0] == 'push':
            transfers.append(args)
        return original_git(repo, args, **kwargs)

    monkeypatch.setattr(parity, 'ssh_exec_stream', stream)
    monkeypatch.setattr(parity, 'git', transfer)
    monkeypatch.setattr(parity, 'git_remote_url', lambda endpoint, mirror: mirror)
    monkeypatch.setattr(parity, 'git_ssh_environment', lambda endpoint: os.environ.copy())
    def run(records=None, root='runtime'):
        return parity.materialize_fixed_sources(workspace_id='test',
            endpoint={'host': 'fixture', 'port': 22, 'user': 'fixture', 'root': str(tmp_path / root)},
            source_snapshot=snapshot(records or [make_record(source, 'project')]),
            container_cache_root=str(tmp_path / 'cache'))
    return SimpleNamespace(source=source, commands=commands, transfers=transfers, run=run, stream=stream,
                           cache=tmp_path / 'cache', root=tmp_path / 'runtime')


def test_cold_two_operations_warm_one_without_recapture(peer):
    record = make_record(peer.source, 'project')
    result = peer.run([record])
    assert result['status'] == 'materialized'
    assert len(peer.commands) == 2 and len(peer.transfers) == 1
    assert peer.transfers[0][-2].startswith(record.commit + ':refs/vaws/snapshots/')
    peer.commands.clear()
    peer.transfers.clear()
    # Current user files and even local retained refs are irrelevant on a hit.
    (peer.source / 'model.py').write_text('new unsubmitted bytes\n')
    result = peer.run([record], root='second')
    assert len(peer.commands) == 1 and not peer.transfers
    assert (peer.root.parent / 'second/project/model.py').read_text() == 'value = 1\n'
    assert result['commits'] == {'project': record.commit}
    mirror = Path(parity.mirror_path_for(str(peer.cache), 'test', record))
    local_objects = peer.root.parent / 'second/project/.git/objects'
    objects = [path for path in (mirror / 'objects').rglob('*') if path.is_file()]
    assert objects and any((local_objects / path.relative_to(mirror / 'objects')).stat().st_ino
                           == path.stat().st_ino for path in objects)


def test_concurrent_roots_ignore_mutable_mirror_refs(peer):
    first = make_record(peer.source, 'project')
    peer.run([first])
    (peer.source / 'model.py').write_text('value = 2\n')
    git(peer.source, 'commit', '-am', 'second')
    second = make_record(peer.source, 'project')
    peer.run([second], root='second')
    mirror = parity.mirror_path_for(str(peer.cache), 'test', first)
    git(mirror, 'update-ref', 'refs/heads/parity-current', second.commit)
    git(mirror, 'update-ref', 'refs/parity/test/transport-carrier', second.commit)
    peer.commands.clear()
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda pair: peer.run([pair[0]], root=pair[1]),
                                    [(first, 'parallel-one'), (second, 'parallel-two')]))
    assert [row['commits']['project'] for row in results] == [first.commit, second.commit]
    assert len(peer.commands) == 2
    assert git(mirror, 'rev-parse', 'refs/vaws/snapshots/' + first.commit) == first.commit


def test_missing_reply_does_not_touch_root_and_transfer_failure_keeps_mirror(peer, monkeypatch):
    first = make_record(peer.source, 'project')
    peer.run([first])
    receipt = peer.root / '.vaws-runtime/source-materialization.json'
    before = receipt.read_bytes()
    (peer.source / 'model.py').write_text('value = 2\n')
    git(peer.source, 'commit', '-am', 'second')
    second = make_record(peer.source, 'project')
    original = parity.git
    def fail(repo, args, **kwargs):
        if args[0] == 'push':
            assert receipt.read_bytes() == before
            raise RuntimeError('injected failed transfer')
        return original(repo, args, **kwargs)
    monkeypatch.setattr(parity, 'git', fail)
    with pytest.raises(RuntimeError, match='injected failed transfer'):
        peer.run([second])
    assert receipt.read_bytes() == before
    mirror = parity.mirror_path_for(str(peer.cache), 'test', first)
    assert git(mirror, 'rev-parse', 'refs/vaws/snapshots/' + first.commit) == first.commit


def test_retry_same_inputs_repairs_tracked_and_untracked_files(peer):
    record = make_record(peer.source, 'project')
    peer.run([record])
    (peer.root / 'project/model.py').write_text('damaged\n')
    (peer.root / 'project/extra.py').write_text('untracked\n')
    peer.run([record])
    assert (peer.root / 'project/model.py').read_text() == 'value = 1\n'
    assert not (peer.root / 'project/extra.py').exists()


def test_different_inputs_cannot_overwrite_owned_root(peer):
    first = make_record(peer.source, 'project')
    peer.run([first])
    (peer.source / 'model.py').write_text('value = 2\n')
    git(peer.source, 'commit', '-am', 'second')
    with pytest.raises(RuntimeError, match='belongs to different inputs'):
        peer.run()
    assert git(peer.root / 'project', 'rev-parse', 'HEAD') == first.commit


def test_submodule_checkout_is_complete_before_parent_validation(peer):
    child = peer.source.parent / 'child'
    git(peer.source.parent, 'clone', '-q', str(peer.source), str(child))
    git(peer.source, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(child), 'nested')
    git(peer.source, 'commit', '-am', 'submodule')
    parent = make_record(peer.source, 'project', submodules=[{'name': 'nested', 'path': 'nested'}])
    nested = make_record(peer.source / 'nested', 'project/nested')
    peer.run([nested, parent])
    assert git(peer.root / 'project', 'status', '--porcelain') == ''
    assert git(peer.root / 'project/nested', 'rev-parse', 'HEAD') == nested.commit


def test_verification_failure_never_publishes_receipt(peer, monkeypatch):
    record = make_record(peer.source, 'project')
    peer.run([record], root='donor')
    def corrupt(endpoint, script, **kwargs):
        script = script.replace('observed = {}',
            "(root / 'project/model.py').write_text('corrupted')\n        observed = {}")
        return peer.stream(endpoint, script, **kwargs)
    monkeypatch.setattr(parity, 'ssh_exec_stream', corrupt)
    with pytest.raises(RuntimeError, match='git diff failed'):
        peer.run([record])
    assert not (peer.root / '.vaws-runtime/source-materialization.json').exists()


def test_owned_missing_and_materialize_use_distinct_durable_jobs(peer, monkeypatch):
    from vaws_coordinator import preparation_process
    from vaws_coordinator.parity_support import ssh_exec_stream
    jobs, saved = [], []
    def control(endpoint, job_id, action, **kwargs):
        assert action == 'launch'
        assert saved[-1]['job_id'] == job_id and saved[-1]['state'] == 'pending'
        jobs.append(job_id)
        # The real remote-dev worker persists its job before launching the
        # command, including the first missing-object probe.
        job_dir = Path(endpoint['root']) / '.remote-dev/jobs' / job_id
        job_dir.mkdir(parents=True)
        (job_dir / 'receipt.json').write_text(json.dumps({'job_id': job_id, 'owned': True}))
        script = kwargs['spec']['command']
        result = subprocess.run(['bash', '-s'], input=script, capture_output=True, text=True)
        return {'quiet': True, 'state': 'completed', 'result': {'exit_code': result.returncode},
                'stdout': result.stdout, 'stderr': result.stderr}
    monkeypatch.setattr(preparation_process, 'control', control)
    monkeypatch.setattr(parity, 'ssh_exec_stream', ssh_exec_stream)
    endpoint = {'host': 'fixture', 'port': 22, 'user': 'fixture',
                'root': str(peer.root), 'cwd': str(peer.root)}
    process = preparation_process.PreparationProcess(endpoint, 'materialize',
        lambda row: saved.append(dict(row)), lambda: False)
    result = parity.materialize_fixed_sources(workspace_id='test', endpoint=endpoint,
        source_snapshot=snapshot([make_record(peer.source, 'project')]),
        container_cache_root=str(peer.cache), process=process)
    assert result['status'] == 'materialized'
    assert len(jobs) == 2 and len(set(jobs)) == 2
    assert saved[-1]['quiet']
    for job_id in jobs:
        receipt = peer.root / '.remote-dev/jobs' / job_id / 'receipt.json'
        assert json.loads(receipt.read_text()) == {'job_id': job_id, 'owned': True}


@pytest.mark.parametrize('name', ['.remote-dev', '.remote-dev/nested', '.vaws-runtime', '.venv', '.git'])
def test_sources_cannot_overlap_owned_runtime_directories(tmp_path, monkeypatch, name):
    row = parity.SnapshotRecord(name, name.replace('/', '__'), 'a' * 40, None, 'a' * 40,
                               'b' * 40, 'refs/input', [], [], source_path=str(tmp_path))
    def unexpected(*args, **kwargs):
        raise AssertionError('reserved source paths must fail before remote work')
    monkeypatch.setattr(parity, 'ssh_exec_stream', unexpected)
    with pytest.raises(ValueError, match='overlaps coordinator-owned runtime files'):
        parity.materialize_fixed_sources(workspace_id='test',
            endpoint={'host': 'fixture', 'port': 22, 'user': 'fixture', 'root': '/isolated/run'},
            source_snapshot=snapshot([row]))


def test_preserves_environment_and_ignored_native_outputs(peer):
    (peer.source / '.gitignore').write_text('*.so\n')
    git(peer.source, 'add', '.')
    git(peer.source, 'commit', '-qm', 'native output ignore')
    record = make_record(peer.source, 'project')
    peer.run([record])
    native = peer.root / 'project/native.so'
    native.write_bytes(b'native-output')
    environment = peer.root / '.venv'
    environment.mkdir()
    (environment / 'interpreter').write_bytes(b'owned-environment')
    peer.run([record])
    assert native.read_bytes() == b'native-output'
    assert (environment / 'interpreter').read_bytes() == b'owned-environment'


@pytest.mark.parametrize('reply', [None, 'broken', {}, {'status': 'missing', 'missing': []},
                                  {'status': 'materialized', 'source_id': 'wrong'}])
def test_invalid_or_uncertain_reply_is_not_replayed(tmp_path, monkeypatch, reply):
    row = parity.SnapshotRecord('project', 'project', 'a' * 40, None, 'a' * 40, 'b' * 40,
                               'refs/input', [], [], source_path=str(tmp_path))
    calls = []
    def stream(*args, **kwargs):
        calls.append(args)
        if reply is None:
            raise RuntimeError('owned process outcome is unknown')
        return SimpleNamespace(stdout=reply if isinstance(reply, str) else json.dumps(reply))
    monkeypatch.setattr(parity, 'ssh_exec_stream', stream)
    with pytest.raises(RuntimeError):
        parity.materialize_fixed_sources(workspace_id='test',
            endpoint={'host': 'fixture', 'port': 22, 'user': 'fixture', 'root': '/isolated/run'},
            source_snapshot=snapshot([row]))
    assert len(calls) == 1
