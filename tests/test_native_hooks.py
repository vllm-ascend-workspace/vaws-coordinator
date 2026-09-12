"""Native tool context injection, including Cursor's asynchronous session start."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
import io
import json

import pytest

from vaws_coordinator.agent_session import AgentSessions
from vaws_coordinator.hooks.vaws_session import handle
from vaws_coordinator.hooks.vaws_session import main
from test_execution_inputs import repo


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT",
                 "VAWS_GITHUB_IDENTITY_FILE", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def cursor_event(root, *, native="conversation", event="preToolUse", **fields):
    return {"conversation_id": native, "generation_id": "first-turn",
            "hook_event_name": event, "cursor_version": "test",
            "workspace_roots": [str(root)], "cwd": str(root),
            "tool_name": "MCP:vaws_run", "tool_input": {"command": "echo ready"}, **fields}


@pytest.mark.parametrize("first", ["sessionStart", "preToolUse"])
def test_cursor_first_tool_and_late_start_share_native_task_and_sources(tmp_path, first):
    source = repo(tmp_path / "project")
    store = AgentSessions(tmp_path / "sessions")
    handle("cursor", cursor_event(source, event=first), store)
    context = store.native_context("cursor", "conversation")
    for event in ("preToolUse", "sessionStart", "preToolUse"):
        output = handle("cursor", cursor_event(source, event=event, generation_id="later-turn"), store)
        if event == "preToolUse":
            assert output == {"updated_input": {"command": "echo ready", "context_file": context["context_file"]}}
    current = store.native_context("cursor", "conversation")
    assert current["session"]["id"] == context["session"]["id"]
    assert current["source_defaults"]["sources"]["project"]["path"] == str(source.resolve())
    assert len(store.sessions()) == 1


def test_cursor_concurrent_start_and_first_tools_create_one_task(tmp_path):
    source = repo(tmp_path / "project")
    store = AgentSessions(tmp_path / "sessions")
    barrier = Barrier(4)

    def start(event):
        barrier.wait(timeout=5)
        return handle("cursor", cursor_event(source, event=event), store)

    with ThreadPoolExecutor(4) as workers:
        results = list(workers.map(start, ["sessionStart", "preToolUse", "sessionStart", "preToolUse"]))
    context = store.native_context("cursor", "conversation")
    assert {results[index]["updated_input"]["context_file"] for index in (1, 3)} == {context["context_file"]}
    assert len(store.sessions()) == 1


@pytest.mark.parametrize("fields", [
    {"tool_name": "Shell", "tool_input": {"command": "echo vaws_run"}},
    {"tool_name": "MCP:unrelated_vaws_run"},
    {"tool_name": "MCP:vaws_run_extra"},
    {"tool_name": "mcp__unrelated__knowledge_query"},
    {"tool_name": "mcp__unrelated__remote_read"},
    {"tool_input": []},
    {"tool_input": {"command": "echo ready", "context_file": "explicit-context"}},
])
def test_cursor_unrelated_or_explicit_calls_never_open_the_task_registry(tmp_path, monkeypatch, fields):
    opening = Mock(side_effect=AssertionError("ordinary tool must not open registry"))
    monkeypatch.setattr("vaws_coordinator.hooks.vaws_session.AgentSessions", opening)
    assert handle("cursor", cursor_event(tmp_path, **fields)) == {}
    opening.assert_not_called()


def test_cursor_missing_native_id_never_uses_cwd_to_create_task(tmp_path, monkeypatch):
    opening = Mock(side_effect=AssertionError("missing identity must not open registry"))
    monkeypatch.setattr("vaws_coordinator.hooks.vaws_session.AgentSessions", opening)
    with pytest.raises(ValueError, match="no native session identity"):
        handle("cursor", cursor_event(tmp_path, native=""))
    opening.assert_not_called()


def test_cursor_first_tool_respects_explicit_association(tmp_path, monkeypatch):
    source = repo(tmp_path / "project")
    store = AgentSessions(tmp_path / "sessions")
    parent = store.attach("codex", "parent", str(source))
    monkeypatch.setenv("VAWS_ATTACH_CONTEXT", parent["context_file"])
    output = handle("cursor", cursor_event(source), store)
    child = store.native_context("cursor", "conversation")
    assert output["updated_input"]["context_file"] == child["context_file"]
    assert child["session"]["id"] == parent["session"]["id"]
    assert len(store.sessions()) == 1


def test_cursor_unknown_subagent_is_not_attached_as_a_new_root(tmp_path):
    store = AgentSessions(tmp_path / "sessions")
    with pytest.raises(ValueError, match="association is missing"):
        handle("cursor", cursor_event(tmp_path, agent_id="child"), store)
    assert store.sessions() == []


@pytest.mark.parametrize("client", ["claude", "codex", "grok", "cursor"])
def test_message_gets_native_context_without_changing_explicit_arguments(tmp_path, client):
    store = AgentSessions(tmp_path / "sessions")
    context = store.attach(client, "native", str(tmp_path))
    arguments = {"recipient": {"reply_reference": "known-reference"}, "text": "ready"}
    payload = {"hook_event_name": "preToolUse", "session_id": "native", "cwd": str(tmp_path),
               "tool_name": "mcp__vaws-task__vaws_message", "tool_input": arguments}
    output = handle(client, payload, store)
    updated = output["updated_input"] if client == "cursor" else output["hookSpecificOutput"]["updatedInput"]
    assert updated == {**arguments, "context_file": context["context_file"]}
    assert "context_file" not in arguments
    explicit = {**arguments, "context_file": "caller-selected-context"}
    assert handle(client, {**payload, "tool_input": explicit}, store) == {}


@pytest.mark.parametrize("name", ["vaws-task__vaws_message", "vaws-knowledge__knowledge_capture", "remote-dev__remote_read"])
def test_grok_dispatcher_keeps_its_nested_envelope(tmp_path, name):
    store = AgentSessions(tmp_path / "sessions")
    context = store.attach("grok", "native", str(tmp_path))
    arguments = {"recipient": {"reply_reference": "known-reference"}, "text": "ready"}
    nested = {"tool_name": name, "tool_input": arguments}
    output = handle("grok", {"hookEventName": "pre_tool_use", "sessionId": "native", "cwd": str(tmp_path),
                             "toolName": "use_tool", "toolInput": nested}, store)
    assert output == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {
        **nested, "tool_input": {**arguments, "context_file": context["context_file"]}}}}


@pytest.mark.parametrize("client,name", [
    ("claude", "mcp__vaws-knowledge__knowledge_query"),
    ("claude", "mcp__vaws-knowledge__knowledge_explain"),
    ("codex", "mcp__vaws_knowledge__knowledge_capture"),
    ("codex", "mcp__remote_dev__remote_read"),
    ("cursor", "MCP:vaws-knowledge__knowledge_query"),
    ("cursor", "MCP:remote-dev__remote_job_status"),
    ("cursor", "MCP:knowledge_query"),
    ("cursor", "MCP:remote_read"),
])
def test_owned_companion_tools_receive_the_same_native_context(tmp_path, client, name):
    store = AgentSessions(tmp_path / "sessions")
    context = store.attach(client, "native", str(tmp_path))
    payload = {"hook_event_name": "preToolUse", "session_id": "native", "cwd": str(tmp_path),
               "tool_name": name, "tool_input": {"query": "existing input"}}
    output = handle(client, payload, store)
    updated = output["updated_input"] if client == "cursor" else output["hookSpecificOutput"]["updatedInput"]
    assert updated == {"query": "existing input", "context_file": context["context_file"]}
    assert handle(client, {**payload, "tool_input": {**updated, "context_file": "explicit-context"}}, store) == {}


@pytest.mark.parametrize("client", ["claude", "codex", "grok"])
@pytest.mark.parametrize("name", ["MCP:knowledge_query", "MCP:remote_read"])
def test_unqualified_companions_are_only_cursor_native_names(tmp_path, client, name):
    store = AgentSessions(tmp_path / "sessions")
    store.attach(client, "native", str(tmp_path))
    assert handle(client, {"hook_event_name": "PreToolUse", "session_id": "native", "cwd": str(tmp_path),
                           "tool_name": name, "tool_input": {}}, store) == {}


def test_hook_hint_reports_current_explicit_workspace_without_claiming_preparation(tmp_path):
    before, prepared = repo(tmp_path / "before"), repo(tmp_path / "prepared")
    store = AgentSessions(tmp_path / "sessions")
    context = store.attach("claude", "native", str(before))
    store.bind_sources(context, {"project": str(prepared)})
    output = handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "native", "cwd": str(before)}, store)
    hint = output["hookSpecificOutput"]["additionalContext"]
    assert context["context_file"] in hint
    assert f'Source defaults (explicit): {json.dumps({"project": str(prepared)})}' in hint
    assert "No session-creation call is needed" not in hint


@pytest.mark.parametrize("agent", [None, "main", "agent-1"])
def test_kimi_extension_prompt_updates_cwd_silently_while_legacy_keeps_context(tmp_path, monkeypatch, capsys, agent):
    before, after = repo(tmp_path / "before"), repo(tmp_path / "after")
    store = AgentSessions(tmp_path / "sessions")
    handle("kimi", {"hook_event_name": "SessionStart", "session_id": "native", "cwd": str(before)}, store)
    if agent == "agent-1":
        handle("kimi", {"hook_event_name": "SubagentStart", "session_id": "native", "agent_id": agent,
                        "parent_agent_id": "main", "cwd": str(before)}, store)
    native_agent = agent if agent == "agent-1" else ""
    original = store.native_context("kimi", "native", native_agent)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "native", "cwd": str(after)}
    if agent:
        payload["agent_id"] = agent
    monkeypatch.setattr("vaws_coordinator.hooks.vaws_session.AgentSessions", lambda: store)
    monkeypatch.setattr("sys.argv", ["hook", "--client", "kimi"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert main() == 0
    text = capsys.readouterr().out.strip()
    if agent:
        assert text == ""
    else:
        assert original["context_file"] in text
    current = store.native_context("kimi", "native", native_agent)
    assert current["session"]["id"] == original["session"]["id"]
    assert current["attachment"]["cwd"] == str(after)
    assert {source["path"] for source in current["source_defaults"]["sources"].values()} == {str(after)}
