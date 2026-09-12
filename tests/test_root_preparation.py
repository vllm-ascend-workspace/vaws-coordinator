"""The short root bootstrap shares RPC without changing owned preparation."""
from types import SimpleNamespace

import pytest

from remote_dev.core.ssh_transport import RemoteCompleted
from vaws_coordinator.backend import RemoteBackend
from vaws_coordinator.parity_support import RemoteCommandError
from vaws_coordinator.preparation_process import PreparationCancelled, PreparationProcess


@pytest.fixture
def preparation(monkeypatch):
    backend = RemoteBackend()
    endpoint = {'host': 'fixture.invalid', 'port': 46001, 'user': 'root',
                'root': '/new-execution', 'cwd': '/new-execution'}
    spec = {'user': 'alice', 'endpoint': endpoint, 'python': '/image/python'}
    finished = []
    monkeypatch.setattr(backend, '_write_ready_profile', lambda *args, **kwargs: finished.append(kwargs))
    def run(**kwargs):
        backend.prepare_task_root(spec, sources={}, environment={},
            source_snapshot={'id': 'accepted', 'records': []},
            **{'donor_python': '/image/python', **kwargs})
    return SimpleNamespace(run=run, endpoint=endpoint, finished=finished)


def test_root_rpc_uses_subsequent_job_endpoint_and_preserves_logs(preparation, monkeypatch, tmp_path):
    calls = []
    def rpc(endpoint, script, **kwargs):
        calls.append((endpoint, script, kwargs))
        return RemoteCompleted(0, 'created\n', 'warning\n')
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script', rpc)
    monkeypatch.setattr('vaws_coordinator.parity_support.ssh_exec_stream',
                        lambda *args, **kwargs: pytest.fail('root bootstrap must use pooled RPC'))
    preparation.run(log_dir=tmp_path)
    endpoint, script, options = calls[0]
    assert len(calls) == 1
    assert endpoint.root == endpoint.effective_cwd == preparation.endpoint['root']
    assert (endpoint.host, endpoint.port, endpoint.user) == ('fixture.invalid', 46001, 'root')
    assert options == {'timeout_ms': 45000}
    assert 'mkdir -p /new-execution' in script
    assert (tmp_path / 'prepare-root.log').read_text() == 'created\nwarning\n'
    assert len(preparation.finished) == 1


@pytest.mark.parametrize('with_log', [False, True])
@pytest.mark.parametrize('reply, expected_code', [
    (RemoteCompleted(17, 'stdout evidence', 'stderr failure'), 17),
    (RemoteCompleted(None, 'partial stdout', 'timeout evidence', timed_out=True), 255),
])
def test_root_failure_keeps_returncode_and_output_without_launching_next_step(
        preparation, monkeypatch, tmp_path, with_log, reply, expected_code):
    calls = []
    def rpc(*args, **kwargs):
        calls.append(args)
        return reply
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script', rpc)
    with pytest.raises(RemoteCommandError) as caught:
        preparation.run(log_dir=tmp_path if with_log else None)
    assert caught.value.returncode == expected_code
    assert reply.stdout in str(caught.value) and reply.stderr in str(caught.value)
    assert len(calls) == 1 and not preparation.finished
    if with_log:
        assert (tmp_path / 'prepare-root.log').read_text() == reply.stdout + reply.stderr


def test_unknown_root_rpc_is_not_replayed_or_followed_by_preparation(preparation, monkeypatch, tmp_path):
    calls = []
    def rpc(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('transport lost; outcome unknown')
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script', rpc)
    with pytest.raises(RuntimeError, match='outcome unknown'):
        preparation.run(log_dir=tmp_path)
    assert len(calls) == 1 and not preparation.finished
    assert 'outcome unknown' in (tmp_path / 'prepare-root.log').read_text()


@pytest.mark.parametrize('when', ['before', 'during', 'reply'])
def test_root_cancellation_stops_before_any_next_step(preparation, monkeypatch, when):
    calls = []
    cancelled = when == 'before'
    def rpc(*args, **kwargs):
        nonlocal cancelled
        calls.append(args)
        cancelled = when == 'during'
        return RemoteCompleted(-9 if when == 'reply' else 0, '', '', cancelled=when == 'reply')
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script', rpc)
    with pytest.raises(PreparationCancelled):
        preparation.run(cancel_requested=lambda: cancelled)
    assert len(calls) == (0 if when == 'before' else 1)
    assert not preparation.finished


def test_cold_venv_still_has_owned_process_after_root_bootstrap(preparation, monkeypatch):
    order = []
    def rpc(*args, **kwargs):
        order.append('root-created')
        return RemoteCompleted(0, '', '')
    def owned(endpoint, script, **kwargs):
        assert order == ['root-created']
        process = kwargs['process']
        assert isinstance(process, PreparationProcess)
        assert process.endpoint == preparation.endpoint
        assert process.step == 'create-venv'
        order.append('owned-venv')
    monkeypatch.setattr('remote_dev.core.ssh_transport.run_rpc_script', rpc)
    monkeypatch.setattr('vaws_coordinator.parity_support.ssh_exec_stream', owned)
    preparation.run(donor_python=None, on_preparation_job=lambda job: None)
    assert order == ['root-created', 'owned-venv']
    assert len(preparation.finished) == 1
