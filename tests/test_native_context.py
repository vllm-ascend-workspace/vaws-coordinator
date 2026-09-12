from unittest.mock import Mock

import pytest

from vaws_coordinator.agent_session import AgentSessions, load_context
from vaws_coordinator.task_client import TaskClient
from test_execution_inputs import repo


@pytest.fixture
def native_env(tmp_path, monkeypatch):
    for name in ("VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT",
                 "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    state = tmp_path / "sessions"
    monkeypatch.setenv("VAWS_AGENT_SESSIONS_DIR", str(state))
    monkeypatch.setenv("CODEX_THREAD_ID", "native-first")
    return state


def test_native_resume_and_new_thread_are_local_and_distinct(native_env, monkeypatch):
    owner = Mock()
    first = TaskClient(service=owner)
    again = TaskClient(service=owner)
    assert first.context["session"]["id"] == again.context["session"]["id"]
    monkeypatch.setenv("CODEX_THREAD_ID", "native-second")
    second = TaskClient(service=owner)
    assert second.context["session"]["id"] != first.context["session"]["id"]
    assert first.status()["executions"] == []
    assert first.context["session"]["sources"] == {}
    assert owner.mock_calls == []


def test_explicit_context_wins_and_invalid_path_never_falls_back(native_env):
    expected = AgentSessions(native_env).attach("claude", "other-client", str(native_env))
    assert load_context(expected["context_file"])["session"]["id"] == expected["session"]["id"]
    with pytest.raises(FileNotFoundError):
        load_context(str(native_env / "missing.json"))
    assert len(AgentSessions(native_env).sessions()) == 1


def test_missing_or_conflicting_identity_does_not_create_state(native_env, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_ID", "another-native-id")
    with pytest.raises(ValueError, match="conflicting native"):
        load_context()
    assert not native_env.exists()
    monkeypatch.delenv("CODEX_THREAD_ID")
    with pytest.raises(ValueError, match="VAWS context is required"):
        load_context()
    assert not native_env.exists()


def test_mcp_does_not_adopt_the_server_process_native_identity(native_env):
    from vaws_coordinator.task_server import call_tool
    reply = call_tool("vaws_session", {})
    assert reply["isError"]
    assert not native_env.exists()


def test_native_status_preserves_a_finished_task(native_env):
    context = load_context()
    store = AgentSessions(native_env)
    with store.transaction() as db:
        session = store.get(db, "session", context["session"]["id"])
        session["state"] = "finished"
        store.put(db, "session", session)
    assert TaskClient(service=Mock()).status()["session"]["state"] == "finished"


def test_explicit_parent_preserves_association(native_env, monkeypatch):
    parent = load_context()
    monkeypatch.setenv("CODEX_THREAD_ID", "child-native")
    monkeypatch.setenv("VAWS_PARENT_CONTEXT", parent["context_file"])
    child = load_context()
    assert child["session"]["id"] == parent["session"]["id"]
    assert child["attachment"]["parent_id"] == parent["attachment"]["id"]


def test_first_native_cli_binds_sources_once_and_shell_cd_keeps_them(native_env, tmp_path, monkeypatch):
    project = repo(tmp_path / "project")
    other = repo(tmp_path / "other")
    monkeypatch.chdir(project)
    first = load_context()
    assert first["source_defaults"]["origin"] == "native-cwd"
    assert first["source_defaults"]["sources"]["project"]["path"] == str(project.resolve())
    assert first["session"]["sources"] == {}
    monkeypatch.chdir(other)
    later = load_context()
    assert later["context_file"] == first["context_file"]
    assert later["attachment"]["cwd"] == str(project.resolve())
    assert later["source_defaults"] == first["source_defaults"]


@pytest.mark.parametrize("association_env", ["VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT"])
def test_first_native_association_preserves_explicit_task_sources(native_env, tmp_path, monkeypatch, association_env):
    project = repo(tmp_path / "project")
    child_root = repo(tmp_path / "child")
    monkeypatch.chdir(project)
    parent = load_context()
    store = AgentSessions(native_env)
    explicit = store.bind_sources(parent, {"chosen": str(project)})
    monkeypatch.setenv("CODEX_THREAD_ID", "native-child")
    monkeypatch.setenv(association_env, parent["context_file"])
    monkeypatch.chdir(child_root)
    child = load_context()
    assert child["session"]["id"] == parent["session"]["id"]
    assert child["attachment"]["sources"]["child"]["path"] == str(child_root.resolve())
    assert child["source_defaults"] == explicit["source_defaults"]
    store.bind_sources(parent, {})
    monkeypatch.chdir(project)
    resumed = load_context()
    assert resumed["attachment"]["cwd"] == str(child_root.resolve())
    assert resumed["source_defaults"] == {"origin": "explicit", "sources": {}}


def test_native_cli_without_git_still_attaches_locally(native_env, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    assert context["source_defaults"]["sources"] == {}
    assert "source reference not yet bound" in capsys.readouterr().err


def test_existing_user_container_needs_no_image_selection():
    from vaws_coordinator.service import CoordinatorService
    service = object.__new__(CoordinatorService)
    service.backend = Mock()
    record = {"host": {"ip": "192.0.2.10", "machine_type": "A3"},
              "container": {"name": "vaws-alice", "ssh_port": 2201}, "user": "alice"}
    service._configured_machines = lambda: [record]
    donor = service._ensure_user_container("alice", {}, {}, set())
    assert donor["ssh_port"] == 2201
    assert donor["recipe"] is None
    assert service.backend.host.call_args.args[1]["action"] == "container-ssh-reserve"
    service.backend.reset_mock()
    assert service._ensure_user_container("bob", {}, {}, set()) is None
    service.backend.host.assert_not_called()
    record["container"] = {}
    assert service._ensure_user_container("alice", {}, {}, set()) is None
    service.backend.host.assert_not_called()
