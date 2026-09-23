import json

import click
import pytest
from claude_payloads import CONVERSATION, conversation, tool_turn
from click.testing import CliRunner

from cx_chats.claude import plugin


@pytest.fixture
def fake_client(monkeypatch):
    class Client:
        payload = None
        pages = []
        calls = []
        instances = 0

        def __init__(self):
            type(self).instances += 1

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def conversation(self, conversation_id):
            self.calls.append(("conversation", conversation_id))
            return self.payload

        def conversations(self, *, limit, offset):
            self.calls.append(("conversations", limit, offset))
            return self.pages.pop(0)

    Client.calls = []
    monkeypatch.setattr(plugin, "ClaudeClient", Client)
    return Client


def test_chat_url_resolves_to_transcript_with_provenance(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"https://claude.ai/chat/{CONVERSATION}", {})
    assert item["source"] == f"claude:chat/{CONVERSATION}"
    assert item["label"] == "Runtime check"
    assert "↪ Runtime:list [t1]" in item["content"]
    assert item["prose_authors"] == ["user", "assistant"]
    metadata = item["metadata"]
    assert metadata["provider"] == "claude"
    assert metadata["context_subpath"] == f"claude/{CONVERSATION}.md"
    assert metadata["source_url"] == f"https://claude.ai/chat/{CONVERSATION}"
    assert metadata["message_count"] == 2
    assert fake_client.calls == [("conversation", CONVERSATION)]


def test_tool_target_reads_one_call_in_full(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"claude:chat/{CONVERSATION}?tool=t2", {})
    assert item["content"].startswith("# Runtime check · t2 Runtime:open")
    assert item["metadata"]["context_subpath"] == f"claude/{CONVERSATION}/t2.md"
    assert item["metadata"]["is_error"] is True


def test_json_output_returns_service_payload(fake_client):
    fake_client.payload = conversation(*tool_turn())
    item, = plugin.resolve(f"claude:{CONVERSATION}?output=json", {})
    assert json.loads(item["content"]) == fake_client.payload


def test_mismatched_conversation_is_rejected(fake_client):
    fake_client.payload = {**conversation(*tool_turn()), "uuid": "55555555-5555-4555-8555-555555555555"}
    with pytest.raises(ValueError, match="different conversation"):
        plugin.resolve(f"claude:chat/{CONVERSATION}", {})


def test_listing_envelope_and_continuation(fake_client):
    fake_client.pages = [{"data": [
        {"uuid": CONVERSATION, "name": "Runtime check", "created_at": "2026-09-22T19:59:00Z", "updated_at": "2026-09-22T20:01:00Z"},
    ], "has_more": True}]
    envelope = plugin.list_targets("claude:chats?limit=1", {})
    entry, = envelope["targets"]
    assert entry["target"] == f"claude:chat/{CONVERSATION}"
    assert entry["metadata"]["source_modified"] == "2026-09-22T20:01:00Z"
    assert envelope["pagination"]["nextTarget"] == "claude:chats?limit=1&offset=1"
    assert fake_client.calls == [("conversations", 1, 0)]


def test_rendered_listing_names_continuation(fake_client):
    fake_client.pages = [{"data": [{"uuid": CONVERSATION, "name": "Runtime check"}], "has_more": True}]
    item, = plugin.resolve("claude:chats?limit=1", {})
    assert f"- Runtime check — claude:chat/{CONVERSATION}" in item["content"]
    assert "Continue: claude:chats?limit=1&offset=1" in item["content"]


@pytest.mark.parametrize("target", [f"claude:chat/{CONVERSATION}", "claude:chats"])
def test_offline_reads_never_create_a_client(target, fake_client):
    with pytest.raises(ValueError, match="offline"):
        plugin.resolve(target, {"cache_only": True})
    assert fake_client.instances == 0


def test_cli_options_become_overrides():
    @click.command()
    def cat(**params):
        click.echo(json.dumps(plugin.collect_cli_overrides("cat", params)))

    plugin.register_cli_options("cat", cat)
    result = CliRunner().invoke(cat, ["--claude-tool", "t3", "--claude-result-head-tokens", "40"])
    assert json.loads(result.output) == {"tool": "t3", "result_head_tokens": 40}


def test_search_renders_snippets_and_continuation(fake_client, monkeypatch):
    def search(self, query, *, limit, project=None):
        self.calls.append(("search", query, limit, project))
        return {"data": [{"conversation": {"uuid": CONVERSATION, "name": "Runtime check"},
                          "matched_snippet": {"text": "…the runtime\nregistry…"}}] * limit}

    monkeypatch.setattr(fake_client, "search", search, raising=False)
    item, = plugin.resolve("claude:search?query=runtime&limit=1", {})
    assert item["content"].startswith("# claude.ai search: runtime")
    assert f"- Runtime check — claude:chat/{CONVERSATION}\n  …the runtime registry…" in item["content"]
    assert "Continue: claude:search?limit=1&offset=1&query=runtime" in item["content"]
    assert fake_client.calls == [("search", "runtime", 1, None)]


def test_continuation_offset_is_not_reset_by_list_context(fake_client):
    fake_client.pages = [{"data": [{"uuid": CONVERSATION, "name": "Runtime check"}], "has_more": False}]
    plugin.list_targets("claude:chats?limit=100&offset=100", {"list_limit": 100, "list_offset": 0})
    assert fake_client.calls == [("conversations", 100, 100)]
